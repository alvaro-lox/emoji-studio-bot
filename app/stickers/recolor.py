"""Перекраска стикеров: .tgs (Lottie) правкой цветов в JSON, .webp — попиксельно.

Тонирование сохраняет светлоту каждого цвета и меняет оттенок/насыщенность на целевые,
поэтому тени, блики и объём рисунка остаются, а сам рисунок становится нужного цвета.
Градиент работает так же, только целевой цвет зависит от светлоты: тени берут первый
цвет, блики — второй (gradient map).

Цвет задаётся строкой: "#RRGGBB" или градиент "#RRGGBB-#RRGGBB".
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

# ключ: (градиент, эмодзи для кнопки)
GRADIENTS: dict[str, tuple[str, str]] = {
    "sunset": ("#5B1A8C-#FF8A3D", "🌇"),
    "ocean": ("#0B3D91-#4FE3E8", "🌊"),
    "neon": ("#3A0CA3-#F72585", "💜"),
    "fire": ("#8B0000-#FFD23F", "🔥"),
    "forest": ("#0B4F2E-#B5E655", "🌿"),
    "ice": ("#1B4F9C-#E8F6FF", "🧊"),
}

HEX_RE = re.compile(r"#?([0-9a-fA-F]{6})")


def parse_hex(text: str) -> str | None:
    """Один цвет HEX или None."""
    colors = HEX_RE.findall(text)
    return f"#{colors[0].upper()}" if len(colors) == 1 and len(text.strip()) <= 7 else None


def parse_color_spec(text: str) -> str | None:
    """«#ff0000» → "#FF0000", «#ff0000 #0000ff» → градиент "#FF0000-#0000FF"."""
    colors = [f"#{c.upper()}" for c in HEX_RE.findall(text)]
    if len(colors) == 1 and len(text.strip()) <= 7:
        return colors[0]
    if len(colors) == 2:
        return "-".join(colors)
    return None


def hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255


class Tint:
    """Функция цвет → цвет. Оттенок и насыщенность — целевые, светлота — исходная."""

    def __init__(self, spec: str):
        self.stops = [hex_to_rgb(color) for color in spec.split("-")[:2]]

    def target(self, light: float) -> tuple[float, float]:
        """(оттенок, насыщенность) цели для данной светлоты."""
        if len(self.stops) == 1:
            r, g, b = self.stops[0]
        else:
            (r0, g0, b0), (r1, g1, b1) = self.stops
            mix = light * light * (3 - 2 * light)  # smoothstep: середина мягче, края чище
            r, g, b = r0 + (r1 - r0) * mix, g0 + (g1 - g0) * mix, b0 + (b1 - b0) * mix
        hue, _, sat = colorsys.rgb_to_hls(r, g, b)
        return hue, sat

    def map(self, light: float, sat: float) -> tuple[float, float, float]:
        hue, target_sat = self.target(light)
        # серые участки тоже окрашиваем, но чуть слабее насыщенных
        return colorsys.hls_to_rgb(hue, light, target_sat * (0.55 + 0.45 * sat))

    def __call__(self, r: float, g: float, b: float) -> tuple[float, float, float]:
        _, light, sat = colorsys.rgb_to_hls(r, g, b)
        return self.map(light, sat)

    def lookup_table(self, light_steps: int = 256, sat_steps: int = 33) -> np.ndarray:
        """Таблица (светлота × насыщенность) → RGB для быстрой перекраски картинок."""
        table = np.empty((light_steps, sat_steps, 3), dtype=np.float32)
        for li in range(light_steps):
            for si in range(sat_steps):
                table[li, si] = self.map(li / (light_steps - 1), si / (sat_steps - 1))
        return table


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


def recolor_tgs(tgs: bytes, spec: str) -> bytes:
    data = json.loads(gzip.decompress(tgs))
    recolor_lottie(data, Tint(spec))
    return gzip.compress(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode(), compresslevel=9)


def recolor_image(image_bytes: bytes, spec: str, fmt: str = "WEBP") -> bytes:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    rgba = np.asarray(image, dtype=np.float32) / 255
    rgb = rgba[..., :3]
    cmax, cmin = rgb.max(-1), rgb.min(-1)
    light = (cmax + cmin) / 2
    chroma = cmax - cmin
    sat = np.clip(chroma / np.maximum(1e-6, 1 - np.abs(2 * light - 1)), 0, 1)

    table = Tint(spec).lookup_table()
    light_index = np.rint(light * (table.shape[0] - 1)).astype(np.intp)
    sat_index = np.rint(sat * (table.shape[1] - 1)).astype(np.intp)
    out = np.concatenate([table[light_index, sat_index], rgba[..., 3:4]], -1)

    result = Image.fromarray(np.clip(out * 255 + 0.5, 0, 255).astype(np.uint8), "RGBA")
    buffer = io.BytesIO()
    result.save(buffer, fmt, lossless=True) if fmt == "WEBP" else result.save(buffer, fmt)
    return buffer.getvalue()
