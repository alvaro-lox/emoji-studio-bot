"""Премиум-эмодзи из картинки или видео: PNG 100×100 или WEBM VP9 100×100 (≤3 с, ≤256 КБ)."""

import io
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw

EMOJI_SIZE = 100
VIDEO_LIMIT = 256 * 1024
MARKER = (255, 0, 254)  # временная заливка фона, в реальных картинках почти не встречается


def first_emoji(text: str | None) -> str | None:
    """Первый эмодзи из подписи (с модификаторами и ZWJ-последовательностями)."""
    if not text:
        return None
    chars = list(text)
    for i, ch in enumerate(chars):
        if unicodedata.category(ch) == "So" or 0x1F000 <= ord(ch) <= 0x1FAFF:
            out = ch
            j = i + 1
            while j < len(chars) and (chars[j] in "️‍" or 0x1F3FB <= ord(chars[j]) <= 0x1F3FF or (chars[j - 1] == "‍")):
                out += chars[j]
                j += 1
            return out
    return None


def remove_background(image: Image.Image, tolerance: int = 40) -> Image.Image:
    """Убирает однотонный фон: заливка от краёв по похожему цвету.

    Хорошо работает на картинках с ровным фоном (скриншоты, логотипы, клипарт)."""
    rgba = image.convert("RGBA")
    rgb = rgba.convert("RGB")
    w, h = rgb.size
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    for xy in seeds:
        if rgba.getchannel("A").getpixel(xy) and rgb.getpixel(xy) != MARKER:
            ImageDraw.floodfill(rgb, xy, MARKER, thresh=tolerance)
    mask = np.all(np.asarray(rgb) == MARKER, axis=-1)
    data = np.asarray(rgba).copy()
    data[mask, 3] = 0
    return Image.fromarray(data, "RGBA")


def fit(image: Image.Image, size: int = EMOJI_SIZE, margin: int = 2) -> Image.Image:
    """Обрезает прозрачные поля и вписывает в квадрат size×size."""
    image = image.convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if bbox:
        image = image.crop(bbox)
    inner = size - margin * 2
    image.thumbnail((inner, inner), Image.Resampling.LANCZOS)
    if image.width < inner and image.height < inner:  # маленькие картинки растягиваем
        ratio = inner / max(image.width, image.height)
        new_size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2), image)
    return canvas


def make_static(data: bytes, remove_bg: bool) -> bytes:
    source = Image.open(io.BytesIO(data))
    source.thumbnail((640, 640), Image.Resampling.LANCZOS)
    image = source.convert("RGBA")
    if remove_bg:
        image = remove_background(image)
    buf = io.BytesIO()
    fit(image).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def make_video(data: bytes, suffix: str = ".mp4") -> bytes:
    """WEBM VP9 с прозрачностью: подбираем качество, пока файл не влезет в 256 КБ."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / f"in{suffix}", Path(tmp) / "out.webm"
        src.write_bytes(data)
        vf = (
            f"fps=30,scale={EMOJI_SIZE}:{EMOJI_SIZE}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={EMOJI_SIZE}:{EMOJI_SIZE}:(ow-iw)/2:(oh-ih)/2:color=black@0,format=yuva420p"
        )
        for crf in (30, 38, 45, 52, 60):
            cmd = [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-t",
                "2.9",
                "-vf",
                vf,
                "-c:v",
                "libvpx-vp9",
                "-pix_fmt",
                "yuva420p",
                "-b:v",
                "0",
                "-crf",
                str(crf),
                "-deadline",
                "good",
                "-row-mt",
                "1",
                "-an",
                str(out),
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode("utf-8", "replace"))
            if out.stat().st_size <= VIDEO_LIMIT:
                return out.read_bytes()
        raise ValueError("video too big")


def video_frame(webm: bytes) -> Image.Image:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "e.webm"
        src.write_bytes(webm)
        png = subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-loglevel",
                "error",
                "-c:v",
                "libvpx-vp9",
                "-i",
                str(src),
                "-ss",
                "0.5",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            ],
            capture_output=True,
            timeout=30,
        ).stdout
    return Image.open(io.BytesIO(png)).convert("RGBA")


def preview(emoji: Image.Image, size: int = 360) -> bytes:
    """Эмодзи крупно на шахматке, чтобы было видно прозрачность."""
    cell = size // 12
    board = Image.new("RGB", (size, size), (44, 56, 70))
    draw = ImageDraw.Draw(board)
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            if (x // cell + y // cell) % 2:
                draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(36, 47, 60))
    big = emoji.resize((size - 40, size - 40), Image.Resampling.LANCZOS)
    board.paste(big, (20, 20), big)
    buf = io.BytesIO()
    board.save(buf, "PNG")
    return buf.getvalue()
