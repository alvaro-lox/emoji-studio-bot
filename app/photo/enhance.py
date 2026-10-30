"""Улучшение фото нейросетями: Real-ESRGAN general x4v3 (детали, шум, артефакты JPEG) + GFPGAN v1.4 (лица).

Обе модели — ONNX, работают на CPU через onnxruntime и скачиваются при первом вызове
в DATA_DIR/models. Real-ESRGAN считает картинку плитками с перекрытием — так память
не растёт с размером фото, а швов не видно. Лица находит YuNet (OpenCV), каждое
выравнивается по шаблону FFHQ 512×512, восстанавливается GFPGAN и вклеивается обратно
с мягкой маской.
"""

import logging
import os
import threading
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelFile:
    name: str
    url: str
    min_size: int  # меньше — значит скачалось не до конца


# general-x4v3 в 15 раз быстрее x4plus на CPU, а на фото почти не уступает; лица всё равно делает GFPGAN
UPSCALER = ModelFile(
    "realesr-general-x4v3.onnx",
    "https://huggingface.co/jonathanst29/tinier-upscale-models/resolve/main/realesr-general-x4v3.onnx",
    4_000_000,
)
FACE_RESTORER = ModelFile(
    "GFPGANv1.4.onnx",
    "https://huggingface.co/netrunner-exe/Face-Upscalers-onnx/resolve/main/GFPGANv1.4.onnx",
    300_000_000,
)
FACE_DETECTOR = ModelFile(
    "face_detection_yunet_2023mar.onnx",
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    200_000,
)

MAX_INPUT_SIDE = 1024  # ×4 → до 4096 px
MAX_OUTPUT_SIDE = 4096
TILE, TILE_OVERLAP = 256, 16
FACE_SIZE = 512
MIN_FACE = 24  # px в исходнике: меньше — это не лицо, а шум
FACE_SCORE = 0.7

# Опорные точки лица в шаблоне FFHQ 512×512: глаза, нос, уголки рта
FFHQ_POINTS = np.array(
    [[192.98, 239.95], [318.90, 240.19], [256.63, 314.02], [201.26, 371.41], [313.09, 371.15]],
    dtype=np.float32,
)

_download_lock = threading.Lock()


def models_dir() -> Path:
    return Path(os.getenv("DATA_DIR") or "data") / "models"


def model_path(model: ModelFile) -> Path:
    """Путь к модели; если её нет — скачиваем (один раз, атомарно через .part)."""
    path = models_dir() / model.name
    with _download_lock:
        if path.exists() and path.stat().st_size >= model.min_size:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        log.info("Скачиваю модель %s", model.name)
        urllib.request.urlretrieve(model.url, partial)
        if partial.stat().st_size < model.min_size:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"model {model.name} downloaded incompletely")
        partial.replace(path)
    return path


@lru_cache(maxsize=2)
def _session(name: str):
    import onnxruntime

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
    model = {UPSCALER.name: UPSCALER, FACE_RESTORER.name: FACE_RESTORER}[name]
    return onnxruntime.InferenceSession(str(model_path(model)), options, providers=["CPUExecutionProvider"])


def warm_up() -> None:
    """Скачать и загрузить модели заранее, чтобы первый пользователь не ждал."""
    try:
        _session(UPSCALER.name)
        _session(FACE_RESTORER.name)
        model_path(FACE_DETECTOR)
    except Exception:  # без сети модели скачаются при первом запросе, а до тех пор — обычная обработка
        log.exception("Не удалось подготовить модели улучшения фото")


# ---------- Real-ESRGAN ----------


def upscale_x4(rgb: np.ndarray) -> np.ndarray:
    """RGB uint8 (H, W, 3) → RGB uint8 (4H, 4W, 3). Плитки с перекрытием, края плиток отбрасываются."""
    session = _session(UPSCALER.name)
    height, width = rgb.shape[:2]
    source = rgb.astype(np.float32) / 255
    result = np.zeros((height * 4, width * 4, 3), dtype=np.float32)

    for top in range(0, height, TILE):
        for left in range(0, width, TILE):
            # плитка с запасом по краям, чтобы у модели был контекст
            pad_top, pad_left = max(0, top - TILE_OVERLAP), max(0, left - TILE_OVERLAP)
            bottom, right = min(height, top + TILE), min(width, left + TILE)
            pad_bottom, pad_right = min(height, bottom + TILE_OVERLAP), min(width, right + TILE_OVERLAP)

            tile = source[pad_top:pad_bottom, pad_left:pad_right].transpose(2, 0, 1)[None]
            output = session.run(None, {"input": tile})[0][0].transpose(1, 2, 0)

            crop_top, crop_left = (top - pad_top) * 4, (left - pad_left) * 4
            crop = output[crop_top : crop_top + (bottom - top) * 4, crop_left : crop_left + (right - left) * 4]
            result[top * 4 : bottom * 4, left * 4 : right * 4] = crop
    return np.clip(result * 255 + 0.5, 0, 255).astype(np.uint8)


# ---------- лица ----------


def detect_faces(rgb: np.ndarray) -> list[np.ndarray]:
    """5 опорных точек каждого найденного лица (глаза, нос, уголки рта) в координатах картинки."""
    import cv2

    height, width = rgb.shape[:2]
    detector = cv2.FaceDetectorYN.create(str(model_path(FACE_DETECTOR)), "", (width, height), FACE_SCORE)
    _, faces = detector.detect(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if faces is None:
        return []
    return [face[4:14].reshape(5, 2) for face in faces if min(face[2], face[3]) >= MIN_FACE]


def restore_face(aligned: np.ndarray) -> np.ndarray:
    """Лицо 512×512 RGB uint8 → восстановленное лицо того же размера."""
    session = _session(FACE_RESTORER.name)
    tensor = (aligned.astype(np.float32) / 255 - 0.5) / 0.5
    output = session.run(None, {"input": tensor.transpose(2, 0, 1)[None]})[0][0].transpose(1, 2, 0)
    return np.clip((output * 0.5 + 0.5) * 255 + 0.5, 0, 255).astype(np.uint8)


def _face_mask() -> np.ndarray:
    """Мягкая маска в координатах шаблона: середина 1, к краям плавно 0 — шва не видно."""
    import cv2

    mask = np.zeros((FACE_SIZE, FACE_SIZE), dtype=np.float32)
    border = FACE_SIZE // 10
    mask[border:-border, border:-border] = 1
    return cv2.GaussianBlur(mask, (0, 0), FACE_SIZE / 20)


def restore_faces(image: np.ndarray, faces: list[np.ndarray]) -> np.ndarray:
    import cv2

    if not faces:
        return image
    height, width = image.shape[:2]
    result = image.astype(np.float32)
    mask = _face_mask()
    for points in faces:
        transform, _ = cv2.estimateAffinePartial2D(points.astype(np.float32), FFHQ_POINTS, method=cv2.LMEDS)
        if transform is None:
            continue
        aligned = cv2.warpAffine(image, transform, (FACE_SIZE, FACE_SIZE), borderMode=cv2.BORDER_REFLECT)
        restored = restore_face(aligned).astype(np.float32)
        inverse = cv2.invertAffineTransform(transform)
        back = cv2.warpAffine(restored, inverse, (width, height), flags=cv2.INTER_LANCZOS4)
        weight = cv2.warpAffine(mask, inverse, (width, height))[..., None]
        result = back * weight + result * (1 - weight)
    return np.clip(result + 0.5, 0, 255).astype(np.uint8)


# ---------- всё вместе ----------


@dataclass
class EnhanceResult:
    image: Image.Image
    faces: int


def enhance_image(image: Image.Image) -> EnhanceResult:
    """Апскейл ×4 нейросетью + восстановление лиц. Прозрачность (если была) растягивается отдельно."""
    alpha = image.getchannel("A") if "A" in image.getbands() else None
    rgb = image.convert("RGB")
    if max(rgb.size) > MAX_INPUT_SIDE:
        rgb.thumbnail((MAX_INPUT_SIDE, MAX_INPUT_SIDE), Image.Resampling.LANCZOS)

    pixels = np.asarray(rgb)
    upscaled = upscale_x4(pixels)
    # лица ищем на исходнике (быстрее), точки переводим в масштаб ×4
    faces = [points * 4 for points in detect_faces(pixels)]
    upscaled = restore_faces(upscaled, faces)

    result = Image.fromarray(upscaled, "RGB")
    if max(result.size) > MAX_OUTPUT_SIDE:
        result.thumbnail((MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE), Image.Resampling.LANCZOS)
    if alpha is not None:
        result = result.convert("RGBA")
        result.putalpha(alpha.resize(result.size, Image.Resampling.LANCZOS))
    return EnhanceResult(result, len(faces))
