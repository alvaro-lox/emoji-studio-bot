"""Рендер подарка в MP4 60 fps (в Telegram он показывается как GIF) и в .gif-файл.

Раскладка повторяет превью подарка Telegram: блок 420×280 с радиальным градиентом,
узором из символа, моделью по центру и подписью снизу. Блок масштабируется под
формат холста (3:2, 1:1, 9:16); вместо градиента можно подложить свою картинку.
"""

import glob
import gzip
import hashlib
import io
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import cast

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from rlottie_python import LottieAnimation

from app.nft.parser import CANVAS_H, CANVAS_W, GiftInfo

FPS = 60
NO_BACKDROP_COLOR = "#17212b"  # фон тёмной темы Telegram
GRADIENT_RADIUS = 0.6965  # r="69.65%" из SVG страницы, в долях высоты блока

# В единицах блока 420×280
MODEL_SIZE, MODEL_CY = 160, 110
TITLE_Y, SUBTITLE_Y = 226, 253
TITLE_SIZE, SUBTITLE_SIZE = 22, 15
NO_BACKDROP_MODEL = 0.62  # доля меньшей стороны холста
NO_TEXT_MODEL = 190  # без подписи модель крупнее и по центру блока

# Холст в единицах масштаба (×scale): 3 → 1260×840, 1080×1080, 1080×1920
ASPECTS = {"3:2": (420, 280), "1:1": (360, 360), "9:16": (360, 640)}

FONT_DIRS = [
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    r"C:\Windows\Fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
]
FONTS = {
    "bold": [
        "Inter-SemiBold.*",
        "Inter-Bold.*",
        "InterVariable.*",
        "Inter.ttc",
        "seguisb.ttf",
        "segoeuib.ttf",
        "DejaVuSans-Bold.ttf",
        "arialbd.ttf",
    ],
    "regular": [
        "Inter-Medium.*",
        "Inter-Regular.*",
        "InterVariable.*",
        "Inter.ttc",
        "segoeui.ttf",
        "DejaVuSans.ttf",
        "arial.ttf",
    ],
}
VARIABLE_WEIGHTS = {"bold": b"SemiBold", "regular": b"Medium"}


@dataclass
class GiftStyle:
    aspect: str = "3:2"
    background: bytes | None = None  # своя картинка вместо градиента
    text: str = "default"  # default | custom | none
    custom_text: str = ""

    @property
    def is_default(self) -> bool:
        return self.aspect == "3:2" and self.background is None and self.text == "default"

    def key(self) -> str:
        if self.is_default:
            return "d"
        raw = f"{self.aspect}|{self.text}|{self.custom_text}|".encode()
        if self.background:
            raw += hashlib.sha1(self.background).digest()
        return hashlib.sha1(raw).hexdigest()[:12]


@dataclass
class RenderResult:
    path: Path
    width: int
    height: int
    duration: int


@lru_cache
def find_font(kind: str, override: str | None = None) -> str | None:
    if override and os.path.exists(override):
        return override
    for pattern in FONTS[kind]:
        for folder in FONT_DIRS:
            found = glob.glob(os.path.join(folder, "**", pattern), recursive=True)
            if found:
                return sorted(found)[0]
    return None


def _font(kind: str, size: int, override: str | None) -> ImageFont.FreeTypeFont:
    path = find_font(kind, override)
    if not path:
        # Pillow ≥ 10.1 с FreeType отдаёт масштабируемый шрифт (тот же класс, что и truetype)
        return cast(ImageFont.FreeTypeFont, ImageFont.load_default(size))
    font = ImageFont.truetype(path, size)
    try:  # вариативный шрифт (InterVariable) по умолчанию тонкий — выбираем начертание
        if VARIABLE_WEIGHTS[kind] in font.get_variation_names():
            font.set_variation_by_name(VARIABLE_WEIGHTS[kind].decode())
    except OSError:
        pass  # обычный, не вариативный шрифт
    return font


def _rgb(hex_color: str | None) -> np.ndarray:
    h = (hex_color or NO_BACKDROP_COLOR).lstrip("#")
    return np.array([int(h[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def format_number(number: int | None) -> str:
    return f"#{number:,}".replace(",", " ") if number is not None else ""


def _paste_alpha(base: Image.Image, tile: Image.Image, x: int, y: int) -> None:
    """alpha_composite, который понимает отрицательные координаты."""
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(tile.width, base.width - x), min(tile.height, base.height - y)
    if right > left and bottom > top:
        base.alpha_composite(tile.crop((left, top, right, bottom)), (x + left, y + top))


@dataclass
class Layout:
    width: int
    height: int
    k: float  # масштаб блока 420×280
    bx: float  # левый верхний угол блока
    by: float

    @classmethod
    def make(cls, aspect: str, scale: int) -> "Layout":
        w, h = (v * scale for v in ASPECTS.get(aspect, ASPECTS["3:2"]))
        # для вертикального холста блок шире экрана: модель крупнее, узор обрезается по бокам
        k = max(w / CANVAS_W, h / CANVAS_H * 0.55)
        return cls(w, h, k, (w - CANVAS_W * k) / 2, (h - CANVAS_H * k) / 2)

    def x(self, v: float) -> float:
        return self.bx + v * self.k

    def y(self, v: float) -> float:
        return self.by + v * self.k


def _gradient(info: GiftInfo, lay: Layout) -> Image.Image:
    yy, xx = np.mgrid[0 : lay.height, 0 : lay.width].astype(np.float32)
    radius = GRADIENT_RADIUS * max(CANVAS_H * lay.k, 0.8 * lay.height)
    t = np.clip(np.hypot(xx - lay.width / 2, yy - lay.y(CANVAS_H / 2)) / radius, 0, 1)[..., None]
    c0, c1 = _rgb(info.center_color), _rgb(info.edge_color)
    grad = c0 + (c1 - c0) * t
    grad += np.random.default_rng(0).uniform(-0.5, 0.5, grad.shape)  # дизеринг против полос
    return Image.fromarray(np.clip(grad, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _custom_background(data: bytes, lay: Layout) -> Image.Image:
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    image = ImageOps.fit(image, (lay.width, lay.height), Image.Resampling.LANCZOS)
    # затемняем низ, чтобы подпись читалась на любой картинке
    shade = np.linspace(0, 1, lay.height, dtype=np.float32)[:, None, None] ** 2 * 0.55 + 0.12
    arr = np.asarray(image, dtype=np.float32) * (1 - shade)
    return Image.fromarray(arr.astype(np.uint8), "RGB").convert("RGBA")


def _draw_pattern(img: Image.Image, info: GiftInfo, pattern: bytes, lay: Layout) -> None:
    alpha = Image.open(io.BytesIO(pattern)).convert("RGBA").getchannel("A")
    color = tuple(int(v) for v in _rgb(info.pattern_color or "#ffffff"))
    for item in info.pattern:
        size = max(1, round(100 * item.scale * lay.k))
        a = alpha.resize((size, size), Image.Resampling.LANCZOS).point(lambda v, o=item.opacity: round(v * o))
        tile = Image.new("RGBA", (size, size), (*color, 255))
        tile.putalpha(a)
        _paste_alpha(img, tile, round(lay.x(item.x)), round(lay.y(item.y)))


FRAGMENT_MODEL_SHARE = 0.63  # модель в карточке Fragment занимает 63% ширины
OVERLAY_OPACITY = 0.3


def _draw_overlay(img: Image.Image, info: GiftInfo, lay: Layout) -> None:
    """Узор из карточки Fragment: масштаб как у модели, цвет — светлее центра фона, полупрозрачный."""
    size = round(MODEL_SIZE * lay.k / FRAGMENT_MODEL_SHARE)
    assert info.pattern_overlay is not None
    alpha = Image.open(io.BytesIO(info.pattern_overlay)).convert("RGBA").getchannel("A")
    alpha = alpha.resize((size, size), Image.Resampling.LANCZOS).point(lambda v: round(v * OVERLAY_OPACITY))
    color = tuple(int(min(255, v * 0.6 + 255 * 0.4 * 0.5)) for v in _rgb(info.center_color))
    tile = Image.new("RGBA", (size, size), (*color, 255))
    tile.putalpha(alpha)
    _paste_alpha(img, tile, round((lay.width - size) / 2), round(lay.y(MODEL_CY) - size / 2))


def _draw_text(img: Image.Image, info: GiftInfo, style: GiftStyle, lay: Layout, font_path: str | None) -> None:
    draw = ImageDraw.Draw(img)
    max_width = lay.width * 0.9
    if style.text == "custom":
        title, number, subtitle = style.custom_text, "", None
    else:
        title, number = info.title, format_number(info.number)
        subtitle = info.model.name if info.model else None

    title_font = _font("bold", round(TITLE_SIZE * lay.k), font_path)
    while True:
        gap = title_font.size * 0.3 if number else 0
        title_w = draw.textlength(title, font=title_font)
        number_w = draw.textlength(number, font=title_font) if number else 0
        if title_w + gap + number_w <= max_width or title_font.size <= 10:
            break
        title_font = _font("bold", int(title_font.size * 0.92), font_path)
    x = (lay.width - (title_w + gap + number_w)) / 2
    y = lay.y(TITLE_Y)
    draw.text((x, y), title, font=title_font, fill="#ffffff", anchor="lm")
    if number:
        draw.text((x + title_w + gap, y), number, font=title_font, fill=info.number_color, anchor="lm")
    if subtitle:
        sub_font = _font("regular", round(SUBTITLE_SIZE * lay.k), font_path)
        draw.text((lay.width / 2, lay.y(SUBTITLE_Y)), subtitle, font=sub_font, fill=info.number_color, anchor="mm")


def build_canvas(
    info: GiftInfo,
    pattern: bytes | None,
    style: GiftStyle,
    lay: Layout,
    with_backdrop: bool,
    font_path: str | None,
) -> tuple[Image.Image, int, int, int]:
    """Статичный фон кадра и место модели: (картинка, x0, y0, размер)."""
    with_backdrop = with_backdrop and info.has_backdrop
    if style.background:
        img = _custom_background(style.background, lay)
    elif with_backdrop:
        img = _gradient(info, lay)
        if pattern and info.pattern:
            _draw_pattern(img, info, pattern, lay)
        elif info.pattern_overlay:
            _draw_overlay(img, info, lay)
    else:
        img = Image.new("RGBA", (lay.width, lay.height), NO_BACKDROP_COLOR)

    framed = with_backdrop or style.background is not None
    show_text = style.text == "custom" or (style.text == "default" and framed)
    if show_text:
        _draw_text(img, info, style, lay, font_path)
        size, cy = MODEL_SIZE * lay.k, lay.y(MODEL_CY)
    elif framed:
        size, cy = NO_TEXT_MODEL * lay.k, lay.y(CANVAS_H / 2)
    else:
        size, cy = min(lay.width, lay.height) * NO_BACKDROP_MODEL, lay.height / 2
    size = int(size) // 2 * 2
    return img.convert("RGB"), int((lay.width - size) / 2), int(cy - size / 2), size


def render_gift(
    info: GiftInfo,
    model_tgs: bytes,
    pattern: bytes | None,
    out_path: Path,
    scale: int = 3,
    with_backdrop: bool = True,
    font_path: str | None = None,
    style: GiftStyle | None = None,
) -> RenderResult:
    style = style or GiftStyle()
    lay = Layout.make(style.aspect, scale)
    background, x0, y0, size = build_canvas(info, pattern, style, lay, with_backdrop, font_path)
    w, h = lay.width, lay.height
    bg = np.asarray(background, dtype=np.uint8).copy()

    # модель может выходить за край холста — работаем с видимой частью
    vx0, vy0, vx1, vy1 = max(0, x0), max(0, y0), min(w, x0 + size), min(h, y0 + size)
    region = bg[vy0:vy1, vx0:vx1].astype(np.float32)

    anim = LottieAnimation.from_data(gzip.decompress(model_tgs).decode("utf-8"))
    total = anim.lottie_animation_get_totalframe()
    rate = anim.lottie_animation_get_framerate() or FPS
    frames_out = max(1, round(total / rate * FPS))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{w}x{h}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None and proc.stderr is not None  # оба созданы через PIPE
    stdin, stderr = proc.stdin, proc.stderr
    try:
        last_index, composed = -1, np.empty(0, dtype=np.uint8)
        for k in range(frames_out):
            index = min(total - 1, int(k * rate / FPS))
            if index != last_index:
                model = np.asarray(anim.render_pillow_frame(index, width=size, height=size), dtype=np.float32)
                model = model[vy0 - y0 : vy1 - y0, vx0 - x0 : vx1 - x0]
                # rlottie отдаёт premultiplied alpha: out = src + dst * (1 - a)
                alpha = model[..., 3:4] / 255.0
                composed = np.clip(model[..., :3] + region * (1 - alpha), 0, 255).astype(np.uint8)
                last_index = index
            bg[vy0:vy1, vx0:vx1] = composed
            stdin.write(bg.tobytes())
        stdin.close()
        error = stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed: {error}")
    finally:
        if proc.poll() is None:
            proc.kill()
        anim.lottie_animation_destroy()
    return RenderResult(out_path, w, h, max(1, round(frames_out / FPS)))


def make_gif(mp4_path: Path, gif_path: Path, width: int = 630, fps: int = 50) -> Path:
    """GIF не умеет 60 fps (задержка кадра кратна 1/100 с), поэтому 50 fps."""
    vf = (
        f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle"
    )
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(mp4_path),
        "-vf",
        vf,
        "-loop",
        "0",
        str(gif_path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode('utf-8', 'replace')}")
    return gif_path
