"""Паки: превью-сетка, загрузка файлов, создание и пополнение наборов."""

import asyncio
import gzip
import io
import logging
import math
import secrets
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputSticker
from PIL import Image, ImageDraw
from rlottie_python import LottieAnimation

from app.common.files import download
from app.nft.render import load_font

log = logging.getLogger(__name__)

INITIAL_STICKERS = 50  # createNewStickerSet принимает до 50 стикеров сразу
GRID_BG = (23, 33, 43)
CELL_BG = (31, 43, 56)


def set_link(name: str, sticker_type: str | None) -> str:
    kind = "addemoji" if sticker_type == "custom_emoji" else "addstickers"
    return f"https://t.me/{kind}/{name}"


def sticker_format(sticker) -> str:
    return "animated" if sticker.is_animated else "video" if sticker.is_video else "static"


def new_set_name(prefix: str, bot_username: str) -> str:
    """Имя набора: начинается с буквы, оканчивается на _by_<бот>, до 64 символов."""
    suffix = f"_by_{bot_username}"
    core = f"{prefix}_{secrets.token_hex(3)}"
    return core[: 64 - len(suffix)] + suffix


def user_pack_name(user_id: int, bot_username: str) -> str:
    return f"my{user_id}_by_{bot_username}"


def _decode_frame(data: bytes, kind: str, size: int) -> Image.Image | None:
    try:
        if kind == "tgs":
            anim = LottieAnimation.from_data(gzip.decompress(data).decode("utf-8"))
            try:
                frame = anim.render_pillow_frame(anim.lottie_animation_get_totalframe() // 2, width=size, height=size)
            finally:
                anim.lottie_animation_destroy()
            # premultiplied → обычная альфа, чтобы края не темнели
            return Image.frombytes("RGBa", frame.size, frame.tobytes()).convert("RGBA")
        if kind == "webm":
            with tempfile.TemporaryDirectory() as temp_dir:
                src = Path(temp_dir) / "in.webm"
                src.write_bytes(data)
                png = subprocess.run(
                    [
                        imageio_ffmpeg.get_ffmpeg_exe(),
                        "-loglevel",
                        "error",
                        "-c:v",
                        "libvpx-vp9",
                        "-i",
                        str(src),
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
            return Image.open(io.BytesIO(png)).convert("RGBA") if png else None
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as e:
        log.warning("Не удалось нарисовать превью (%s): %s", kind, e)
        return None


async def preview_image(bot: Bot, sticker, slots: asyncio.Semaphore, size: int = 96) -> Image.Image | None:
    """Кадр стикера для сетки: tgs рендерим сами (чётче), остальное — по миниатюре."""
    if sticker.is_animated:
        data = await download(bot, sticker.file_id, slots)
        return await asyncio.to_thread(_decode_frame, data, "tgs", size) if data else None
    file_id = sticker.thumbnail.file_id if sticker.thumbnail else sticker.file_id
    kind = "webm" if (sticker.is_video and not sticker.thumbnail) else "image"
    data = await download(bot, file_id, slots)
    return await asyncio.to_thread(_decode_frame, data, kind, size) if data else None


def render_grid(images: list[Image.Image | None], title: str, subtitle: str, font_path: str | None = None) -> bytes:
    count = len(images)
    cols = 5 if count <= 15 else 8 if count <= 64 else 10
    rows = max(1, math.ceil(count / cols))
    cell, gap, pad, header = 104, 8, 24, 96
    width = pad * 2 + cols * cell + (cols - 1) * gap
    height = header + pad + rows * cell + (rows - 1) * gap
    canvas = Image.new("RGB", (width, height), GRID_BG)
    draw = ImageDraw.Draw(canvas)

    title_font = load_font("bold", 34, font_path)
    subtitle_font = load_font("regular", 20, font_path)
    number_font = load_font("regular", 14, font_path)
    while draw.textlength(title, font=title_font) > width - pad * 2 and title_font.size > 16:
        title_font = load_font("bold", int(title_font.size) - 2, font_path)
    draw.text((pad, 22), title, font=title_font, fill="#ffffff")
    draw.text((pad, 64), subtitle, font=subtitle_font, fill="#8c99a6")

    for index, image in enumerate(images):
        x = pad + (index % cols) * (cell + gap)
        y = header + (index // cols) * (cell + gap)
        draw.rounded_rectangle((x, y, x + cell, y + cell), radius=18, fill=CELL_BG)
        if image is not None:
            image.thumbnail((cell - 16, cell - 16), Image.Resampling.LANCZOS)
            canvas.paste(image, (x + (cell - image.width) // 2, y + (cell - image.height) // 2), image)
        draw.text((x + cell - 8, y + cell - 6), str(index + 1), font=number_font, fill="#5d6d7e", anchor="rd")

    buffer = io.BytesIO()
    canvas.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def input_sticker(data: bytes, fmt: str, emoji_list: list[str] | None, filename: str) -> InputSticker:
    return InputSticker(
        sticker=BufferedInputFile(data, filename=filename), format=fmt, emoji_list=(emoji_list or ["⭐"])[:20]
    )


async def _with_retry(call):
    for _ in range(5):
        try:
            return await call()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    return await call()


async def create_set(
    bot: Bot, user_id: int, name: str, title: str, sticker_type: str, stickers: list[InputSticker], progress=None
) -> int:
    """Создаёт набор и докладывает остальные стикеры. Возвращает, сколько добавлено."""
    await _with_retry(
        lambda: bot.create_new_sticker_set(
            user_id=user_id,
            name=name,
            title=title[:64],
            stickers=stickers[:INITIAL_STICKERS],
            sticker_type=sticker_type,
        )
    )
    added = min(len(stickers), INITIAL_STICKERS)
    for sticker in stickers[INITIAL_STICKERS:]:
        try:
            await _with_retry(lambda s=sticker: bot.add_sticker_to_set(user_id=user_id, name=name, sticker=s))
            added += 1
        except TelegramBadRequest as e:
            log.warning("Стикер не добавлен в %s: %s", name, e.message)
        if progress:
            await progress(added, len(stickers))
    return added


async def add_to_user_pack(bot: Bot, user_id: int, name: str, title: str, sticker: InputSticker) -> int:
    """Добавляет эмодзи в личный пак, создавая его при первом вызове. Возвращает размер пака."""
    try:
        await _with_retry(lambda: bot.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker))
    except TelegramBadRequest as e:
        if "STICKERSET_INVALID" not in e.message.upper():
            raise
        await _with_retry(
            lambda: bot.create_new_sticker_set(
                user_id=user_id,
                name=name,
                title=title,
                stickers=[sticker],
                sticker_type="custom_emoji",
            )
        )
    return len((await bot.get_sticker_set(name=name)).stickers)
