"""Рендер подарка в MP4 60 fps (в Telegram он показывается как GIF) и в .gif-файл.

Раскладка повторяет превью подарка Telegram: блок 420×280 с радиальным градиентом,
узором из символа, моделью по центру и подписью снизу. Блок масштабируется под
формат холста (3:2, 1:1, 9:16); вместо градиента можно подложить свою картинку.

Модель — анимация Lottie. rlottie отдаёт кадры с premultiplied alpha, поэтому кадр
накладывается как out = src + dst · (1 − α), а не по «учебной» формуле.
"""

import glob
import gzip
import hashlib
import io
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import IO, cast

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from rlottie_python import LottieAnimation

from app.nft.parser import CANVAS_H, CANVAS_W, GiftInfo

FPS = 60
NO_BACKDROP_COLOR = "#17212b"  # фон тёмной темы Telegram
GRADIENT_RADIUS = 0.6965  # r="69.65%" из SVG страницы, в долях высоты блока

# В единицах блока 420×280
MODEL_SIZE, MODEL_CENTER_Y = 160, 110
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


# ---------- шрифты и цвета ----------


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


def load_font(kind: str, size: int, override: str | None = None) -> ImageFont.FreeTypeFont:
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


def hex_rgb(hex_color: str | None) -> np.ndarray:
    digits = (hex_color or NO_BACKDROP_COLOR).lstrip("#")
    return np.array([int(digits[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def format_number(number: int | None) -> str:
    return f"#{number:,}".replace(",", " ") if number is not None else ""


def paste_alpha(base: Image.Image, tile: Image.Image, x: int, y: int) -> None:
    """alpha_composite, который понимает отрицательные координаты."""
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(tile.width, base.width - x), min(tile.height, base.height - y)
    if right > left and bottom > top:
        base.alpha_composite(tile.crop((left, top, right, bottom)), (x + left, y + top))


# ---------- фон подарка ----------


@dataclass
class Layout:
    """Где на холсте лежит блок 420×280 и во сколько раз он увеличен."""

    width: int
    height: int
    block_scale: float
    block_left: float
    block_top: float

    @classmethod
    def make(cls, aspect: str, scale: int) -> "Layout":
        width, height = (side * scale for side in ASPECTS.get(aspect, ASPECTS["3:2"]))
        # для вертикального холста блок шире экрана: модель крупнее, узор обрезается по бокам
        block_scale = max(width / CANVAS_W, height / CANVAS_H * 0.55)
        return cls.centered(width, height, block_scale)

    @classmethod
    def centered(cls, width: int, height: int, block_scale: float) -> "Layout":
        left = (width - CANVAS_W * block_scale) / 2
        top = (height - CANVAS_H * block_scale) / 2
        return cls(width, height, block_scale, left, top)

    def x(self, value: float) -> float:
        return self.block_left + value * self.block_scale

    def y(self, value: float) -> float:
        return self.block_top + value * self.block_scale


def radial_gradient(info: GiftInfo, layout: Layout) -> Image.Image:
    rows, cols = np.mgrid[0 : layout.height, 0 : layout.width].astype(np.float32)
    radius = GRADIENT_RADIUS * max(CANVAS_H * layout.block_scale, 0.8 * layout.height)
    distance = np.hypot(cols - layout.width / 2, rows - layout.y(CANVAS_H / 2))
    position = np.clip(distance / radius, 0, 1)[..., None]
    center, edge = hex_rgb(info.center_color), hex_rgb(info.edge_color)
    pixels = center + (edge - center) * position
    pixels += np.random.default_rng(0).uniform(-0.5, 0.5, pixels.shape)  # дизеринг против полос
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _custom_background(data: bytes, layout: Layout) -> Image.Image:
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    image = ImageOps.fit(image, (layout.width, layout.height), Image.Resampling.LANCZOS)
    # затемняем низ, чтобы подпись читалась на любой картинке
    shade = np.linspace(0, 1, layout.height, dtype=np.float32)[:, None, None] ** 2 * 0.55 + 0.12
    pixels = np.asarray(image, dtype=np.float32) * (1 - shade)
    return Image.fromarray(pixels.astype(np.uint8), "RGB").convert("RGBA")


def draw_pattern(image: Image.Image, info: GiftInfo, pattern: bytes, layout: Layout) -> None:
    """Узор со страницы подарка: одна плитка-символ, разложенная с разным масштабом и прозрачностью."""
    symbol = Image.open(io.BytesIO(pattern)).convert("RGBA").getchannel("A")
    color = tuple(int(v) for v in hex_rgb(info.pattern_color or "#ffffff"))
    for item in info.pattern:
        size = max(1, round(100 * item.scale * layout.block_scale))
        alpha = symbol.resize((size, size), Image.Resampling.LANCZOS)
        alpha = alpha.point(lambda value, opacity=item.opacity: round(value * opacity))
        tile = Image.new("RGBA", (size, size), (*color, 255))
        tile.putalpha(alpha)
        paste_alpha(image, tile, round(layout.x(item.x)), round(layout.y(item.y)))


FRAGMENT_MODEL_SHARE = 0.63  # модель в карточке Fragment занимает 63% ширины
OVERLAY_OPACITY = 0.3


def draw_fragment_pattern(image: Image.Image, info: GiftInfo, layout: Layout) -> None:
    """Узор из карточки Fragment: масштаб как у модели, цвет — светлее центра фона, полупрозрачный."""
    assert info.pattern_overlay is not None
    size = round(MODEL_SIZE * layout.block_scale / FRAGMENT_MODEL_SHARE)
    alpha = Image.open(io.BytesIO(info.pattern_overlay)).convert("RGBA").getchannel("A")
    alpha = alpha.resize((size, size), Image.Resampling.LANCZOS).point(lambda v: round(v * OVERLAY_OPACITY))
    color = tuple(int(min(255, v * 0.6 + 255 * 0.4 * 0.5)) for v in hex_rgb(info.center_color))
    tile = Image.new("RGBA", (size, size), (*color, 255))
    tile.putalpha(alpha)
    paste_alpha(image, tile, round((layout.width - size) / 2), round(layout.y(MODEL_CENTER_Y) - size / 2))


def backdrop_image(info: GiftInfo, pattern: bytes | None, layout: Layout) -> Image.Image:
    """Фирменный фон подарка: радиальный градиент и узор. RGBA размера layout."""
    image = radial_gradient(info, layout)
    if pattern and info.pattern:
        draw_pattern(image, info, pattern, layout)
    elif info.pattern_overlay:
        draw_fragment_pattern(image, info, layout)
    return image


def _draw_caption(image: Image.Image, info: GiftInfo, style: GiftStyle, layout: Layout, font_path: str | None) -> None:
    draw = ImageDraw.Draw(image)
    max_width = layout.width * 0.9
    if style.text == "custom":
        title, number, subtitle = style.custom_text, "", None
    else:
        title, number = info.title, format_number(info.number)
        subtitle = info.model.name if info.model else None

    title_font = load_font("bold", round(TITLE_SIZE * layout.block_scale), font_path)
    while True:
        gap = title_font.size * 0.3 if number else 0
        title_width = draw.textlength(title, font=title_font)
        number_width = draw.textlength(number, font=title_font) if number else 0
        if title_width + gap + number_width <= max_width or title_font.size <= 10:
            break
        title_font = load_font("bold", int(title_font.size * 0.92), font_path)
    left = (layout.width - (title_width + gap + number_width)) / 2
    baseline = layout.y(TITLE_Y)
    draw.text((left, baseline), title, font=title_font, fill="#ffffff", anchor="lm")
    if number:
        draw.text((left + title_width + gap, baseline), number, font=title_font, fill=info.number_color, anchor="lm")
    if subtitle:
        subtitle_font = load_font("regular", round(SUBTITLE_SIZE * layout.block_scale), font_path)
        position = (layout.width / 2, layout.y(SUBTITLE_Y))
        draw.text(position, subtitle, font=subtitle_font, fill=info.number_color, anchor="mm")


@dataclass
class ModelSpot:
    """Где рисовать модель: левый верхний угол и сторона квадрата."""

    left: int
    top: int
    size: int


def build_canvas(
    info: GiftInfo,
    pattern: bytes | None,
    style: GiftStyle,
    layout: Layout,
    with_backdrop: bool,
    font_path: str | None,
) -> tuple[Image.Image, ModelSpot]:
    """Статичная часть кадра (всё, кроме модели) и место под модель."""
    with_backdrop = with_backdrop and info.has_backdrop
    if style.background:
        image = _custom_background(style.background, layout)
    elif with_backdrop:
        image = backdrop_image(info, pattern, layout)
    else:
        image = Image.new("RGBA", (layout.width, layout.height), NO_BACKDROP_COLOR)

    framed = with_backdrop or style.background is not None
    show_text = style.text == "custom" or (style.text == "default" and framed)
    if show_text:
        _draw_caption(image, info, style, layout, font_path)
        size, center_y = MODEL_SIZE * layout.block_scale, layout.y(MODEL_CENTER_Y)
    elif framed:
        size, center_y = NO_TEXT_MODEL * layout.block_scale, layout.y(CANVAS_H / 2)
    else:
        size, center_y = min(layout.width, layout.height) * NO_BACKDROP_MODEL, layout.height / 2
    size = int(size) // 2 * 2
    spot = ModelSpot(int((layout.width - size) / 2), int(center_y - size / 2), size)
    return image.convert("RGB"), spot


# ---------- анимация модели и запись видео ----------


class ModelAnimation:
    """Кадры модели нужного размера, по кругу и с частотой 60 fps.

    Соседние кадры вывода часто совпадают с одним кадром Lottie — такой кадр не рендерим заново.
    """

    def __init__(self, model_tgs: bytes, size: int):
        self.size = size
        self._animation = LottieAnimation.from_data(gzip.decompress(model_tgs).decode("utf-8"))
        self.total_frames = max(1, self._animation.lottie_animation_get_totalframe())
        self.frame_rate = self._animation.lottie_animation_get_framerate() or FPS
        self._last_index = -1
        self._last_frame = np.empty(0, dtype=np.float32)

    @property
    def duration(self) -> float:
        return self.total_frames / self.frame_rate

    @property
    def output_frames(self) -> int:
        return max(1, round(self.duration * FPS))

    def frame(self, output_index: int) -> np.ndarray:
        """Premultiplied RGBA (float32) для кадра вывода; анимация повторяется по кругу."""
        index = int(output_index * self.frame_rate / FPS) % self.total_frames
        if index != self._last_index:
            rendered = self._animation.render_pillow_frame(index, width=self.size, height=self.size)
            self._last_frame = np.asarray(rendered, dtype=np.float32)
            self._last_index = index
        return self._last_frame

    def close(self) -> None:
        self._animation.lottie_animation_destroy()


def composite_premultiplied(model: np.ndarray, under: np.ndarray) -> np.ndarray:
    """model (premultiplied RGBA) поверх under (RGB) → uint8 RGB."""
    alpha = model[..., 3:4] * (1 / 255)
    return np.clip(model[..., :3] + under * (1 - alpha), 0, 255).astype(np.uint8)


@contextmanager
def video_writer(path: Path, width: int, height: int, crf: int = 18) -> Iterator[IO[bytes]]:
    """Пишем сырые RGB-кадры прямо в ffmpeg (без промежуточных файлов), на выходе H.264 60 fps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",  # почти тот же размер, что у fast, но кодирует заметно быстрее
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(path),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None and process.stderr is not None  # оба созданы через PIPE
    try:
        yield process.stdin
        process.stdin.close()
        error = process.stderr.read().decode("utf-8", "replace")
        if process.wait() != 0:
            raise RuntimeError(f"ffmpeg failed: {error}")
    finally:
        if process.poll() is None:
            process.kill()


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
    layout = Layout.make(style.aspect, scale)
    background, spot = build_canvas(info, pattern, style, layout, with_backdrop, font_path)
    width, height = layout.width, layout.height
    frame = np.asarray(background, dtype=np.uint8).copy()

    # модель может выходить за край холста — работаем с видимой частью
    left, top = max(0, spot.left), max(0, spot.top)
    right, bottom = min(width, spot.left + spot.size), min(height, spot.top + spot.size)
    under_model = frame[top:bottom, left:right].astype(np.float32)
    crop = (slice(top - spot.top, bottom - spot.top), slice(left - spot.left, right - spot.left))

    model = ModelAnimation(model_tgs, spot.size)
    try:
        with video_writer(out_path, width, height) as stream:
            for index in range(model.output_frames):
                frame[top:bottom, left:right] = composite_premultiplied(model.frame(index)[crop], under_model)
                stream.write(frame.tobytes())
    finally:
        model.close()
    return RenderResult(out_path, width, height, max(1, round(model.output_frames / FPS)))


def render_still(
    info: GiftInfo, model_tgs: bytes, pattern: bytes | None, scale: int = 3, with_backdrop: bool = True
) -> bytes:
    """Один кадр (середина анимации) в PNG — для архива «Скачать подарок»."""
    layout = Layout.make("3:2", scale)
    background, spot = build_canvas(info, pattern, GiftStyle(text="none"), layout, with_backdrop, None)
    image = background.convert("RGBA")
    model = ModelAnimation(model_tgs, spot.size)
    try:
        pixels = model.frame(model.output_frames // 2)
    finally:
        model.close()
    rendered = Image.frombytes("RGBa", (spot.size, spot.size), pixels.astype(np.uint8).tobytes()).convert("RGBA")
    paste_alpha(image, rendered, spot.left, spot.top)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def render_model_png(model_tgs: bytes, size: int = 1024) -> bytes:
    """Модель без фона, с прозрачностью."""
    model = ModelAnimation(model_tgs, size)
    try:
        pixels = model.frame(model.output_frames // 2)
    finally:
        model.close()
    image = Image.frombytes("RGBa", (size, size), pixels.astype(np.uint8).tobytes()).convert("RGBA")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def make_gif(mp4_path: Path, gif_path: Path, width: int = 630, fps: int = 50) -> Path:
    """GIF не умеет 60 fps (задержка кадра кратна 1/100 с), поэтому 50 fps."""
    filters = (
        f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle"
    )
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(mp4_path),
        "-vf",
        filters,
        "-loop",
        "0",
        str(gif_path),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode('utf-8', 'replace')}")
    return gif_path
