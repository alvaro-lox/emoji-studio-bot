"""Коллаж из нескольких NFT-подарков в одной анимации 60 fps.

Холст собирается из слоёв:
1. фон холста — статичный или «живой» градиент, тёмный фон или своё фото;
2. статичный слой — карточки подарков (фон, узор, рамка, тень), подписи и заголовок;
3. модели — анимации Lottie, каждая крутится по кругу в своей карточке.

Статичный слой считается один раз. Если фон холста не двигается, каждый кадр — это
готовая картинка плюс модели. Если двигается — фон пересчитывается в 1/8 разрешения
и растягивается: так плавно и в десятки раз дешевле, чем считать градиент целиком.
"""

import hashlib
import io
import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from app.nft.parser import CANVAS_H, GiftInfo
from app.nft.render import (
    FPS,
    Layout,
    ModelAnimation,
    RenderResult,
    backdrop_image,
    composite_premultiplied,
    format_number,
    hex_rgb,
    load_font,
    paste_alpha,
    video_writer,
)

FRAMES = ("cards", "frames", "circles", "puzzle")
BACKGROUNDS = ("gradient", "animated", "dark", "photo")
ASPECTS = {"1:1": (1080, 1080), "9:16": (1080, 1920), "16:9": (1920, 1080)}
MAX_GIFTS = 12
MIN_LOOP, MAX_LOOP = 2.0, 6.0  # секунды

# палитры фона холста: от тёмного края к светлому центру
PALETTES: dict[str, tuple[str, ...]] = {
    "night": ("#0B1020", "#1B2A4A", "#2E4A7A"),
    "sunset": ("#1A0B2E", "#5B1A5C", "#C4426B"),
    "ocean": ("#03141F", "#0B4A6F", "#1C9AA8"),
    "aurora": ("#06141B", "#0F5144", "#5B2A86"),
    "gold": ("#140E04", "#5A3B0E", "#B8892E"),
    "graphite": ("#0D0F12", "#1E232B", "#3A4250"),
}
DARK_COLOR = "#0E1116"

SUPERSAMPLE = 3  # маски рамок рисуем крупнее и уменьшаем — края гладкие


@dataclass
class CollageStyle:
    frame: str = "cards"  # cards | frames | circles | puzzle
    gap: bool = True  # для рамок: с отступами или вплотную
    gift_backdrop: bool = True  # фон подарка внутри карточки
    background: str = "gradient"  # gradient | animated | dark | photo
    palette: str = "auto"  # auto (по цветам подарков) или ключ PALETTES
    photo: bytes | None = None
    captions: bool = True
    title: str = ""
    aspect: str = "1:1"

    @property
    def needs_canvas_background(self) -> bool:
        """Пазл закрывает весь холст сам — фон под ним не виден."""
        return self.frame != "puzzle"

    def key(self) -> str:
        raw = (
            f"{self.frame}|{self.gap}|{self.gift_backdrop}|{self.background}|{self.palette}|"
            f"{self.captions}|{self.title}|{self.aspect}|"
        ).encode()
        if self.photo and self.background == "photo":
            raw += hashlib.sha1(self.photo).digest()
        return hashlib.sha1(raw).hexdigest()[:12]


@dataclass
class CollageGift:
    info: GiftInfo
    model: bytes
    pattern: bytes | None


@dataclass
class Tile:
    """Место подарка на холсте: клетка, квадрат модели и куда писать подпись."""

    gift: CollageGift
    left: int
    top: int
    width: int
    height: int
    model_left: int = 0
    model_top: int = 0
    model_size: int = 0
    animation: ModelAnimation | None = field(default=None, repr=False)


# ---------- сетка ----------


def grid_shape(count: int, area_width: float, area_height: float, gap_ratio: float, caption_ratio: float):
    """(колонки, строки, сторона клетки) с самыми крупными клетками для этой площади."""
    best = (1, count, 0.0)
    for columns in range(1, count + 1):
        rows = math.ceil(count / columns)
        by_width = area_width / (columns + (columns - 1) * gap_ratio)
        by_height = area_height / (rows * (1 + caption_ratio) + (rows - 1) * gap_ratio)
        side = min(by_width, by_height)
        if side > best[2]:
            best = (columns, rows, side)
    return best


def puzzle_rows(count: int, width: int, height: int) -> list[int]:
    """Сколько кусочков в каждой строке пазла. Пустых мест нет: строки отличаются максимум на один."""
    best: tuple[float, list[int]] | None = None
    for rows in range(1, count + 1):
        base, extra = divmod(count, rows)
        counts = [base + 1] * extra + [base] * (rows - extra)
        row_height = height / rows
        # кусочки должны быть поближе к квадрату
        score = sum(abs(math.log((width / n) / row_height)) for n in counts) / rows
        if best is None or score < best[0]:
            best = (score, counts)
    assert best is not None
    return best[1]


# ---------- фон холста ----------


def _palette(style: CollageStyle, gifts: list[CollageGift]) -> list[np.ndarray]:
    if style.palette in PALETTES:
        return [hex_rgb(color) for color in PALETTES[style.palette]]
    centers = [hex_rgb(g.info.center_color) for g in gifts if g.info.has_backdrop]
    if not centers:
        return [hex_rgb(color) for color in PALETTES["night"]]
    # цвета подарков, приглушённые, чтобы фон не спорил с карточками
    first, second = centers[0], centers[1 % len(centers)]
    return [first * 0.18, (first * 0.45 + second * 0.45) * 0.75, second * 0.6]


def _gradient_pixels(colors: list[np.ndarray], width: int, height: int, phase: float) -> np.ndarray:
    """Линейный градиент по нескольким цветам с поворотом на угол phase·360° + мягкая виньетка."""
    rows, cols = np.mgrid[0:height, 0:width].astype(np.float32)
    rows = rows / max(1, height - 1) - 0.5
    cols = cols / max(1, width - 1) - 0.5
    angle = math.radians(35) + phase * 2 * math.pi
    position = cols * math.cos(angle) + rows * math.sin(angle)
    position = np.clip(position / 0.75 + 0.5 + 0.08 * math.sin(phase * 2 * math.pi), 0, 1)

    stops = np.linspace(0, 1, len(colors))
    pixels = np.empty((height, width, 3), dtype=np.float32)
    for channel in range(3):
        pixels[..., channel] = np.interp(position, stops, [color[channel] for color in colors])
    vignette = 1 - 0.35 * np.clip(np.hypot(cols, rows) * 1.4, 0, 1) ** 2
    return pixels * vignette[..., None]


def _dark_pixels(width: int, height: int) -> np.ndarray:
    image = Image.new("RGB", (width, height), DARK_COLOR)
    draw = ImageDraw.Draw(image)
    step = max(24, width // 22)
    for top in range(step // 2, height, step):  # еле заметная сетка точек — фон не «пустой»
        for left in range((top // step % 2) * step // 2, width, step):
            draw.ellipse((left - 1, top - 1, left + 1, top + 1), fill="#1A2029")
    pixels = np.asarray(image, dtype=np.float32)
    rows, cols = np.mgrid[0:height, 0:width].astype(np.float32)
    distance = np.hypot(cols / width - 0.5, rows / height - 0.5)
    return pixels * (1 - 0.45 * np.clip(distance * 1.5, 0, 1) ** 2)[..., None]


def _photo_pixels(data: bytes, width: int, height: int) -> np.ndarray:
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    image = ImageOps.fit(image, (width, height), Image.Resampling.LANCZOS)
    image = image.filter(ImageFilter.GaussianBlur(max(2, width // 400)))  # карточки читаются лучше
    return np.asarray(image, dtype=np.float32) * 0.62


class CanvasBackground:
    def __init__(self, style: CollageStyle, gifts: list[CollageGift], width: int, height: int):
        self.width, self.height = width, height
        self.animated = style.background == "animated" and style.needs_canvas_background
        self._colors = _palette(style, gifts)
        if not style.needs_canvas_background:
            self._static = _dark_pixels(width, height)
        elif style.background == "photo" and style.photo:
            self._static = _photo_pixels(style.photo, width, height)
        elif style.background == "dark":
            self._static = _dark_pixels(width, height)
        else:
            self._static = _gradient_pixels(self._colors, width, height, 0.0)

    def frame(self, phase: float) -> np.ndarray:
        if not self.animated:
            return self._static
        small = _gradient_pixels(self._colors, self.width // 8, self.height // 8, phase)
        image = Image.fromarray(np.clip(small, 0, 255).astype(np.uint8), "RGB")
        return np.asarray(image.resize((self.width, self.height), Image.Resampling.BILINEAR), dtype=np.float32)


# ---------- формы ----------


def _shape_mask(kind: str, width: int, height: int) -> Image.Image:
    big = Image.new("L", (width * SUPERSAMPLE, height * SUPERSAMPLE), 0)
    draw = ImageDraw.Draw(big)
    box = (0, 0, big.width - 1, big.height - 1)
    if kind == "circles":
        draw.ellipse(box, fill=255)
    elif kind == "cards":
        draw.rounded_rectangle(box, radius=round(min(big.size) * 0.17), fill=255)
    else:
        draw.rectangle(box, fill=255)
    return big.resize((width, height), Image.Resampling.LANCZOS)


def _edges(mask: Image.Image, width: int) -> tuple[Image.Image, Image.Image]:
    """(внешний контур, внутренняя полоска у края) — для рамок и швов пазла."""
    inner = mask.filter(ImageFilter.MinFilter(width * 2 + 1))
    outline = Image.fromarray(
        np.clip(np.asarray(mask, np.int16) - np.asarray(inner, np.int16), 0, 255).astype(np.uint8)
    )
    deeper = inner.filter(ImageFilter.MinFilter(width * 2 + 1))
    rim = Image.fromarray(np.clip(np.asarray(inner, np.int16) - np.asarray(deeper, np.int16), 0, 255).astype(np.uint8))
    return outline, rim


def _tile_backdrop(gift: CollageGift, width: int, height: int, cell: float, with_backdrop: bool) -> Image.Image:
    """Содержимое карточки: фон подарка (градиент и узор) или полупрозрачное «стекло»."""
    if with_backdrop and gift.info.has_backdrop:
        layout = Layout.centered(width, height, cell / CANVAS_H)
        return backdrop_image(gift.info, gift.pattern, layout)
    glass = Image.new("RGBA", (width, height), (255, 255, 255, 18))
    shine = np.linspace(34, 0, height, dtype=np.float32)[:, None].repeat(width, 1)
    glass.alpha_composite(Image.fromarray(np.dstack([np.full_like(shine, 255)] * 3 + [shine]).astype(np.uint8), "RGBA"))
    return glass


def _shadow(mask: Image.Image, radius: int, opacity: float) -> Image.Image:
    pad = radius * 3
    shadow = Image.new("L", (mask.width + pad * 2, mask.height + pad * 2), 0)
    shadow.paste(mask, (pad, pad))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius)).point(lambda v: round(v * opacity))
    layer = Image.new("RGBA", shadow.size, (0, 0, 0, 255))
    layer.putalpha(shadow)
    return layer


def _framed_tile(gift: CollageGift, kind: str, size: int, style: CollageStyle, tight: bool) -> tuple[Image.Image, int]:
    """Готовая карточка с тенью и рамкой. Возвращает (картинка, отступ под тень)."""
    mask = _shape_mask(kind, size, size)
    content = _tile_backdrop(gift, size, size, size, style.gift_backdrop)
    content.putalpha(Image.fromarray(np.minimum(np.asarray(content.getchannel("A")), np.asarray(mask))))

    border = max(2, size // 110)
    outline, rim = _edges(mask, border)
    if tight:  # вплотную: тонкий тёмный шов между карточками
        line = Image.new("RGBA", (size, size), (0, 0, 0, 150))
        line.putalpha(outline.point(lambda v: round(v * 0.6)))
    else:  # стеклянная рамка: светлый контур и блик у верхнего края
        line = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        line.putalpha(outline.point(lambda v: round(v * 0.32)))
        highlight = np.linspace(0.9, 0, size, dtype=np.float32)[:, None] * np.asarray(rim, np.float32) * 0.35
        glow = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        glow.putalpha(Image.fromarray(highlight.astype(np.uint8)))
        content.alpha_composite(glow)
    content.alpha_composite(line)

    if tight:
        return content, 0
    blur = max(4, size // 28)
    canvas = _shadow(mask, blur, 0.55)
    pad = blur * 3
    canvas.alpha_composite(content, (pad, pad + blur // 2))
    return canvas, pad


# ---------- пазл ----------


@dataclass
class PuzzlePiece:
    left: float
    top: float
    width: float
    height: float


def _puzzle_pieces(row_counts: list[int], width: int, height: int) -> list[PuzzlePiece]:
    pieces: list[PuzzlePiece] = []
    row_height = height / len(row_counts)
    for row, count in enumerate(row_counts):
        piece_width = width / count
        pieces.extend(
            PuzzlePiece(column * piece_width, row * row_height, piece_width, row_height) for column in range(count)
        )
    return pieces


def _puzzle_labels(pieces: list[PuzzlePiece], row_counts: list[int], width: int, height: int, seed: int) -> np.ndarray:
    """Карта номеров кусочков в увеличенном масштабе: клетки + «замки» на каждом общем крае.

    Замок — круглая головка на шейке. Её пиксели отдаются соседу, поэтому выступ одного
    кусочка всегда совпадает с вырезом другого, даже если в строках разное число кусочков.
    """
    scale = SUPERSAMPLE
    labels = Image.new("I", (width * scale, height * scale), 0)
    draw = ImageDraw.Draw(labels)
    for index, piece in enumerate(pieces):
        left, top = round(piece.left * scale), round(piece.top * scale)
        right, bottom = round((piece.left + piece.width) * scale), round((piece.top + piece.height) * scale)
        draw.rectangle((left, top, right - 1, bottom - 1), fill=index)

    rng = np.random.default_rng(seed)

    def knob(owner: int, edge_x: float, edge_y: float, dx: int, dy: int, radius: float) -> None:
        reach, neck = radius * 0.8, radius * 0.6
        head_x, head_y = (edge_x + dx * reach) * scale, (edge_y + dy * reach) * scale
        head = radius * scale
        draw.ellipse((head_x - head, head_y - head, head_x + head, head_y + head), fill=owner)
        base_x, base_y = edge_x * scale, edge_y * scale
        if dx:
            draw.rectangle(
                (min(base_x, head_x), base_y - neck * scale, max(base_x, head_x), base_y + neck * scale), fill=owner
            )
        else:
            draw.rectangle(
                (base_x - neck * scale, min(base_y, head_y), base_x + neck * scale, max(base_y, head_y)), fill=owner
            )

    starts = np.cumsum([0, *row_counts])
    for row, count in enumerate(row_counts):
        row_pieces = list(range(starts[row], starts[row] + count))
        for left_index, right_index in itertools.pairwise(row_pieces):  # края внутри строки
            left_piece, right_piece = pieces[left_index], pieces[right_index]
            radius = min(left_piece.width, left_piece.height) * 0.15
            owner, direction = (left_index, 1) if rng.random() < 0.5 else (right_index, -1)
            knob(owner, right_piece.left, left_piece.top + left_piece.height / 2, direction, 0, radius)
        if row + 1 == len(row_counts):
            continue
        for upper_index in row_pieces:  # края между строками: по замку на каждый общий отрезок
            upper = pieces[upper_index]
            for lower_index in range(starts[row + 1], starts[row + 2]):
                lower = pieces[lower_index]
                overlap_left = max(upper.left, lower.left)
                overlap_right = min(upper.left + upper.width, lower.left + lower.width)
                overlap = overlap_right - overlap_left
                if overlap < min(upper.width, lower.width) * 0.4:
                    continue
                radius = min(overlap * 0.3, upper.height * 0.15)
                owner, direction = (upper_index, 1) if rng.random() < 0.5 else (lower_index, -1)
                knob(owner, (overlap_left + overlap_right) / 2, lower.top, 0, direction, radius)
    return np.asarray(labels, dtype=np.int32)


def _downsample(mask: np.ndarray, width: int, height: int) -> Image.Image:
    image = Image.fromarray(mask.astype(np.uint8) * 255, "L")
    return image.resize((width, height), Image.Resampling.BOX)


def _subtract(outer: Image.Image, inner: Image.Image) -> Image.Image:
    difference = np.asarray(outer, np.int16) - np.asarray(inner, np.int16)
    return Image.fromarray(np.clip(difference, 0, 255).astype(np.uint8), "L")


# ---------- подписи ----------


class Captions:
    def __init__(self, font_path: str | None, size: int):
        self.title_font = load_font("bold", size, font_path)

    def draw(self, image: Image.Image, info: GiftInfo, center_x: float, center_y: float, max_width: float) -> None:
        """«Название #номер» по центру. Рисуем на отдельном слое: ImageDraw на RGBA заменяет
        пиксели, а не смешивает их, и полупрозрачная тень пробила бы дыру в картинке."""
        measure = ImageDraw.Draw(image)
        font = self.title_font
        title, number = info.title, format_number(info.number)
        while True:
            gap = font.size * 0.3
            title_width = measure.textlength(title, font=font)
            number_width = measure.textlength(number, font=font)
            if title_width + gap + number_width <= max_width or font.size <= 10:
                break
            font = font.font_variant(size=int(font.size * 0.9))

        shadow = max(1, int(font.size) // 14)
        width = round(title_width + gap + number_width) + shadow * 4
        height = round(font.size * 1.6)
        middle = height / 2
        parts = ((title, 0.0, "#FFFFFF"), (number, title_width + gap, "#A9B4C2"))
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for text, offset, _ in parts:
            draw.text((offset + shadow * 2, middle + shadow), text, font=font, fill=(0, 0, 0, 150), anchor="lm")
        layer = layer.filter(ImageFilter.GaussianBlur(shadow * 0.8))
        letters = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(letters)
        for text, offset, color in parts:
            draw.text((offset + shadow, middle), text, font=font, fill=color, anchor="lm")
        layer.alpha_composite(letters)
        paste_alpha(image, layer, round(center_x - width / 2), round(center_y - middle))


def _draw_title(image: Image.Image, text: str, band: int, font_path: str | None) -> None:
    draw = ImageDraw.Draw(image)
    font = load_font("bold", round(band * 0.52), font_path)
    while draw.textlength(text, font=font) > image.width * 0.9 and font.size > 12:
        font = font.font_variant(size=int(font.size * 0.92))
    center = (image.width / 2, band * 0.58)
    blur_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(blur_layer).text(center, text, font=font, fill=(0, 0, 0, 170), anchor="mm")
    image.alpha_composite(blur_layer.filter(ImageFilter.GaussianBlur(max(2, font.size // 10))))
    draw.text(center, text, font=font, fill="#FFFFFF", anchor="mm")


# ---------- раскладка ----------


MODEL_SHARE = {"cards": 0.74, "frames": 0.74, "circles": 0.66, "puzzle": 0.62}


def _layout_framed(
    gifts: list[CollageGift],
    style: CollageStyle,
    width: int,
    height: int,
    top_band: int,
    overlay: Image.Image,
    font_path,
) -> list[Tile]:
    tight = style.frame == "frames" and not style.gap
    margin = 0 if tight else round(min(width, height) * 0.05)
    gap_ratio = 0 if tight else 0.08
    caption_ratio = 0.16 if style.captions and not tight else 0
    area_top = top_band + (0 if tight else margin // 2 if top_band else margin)
    area_width, area_height = width - margin * 2, height - area_top - margin

    columns, rows, side = grid_shape(len(gifts), area_width, area_height, gap_ratio, caption_ratio)
    side = int(side) // 2 * 2
    gap, caption = side * gap_ratio, side * caption_ratio
    block_height = rows * (side + caption) + (rows - 1) * gap
    first_top = area_top + (area_height - block_height) / 2
    captions = Captions(font_path, max(14, round(side * 0.085)))

    tiles: list[Tile] = []
    for index, gift in enumerate(gifts):
        row, column = divmod(index, columns)
        in_row = min(columns, len(gifts) - row * columns)
        row_left = margin + (area_width - (in_row * side + (in_row - 1) * gap)) / 2
        left = round(row_left + column * (side + gap))
        top = round(first_top + row * (side + caption + gap))

        card, pad = _framed_tile(gift, style.frame, side, style, tight)
        paste_alpha(overlay, card, left - pad, top - pad)

        model_size = int(side * MODEL_SHARE[style.frame]) // 2 * 2
        model_top = top + (side - model_size) // 2
        if tight and style.captions:  # подпись внутри карточки — модель чуть выше
            model_top -= round(side * 0.06)
            _scrim(overlay, left, top + round(side * 0.72), side, side - round(side * 0.72))
            captions.draw(overlay, gift.info, left + side / 2, top + side * 0.89, side * 0.9)
        elif style.captions:
            captions.draw(overlay, gift.info, left + side / 2, top + side + caption * 0.55, side + gap)
        tiles.append(Tile(gift, left, top, side, side, left + (side - model_size) // 2, model_top, model_size))
    return tiles


def _scrim(image: Image.Image, left: int, top: int, width: int, height: int) -> None:
    """Затемнение снизу карточки под подписью."""
    alpha = (np.linspace(0, 1, height, dtype=np.float32) ** 1.4 * 150)[:, None].repeat(width, 1)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    layer.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    paste_alpha(image, layer, left, top)


def _layout_puzzle(
    gifts: list[CollageGift], style: CollageStyle, width: int, height: int, overlay: Image.Image, font_path
) -> list[Tile]:
    row_counts = puzzle_rows(len(gifts), width, height)
    pieces = _puzzle_pieces(row_counts, width, height)
    seed = int(hashlib.sha1("|".join(g.info.slug for g in gifts).encode()).hexdigest()[:8], 16)
    labels = _puzzle_labels(pieces, row_counts, width, height, seed)
    smallest = min(min(piece.width, piece.height) for piece in pieces)
    captions = Captions(font_path, max(14, round(smallest * 0.075)))
    dark = (*hex_rgb(DARK_COLOR).astype(int).tolist(), 255)

    board = Image.new("RGBA", (width, height), dark)
    tiles: list[Tile] = []
    for index, (gift, piece) in enumerate(zip(gifts, pieces, strict=True)):
        mask = _downsample(labels == index, width, height)
        box = mask.getbbox()
        if not box:
            continue
        left, top, right, bottom = box
        side = min(piece.width, piece.height)
        content = _tile_backdrop(gift, right - left, bottom - top, side, style.gift_backdrop)
        piece_image = Image.new("RGBA", content.size, dark)
        piece_image.alpha_composite(content)
        piece_image.putalpha(mask.crop(box))
        board.alpha_composite(piece_image, (left, top))

        model_size = int(side * MODEL_SHARE["puzzle"]) // 2 * 2
        model_left = round(piece.left + (piece.width - model_size) / 2)
        model_top = round(piece.top + (piece.height - model_size) / 2 - (side * 0.05 if style.captions else 0))
        tile_box = (round(piece.left), round(piece.top), round(piece.width), round(piece.height))
        tiles.append(Tile(gift, *tile_box, model_left, model_top, model_size))

    # швы: где у соседних пикселей разные кусочки — тёмная линия, рядом светлая кромка
    seam = np.zeros(labels.shape, dtype=bool)
    seam[:, 1:] |= labels[:, 1:] != labels[:, :-1]
    seam[1:, :] |= labels[1:, :] != labels[:-1, :]
    seam_width = max(3, round(smallest / 120)) | 1
    line = _downsample(seam, width, height).point(lambda v: min(255, v * 3)).filter(ImageFilter.MaxFilter(seam_width))
    rim = _subtract(line.filter(ImageFilter.MaxFilter(seam_width + 4)), line)
    highlight = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    highlight.putalpha(rim.point(lambda v: round(v * 0.16)))
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    shadow.putalpha(line.point(lambda v: round(v * 0.75)))
    board.alpha_composite(highlight)
    board.alpha_composite(shadow)
    overlay.alpha_composite(board)

    if style.captions:
        for tile in tiles:
            center_x, caption_y = tile.left + tile.width / 2, tile.top + tile.height * 0.86
            captions.draw(overlay, tile.gift.info, center_x, caption_y, tile.width * 0.86)
    return tiles


# ---------- рендер ----------


def collage_size(style: CollageStyle, scale: float = 1.0) -> tuple[int, int]:
    width, height = ASPECTS.get(style.aspect, ASPECTS["1:1"])
    return int(width * scale) // 2 * 2, int(height * scale) // 2 * 2


def build_overlay(
    gifts: list[CollageGift], style: CollageStyle, width: int, height: int, font_path: str | None
) -> tuple[Image.Image, list[Tile]]:
    """Статичный слой (всё, кроме фона холста и моделей) и места моделей."""
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    title_band = round(min(width, height) * 0.13) if style.title else 0
    if style.frame == "puzzle":
        tiles = _layout_puzzle(gifts, style, width, height, overlay, font_path)
    else:
        tiles = _layout_framed(gifts, style, width, height, title_band, overlay, font_path)
    if style.title:
        if style.frame == "puzzle":
            _scrim_top(overlay, round(title_band * 1.4))
        _draw_title(overlay, style.title, title_band, font_path)
    return overlay, tiles


def _scrim_top(image: Image.Image, height: int) -> None:
    alpha = (np.linspace(1, 0, height, dtype=np.float32) ** 1.2 * 170)[:, None].repeat(image.width, 1)
    layer = Image.new("RGBA", (image.width, height), (0, 0, 0, 255))
    layer.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    image.alpha_composite(layer)


def render_collage(
    gifts: list[CollageGift],
    style: CollageStyle,
    out_path: Path,
    font_path: str | None = None,
    scale: float = 1.0,
) -> RenderResult:
    width, height = collage_size(style, scale)
    overlay, tiles = build_overlay(gifts, style, width, height, font_path)
    background = CanvasBackground(style, gifts, width, height)

    overlay_pixels = np.asarray(overlay, dtype=np.float32)
    overlay_alpha = overlay_pixels[..., 3:4] / 255
    overlay_premultiplied = overlay_pixels[..., :3] * overlay_alpha
    del overlay_pixels

    def compose(phase: float) -> np.ndarray:
        return overlay_premultiplied + background.frame(phase) * (1 - overlay_alpha)

    for tile in tiles:
        tile.animation = ModelAnimation(tile.gift.model, tile.model_size)
    try:
        loop_seconds = max(tile.animation.duration for tile in tiles if tile.animation)
        frame_count = round(min(MAX_LOOP, max(MIN_LOOP, loop_seconds)) * FPS)

        static_under = None if background.animated else compose(0.0)
        frame = np.empty((height, width, 3), dtype=np.uint8)
        if static_under is not None:  # фон не двигается: каждый кадр перерисовываем только модели
            np.copyto(frame, np.clip(static_under, 0, 255), casting="unsafe")
        with video_writer(out_path, width, height, crf=20) as stream:
            for index in range(frame_count):
                under = static_under
                if under is None:
                    under = compose(index / frame_count)
                    np.copyto(frame, np.clip(under, 0, 255), casting="unsafe")
                for tile in tiles:
                    _draw_model(frame, under, tile, index)
                stream.write(frame.tobytes())
    finally:
        for tile in tiles:
            if tile.animation:
                tile.animation.close()
    return RenderResult(out_path, width, height, max(1, round(frame_count / FPS)))


def _draw_model(frame: np.ndarray, under: np.ndarray, tile: Tile, index: int) -> None:
    assert tile.animation is not None
    height, width = frame.shape[:2]
    size = tile.model_size
    left, top = max(0, tile.model_left), max(0, tile.model_top)
    right, bottom = min(width, tile.model_left + size), min(height, tile.model_top + size)
    if right <= left or bottom <= top:
        return
    model = tile.animation.frame(index)[
        top - tile.model_top : bottom - tile.model_top, left - tile.model_left : right - tile.model_left
    ]
    frame[top:bottom, left:right] = composite_premultiplied(model, under[top:bottom, left:right])


def preview_collage(gifts: list[CollageGift], style: CollageStyle, font_path: str | None = None) -> bytes:
    """Быстрое статичное превью (JPEG) — чтобы показать раскладку до долгого рендера."""
    width, height = collage_size(style, 0.5)
    overlay, tiles = build_overlay(gifts, style, width, height, font_path)
    background = CanvasBackground(style, gifts, width, height)
    composed = np.asarray(overlay, np.float32)
    alpha = composed[..., 3:4] / 255
    pixels = composed[..., :3] * alpha + background.frame(0.0) * (1 - alpha)
    frame = np.clip(pixels, 0, 255).astype(np.uint8)
    for tile in tiles:
        tile.animation = ModelAnimation(tile.gift.model, tile.model_size)
        try:
            _draw_model(frame, pixels, tile, tile.animation.output_frames // 2)
        finally:
            tile.animation.close()
    buffer = io.BytesIO()
    Image.fromarray(frame, "RGB").save(buffer, "JPEG", quality=90)
    return buffer.getvalue()
