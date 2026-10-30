"""Создание премиум-эмодзи из картинки, GIF или видео и личный пак пользователя."""

import asyncio
import io
import logging
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto, Message
from PIL import Image

from app.common.files import read_file
from app.common.i18n import t
from app.common.icons import icons
from app.common.limits import Limits
from app.common.storage import Storage, TokenStore
from app.common.ui import heavy_ok, keyboard
from app.stickers.emoji_maker import first_emoji, make_static, make_video, preview, video_frame
from app.stickers.packs import add_to_user_pack, input_sticker, set_link, user_pack_name

log = logging.getLogger(__name__)
router = Router()

MAX_SOURCE = 20 * 1024 * 1024  # больше бот скачать не может


def media_source(message: Message):
    """(file, тип 'static' | 'video', расширение) или None."""
    if message.photo:
        return message.photo[-1], "static", ".jpg"
    if message.animation:
        return message.animation, "video", ".mp4"
    if message.video:
        return message.video, "video", ".mp4"
    if message.video_note:
        return message.video_note, "video", ".mp4"
    doc = message.document
    if doc:
        mime = doc.mime_type or ""
        if mime == "image/gif":
            return doc, "video", ".gif"
        if mime.startswith("image/"):
            return doc, "static", ".png"
        if mime.startswith("video/"):
            return doc, "video", "." + (mime.split("/")[-1] or "mp4")
    return None


def maker_kb(lang: str, token: str, state: dict):
    rows = []
    if state["kind"] == "static":
        label = t(lang, "btn_bg_restore") if state["bg"] else t(lang, "btn_bg_erase")
        rows.append([icons.button(label, "eraser", "primary", callback_data=f"mk:bg:{token}")])
    rows.append([icons.button(t(lang, "btn_add_pack"), "add", "success", callback_data=f"mk:add:{token}")])
    rows.append(
        [
            icons.button(
                t(lang, "btn_png" if state["kind"] == "static" else "btn_webm"),
                "file",
                "primary",
                callback_data=f"mk:file:{token}",
            )
        ]
    )
    return keyboard(*rows)


def _build(state: dict) -> tuple[bytes, bytes]:
    """(готовый эмодзи, превью-картинка)."""
    if state["kind"] == "static":
        result = make_static(state["src"], state["bg"])
        return result, preview(Image.open(io.BytesIO(result)))
    result = make_video(state["src"], state["ext"])
    return result, preview(video_frame(result))


def _caption(lang: str, state: dict) -> str:
    key = "maker_ready" if state["kind"] == "static" else "maker_ready_video"
    return t(lang, key, emoji=state["emoji"])


async def make_emoji(
    message: Message, bot: Bot, lang: str, file_id: str, kind: str, ext: str, emoji: str, blobs: TokenStore
) -> None:
    """Скачивает исходник, делает эмодзи и отвечает карточкой с превью и кнопками."""
    status = await message.answer(t(lang, "maker_wait"))
    try:
        data = await read_file(bot, file_id)
        state = {"src": data, "kind": kind, "ext": ext, "bg": False, "emoji": emoji}
        state["result"], shot = await asyncio.to_thread(_build, state)
    except Exception:
        log.exception("Не удалось сделать эмодзи")
        with suppress(TelegramBadRequest):
            await status.edit_text(t(lang, "maker_failed"))
        return
    token = blobs.put(state)
    await message.reply_photo(
        BufferedInputFile(shot, filename="emoji.png"),
        caption=_caption(lang, state),
        reply_markup=maker_kb(lang, token, state),
    )
    with suppress(TelegramBadRequest):
        await status.delete()


@router.message(F.photo | F.animation | F.video | F.video_note | F.document)
async def on_media(message: Message, bot: Bot, lang: str, blobs: TokenStore, limits: Limits, is_admin: bool) -> None:
    source = media_source(message)
    if not source:
        await message.answer(t(lang, "nothing_found"))
        return
    file, kind, ext = source
    if (file.file_size or 0) > MAX_SOURCE:
        await message.answer(t(lang, "file_too_big"))
        return
    if not await heavy_ok(message, limits, lang, is_admin):
        return
    emoji = first_emoji(message.caption) or "⭐"
    await make_emoji(message, bot, lang, file.file_id, kind, ext, emoji, blobs)


@router.callback_query(F.data.startswith("mk:"))
async def cb_maker(call: CallbackQuery, bot: Bot, lang: str, blobs: TokenStore, db: Storage) -> None:
    _, action, token = ([*call.data.split(":", 2), "", ""])[:3]
    state = blobs.get(token)
    if not state:
        await call.answer(t(lang, "expired"), show_alert=True)
        return

    if action == "bg":
        await call.answer(t(lang, "maker_toast"))
        state["bg"] = not state["bg"]
        try:
            state["result"], shot = await asyncio.to_thread(_build, state)
        except Exception:
            log.exception("Не удалось пересобрать эмодзи")
            return
        with suppress(TelegramBadRequest):
            await call.message.edit_media(
                InputMediaPhoto(media=BufferedInputFile(shot, filename="emoji.png"), caption=_caption(lang, state)),
                reply_markup=maker_kb(lang, token, state),
            )
    elif action == "file":
        await call.answer()
        ext = "png" if state["kind"] == "static" else "webm"
        await call.message.answer_document(
            BufferedInputFile(state["result"], filename=f"emoji.{ext}"), disable_content_type_detection=True
        )
    elif action == "add":
        await call.answer(t(lang, "maker_toast"))
        me = await bot.me()
        name = user_pack_name(call.from_user.id, me.username or "")
        fmt = "static" if state["kind"] == "static" else "video"
        sticker = input_sticker(state["result"], fmt, [state["emoji"]], f"emoji.{'png' if fmt == 'static' else 'webm'}")
        try:
            count = await add_to_user_pack(
                bot, call.from_user.id, name, t(lang, "my_pack_title", bot=me.username), sticker
            )
        except TelegramBadRequest as e:
            log.warning("Не удалось добавить эмодзи в %s: %s", name, e.message)
            key = "pack_full" if "TOO_MUCH" in e.message.upper() else "pack_failed"
            await call.message.answer(t(lang, key, error=escape(e.message)))
            return
        await db.log_event("emoji", call.from_user.id)
        await call.message.answer(
            t(lang, "maker_added", n=count),
            reply_markup=keyboard(
                [icons.button(t(lang, "btn_open_pack"), "archive", "success", url=set_link(name, "custom_emoji"))]
            ),
        )
    else:
        await call.answer()


@router.callback_query(F.data == "mypack")
async def cb_my_pack(call: CallbackQuery, bot: Bot, lang: str) -> None:
    me = await bot.me()
    name = user_pack_name(call.from_user.id, me.username or "")
    try:
        count = len((await bot.get_sticker_set(name=name)).stickers)
    except TelegramBadRequest:
        await call.answer(t(lang, "mypack_empty"), show_alert=True)
        return
    await call.message.answer(
        t(lang, "mypack", n=count),
        reply_markup=keyboard(
            [icons.button(t(lang, "btn_open_pack"), "archive", "success", url=set_link(name, "custom_emoji"))]
        ),
    )
    await call.answer()
