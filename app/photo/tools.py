"""Обработка фото в полном разрешении: удаление фона и улучшение качества (см. enhance.py).

Фон убирает нейросеть (rembg, модель ISNet). Она считает маску на уменьшенной копии,
а маска потом растягивается до исходного размера — сами пиксели фото не пережимаются.
Если rembg не установлен, работает простой вариант: заливка однотонного фона от краёв.
"""

import io
import logging
import threading
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

log = logging.getLogger(__name__)

MAX_SIDE = 4096  # больше Telegram всё равно не покажет, а память не резиновая
BACKGROUND_MODEL = "isnet-general-use"
MARKER = (255, 0, 254)  # временная заливка фона в простом режиме

_session_lock = threading.Lock()


def open_image(data: bytes) -> Image.Image:
    """Картинка с учётом поворота из EXIF, не больше MAX_SIDE по большей стороне."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    return image


def to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=False, compress_level=6)
    return buffer.getvalue()


@lru_cache(maxsize=1)
def _rembg_session():
    """Сессия нейросети. None — если rembg не установлен. Модель скачается при первом вызове."""
    try:
        from rembg import new_session
    except ImportError:
        log.warning("rembg не установлен — фон убирается простым способом")
        return None
    return new_session(BACKGROUND_MODEL)


def has_neural_cutout() -> bool:
    with _session_lock:
        return _rembg_session() is not None


def _neural_mask(image: Image.Image) -> Image.Image | None:
    with _session_lock:
        session = _rembg_session()
    if session is None:
        return None
    from rembg import remove

    mask = remove(image.convert("RGB"), session=session, only_mask=True, post_process_mask=True)
    return mask.convert("L").resize(image.size, Image.Resampling.LANCZOS)


def _flat_background_mask(image: Image.Image, tolerance: int = 40) -> Image.Image:
    """Маска для картинок с однотонным фоном: заливка от краёв по похожему цвету."""
    rgba = image.convert("RGBA")
    rgb = rgba.convert("RGB")
    width, height = rgb.size
    seeds = [
        (0, 0),
        (width - 1, 0),
        (0, height - 1),
        (width - 1, height - 1),
        (width // 2, 0),
        (width // 2, height - 1),
        (0, height // 2),
        (width - 1, height // 2),
    ]
    alpha = rgba.getchannel("A")
    for point in seeds:
        if alpha.getpixel(point) and rgb.getpixel(point) != MARKER:
            ImageDraw.floodfill(rgb, point, MARKER, thresh=tolerance)
    background = np.all(np.asarray(rgb) == MARKER, axis=-1)
    mask = np.where(background, 0, np.asarray(alpha)).astype(np.uint8)
    # сглаживаем ступеньки по краю
    return Image.fromarray(mask, "L").filter(ImageFilter.GaussianBlur(0.6))


def cut_out(image: Image.Image) -> Image.Image:
    """Объект без фона (RGBA) в исходном разрешении."""
    mask = _neural_mask(image)
    if mask is None:
        mask = _flat_background_mask(image)
    elif "A" in image.getbands():  # у PNG могла быть своя прозрачность — сохраняем её
        mask = Image.fromarray(np.minimum(np.asarray(mask), np.asarray(image.getchannel("A"))), "L")
    result = image.convert("RGBA")
    result.putalpha(mask)
    return result


def crop_to_content(image: Image.Image, padding: int = 8) -> Image.Image:
    bbox = image.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    if not bbox:
        return image
    left, top, right, bottom = bbox
    return image.crop(
        (
            max(0, left - padding),
            max(0, top - padding),
            min(image.width, right + padding),
            min(image.height, bottom + padding),
        )
    )


def remove_background(data: bytes) -> bytes:
    return to_png(crop_to_content(cut_out(open_image(data))))


@dataclass
class Enhanced:
    data: bytes
    extension: str  # "jpg" — обычное фото, "png" — если была прозрачность
    size: tuple[int, int]
    faces: int


def classic_enhance(image: Image.Image) -> Image.Image:
    """Запасной вариант без нейросетей: ×2, мягкое шумоподавление, резкость, контраст и цвет."""
    alpha = image.getchannel("A") if "A" in image.getbands() else None
    rgb = image.convert("RGB")
    if max(rgb.size) * 2 <= MAX_SIDE:
        rgb = rgb.resize((rgb.width * 2, rgb.height * 2), Image.Resampling.LANCZOS)
    rgb = Image.blend(rgb, rgb.filter(ImageFilter.MedianFilter(3)), 0.35)
    rgb = rgb.filter(ImageFilter.UnsharpMask(radius=3, percent=110, threshold=2))
    rgb = ImageOps.autocontrast(rgb, cutoff=0.3, preserve_tone=True)
    rgb = ImageEnhance.Contrast(ImageEnhance.Color(rgb).enhance(1.08)).enhance(1.04)
    if alpha is not None:
        rgb = rgb.convert("RGBA")
        rgb.putalpha(alpha.resize(rgb.size, Image.Resampling.LANCZOS))
    return rgb


def enhance(data: bytes) -> Enhanced:
    """Нейросети (Real-ESRGAN ×4 + GFPGAN для лиц); если их нет — классическая обработка."""
    image = open_image(data)
    faces = 0
    try:
        from app.photo.enhance import enhance_image

        result = enhance_image(image)
        image, faces = result.image, result.faces
    except Exception:
        log.exception("Нейросетевое улучшение не сработало — делаю обычное")
        image = classic_enhance(image)

    buffer = io.BytesIO()
    if "A" in image.getbands():
        image.save(buffer, "PNG", compress_level=6)
        extension = "png"
    else:  # фото без прозрачности: JPEG 95 в разы меньше PNG и на глаз не отличается
        image.save(buffer, "JPEG", quality=95, subsampling=0, optimize=True)
        extension = "jpg"
    return Enhanced(buffer.getvalue(), extension, image.size, faces)


def checkerboard_preview(image_png: bytes, max_side: int = 1280) -> bytes:
    """Превью прозрачной картинки на шахматке — в чате видно, где фон убран."""
    image = Image.open(io.BytesIO(image_png)).convert("RGBA")
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    cell = max(8, max(image.size) // 40)
    board = Image.new("RGB", image.size, (44, 56, 70))
    draw = ImageDraw.Draw(board)
    for top in range(0, image.height, cell):
        for left in range(0, image.width, cell):
            if (left // cell + top // cell) % 2:
                draw.rectangle((left, top, left + cell - 1, top + cell - 1), fill=(36, 47, 60))
    board.paste(image, (0, 0), image)
    buffer = io.BytesIO()
    board.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()
