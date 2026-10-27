"""Перекраска стикеров: .tgs (Lottie) правкой цветов в JSON, .webp — через HLS-тонирование.

Тонирование сохраняет светлоту каждого цвета и меняет оттенок/насыщенность на целевые,
поэтому тени, блики и объём рисунка остаются, а сам рисунок становится нужного цвета.
"""

import colorsys
import gzip
import io
import json
import re

import numpy as np
from PIL import Image

# ключ: (hex, эмодзи для кнопки, стиль кнопки)
PRESETS: dict[str, tuple[str, str, str | None]] = {
    "gold": ("#E8B923", "🟡", None),
    "silver": ("#C0C6CC", "⚪", None),
    "red": ("#E53935", "🔴", "danger"),
    "orange": ("#FB8C00", "🟠", None),
    "green": ("#43A047", "🟢", "success"),
    "mint": ("#26C6DA", "🩵", None),
    "blue": ("#1E88E5", "🔵", "primary"),
    "purple": ("#8E24AA", "🟣", None),
    "pink": ("#EC407A", "🩷", None),
    "mono": ("#808080", "⚫", None),
}

HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_hex(text: str) -> str | None:
    m = HEX_RE.match(text.strip())
    return f"#{m.group(1).upper()}" if m else None


def hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255


class Tint:
    """Функция цвет → цвет для одного целевого оттенка."""

    def __init__(self, hex_color: str):
        r, g, b = hex_to_rgb(hex_color)
        self.hue, self.light, self.sat = colorsys.rgb_to_hls(r, g, b)

    def __call__(self, r: float, g: float, b: float) -> tuple[float, float, float]:
        _, light, sat = colorsys.rgb_to_hls(r, g, b)
        # серые участки тоже окрашиваем, но чуть слабее насыщенных
        new_sat = self.sat * (0.55 + 0.45 * sat)
        return colorsys.hls_to_rgb(self.hue, light, new_sat)


def _tint_list(values: list, tint: Tint, offset: int = 0) -> list:
    rgb = values[offset : offset + 3]
    if len(rgb) < 3 or not all(isinstance(v, (int, float)) for v in rgb):
        return values
    scale = 255.0 if max(rgb) > 1 else 1.0  # старые файлы хранят 0..255
    new = tint(*(v / scale for v in rgb))
    values = list(values)
    values[offset : offset + 3] = [round(v * scale, 4) for v in new]
    return values


def _recolor_value(prop: dict, fn) -> None:
    """prop — анимируемое свойство Lottie: {"a":0,"k":значение} или {"a":1,"k":[кейфреймы]}."""
    k = prop.get("k")
    if prop.get("a") and isinstance(k, list):
        for frame in k:
            if isinstance(frame, dict):
                for key in ("s", "e"):
                    if isinstance(frame.get(key), list):
                        frame[key] = fn(frame[key])
    elif isinstance(k, list):
        prop["k"] = fn(k)


def recolor_lottie(data, tint: Tint) -> None:
    """Меняет цвета заливок, обводок, градиентов и solid-слоёв на месте."""
    if isinstance(data, dict):
        kind = data.get("ty")
        if kind in ("fl", "st") and isinstance(data.get("c"), dict):
            _recolor_value(data["c"], lambda v: _tint_list(v, tint))
        elif kind in ("gf", "gs") and isinstance(data.get("g"), dict):
            stops = int(data["g"].get("p") or 0)

            def gradient(values: list) -> list:
                for i in range(stops):
                    values = _tint_list(values, tint, i * 4 + 1)
                return values

            if isinstance(data["g"].get("k"), dict):
                _recolor_value(data["g"]["k"], gradient)
        if kind == 1 and isinstance(data.get("sc"), str) and data["sc"].startswith("#") and len(data["sc"]) == 7:
            r, g, b = tint(*hex_to_rgb(data["sc"]))
            data["sc"] = f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"
        for value in data.values():
            if isinstance(value, (dict, list)):
                recolor_lottie(value, tint)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                recolor_lottie(item, tint)


def recolor_tgs(tgs: bytes, hex_color: str) -> bytes:
    data = json.loads(gzip.decompress(tgs))
    recolor_lottie(data, Tint(hex_color))
    return gzip.compress(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode(), compresslevel=9)


def recolor_image(image_bytes: bytes, hex_color: str, fmt: str = "WEBP") -> bytes:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    rgba = np.asarray(image, dtype=np.float32) / 255
    rgb = rgba[..., :3]
    # HLS-тонирование в numpy: светлота сохраняется, оттенок и насыщенность — целевые
    cmax, cmin = rgb.max(-1), rgb.min(-1)
    light = (cmax + cmin) / 2
    delta = cmax - cmin
    sat = np.where(delta == 0, 0, delta / np.maximum(1e-6, 1 - np.abs(2 * light - 1)))
    tint = Tint(hex_color)
    new_sat = tint.sat * (0.55 + 0.45 * np.clip(sat, 0, 1))
    chroma = (1 - np.abs(2 * light - 1)) * new_sat
    h6 = tint.hue * 6
    x = chroma * (1 - abs(h6 % 2 - 1))
    sector = int(h6) % 6
    zero = np.zeros_like(chroma)
    r, g, b = [
        (chroma, x, zero),
        (x, chroma, zero),
        (zero, chroma, x),
        (zero, x, chroma),
        (x, zero, chroma),
        (chroma, zero, x),
    ][sector]
    m = light - chroma / 2
    out = np.stack([r + m, g + m, b + m, rgba[..., 3]], -1)
    result = Image.fromarray(np.clip(out * 255 + 0.5, 0, 255).astype(np.uint8), "RGBA")
    buf = io.BytesIO()
    result.save(buf, fmt, lossless=True) if fmt == "WEBP" else result.save(buf, fmt)
    return buf.getvalue()
