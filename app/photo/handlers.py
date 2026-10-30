"""Раздел «Фото»: на присланную картинку бот предлагает убрать фон, улучшить качество или сделать эмодзи.

Картинка с эмодзи в подписи сразу уходит в создание эмодзи — подпись задаёт его значок.
Результаты отправляются документом, чтобы Telegram не пережимал их.
"""

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from app.common.files import read_file
from app.common.i18n import t
from app.common.icons import icons
from app.common.limits import Limits
from app.common.storage import Storage, TokenStore
from app.common.ui import heavy_ok, keyboard
from app.photo.tools import enhance, remove_background
from app.stickers.emoji_maker import first_emoji
from app.stickers.handlers.maker import MAX_SOURCE, make_emoji

log = logging.getLogger(__name__)
router = Router()

IMAGE_MIME_PREFIX = "image/"


def _image_file(message: Message):
    """(file_id, размер, расширение) для фото или картинки-документа; GIF сюда не относится."""
    if message.photo:
        photo = message.photo[-1]
        return photo.file_id, photo.file_size or 0, ".jpg"
    document = message.document
    mime = (document.mime_type or "") if document else ""
    if document and mime.startswith(IMAGE_MIME_PREFIX) and mime != "image/gif":
        return document.file_id, document.file_size or 0, "." + (mime.split("/")[-1] or "png")
    return None


def photo_actions_kb(lang: str, token: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            icons.button(t(lang, "btn_photo_cutout"), "eraser", "primary", callback_data=f"ph:bg:{token}"),
            icons.button(t(lang, "btn_photo_enhance"), "sparkles", "primary", callback_data=f"ph:up:{token}"),
        ],
        [icons.button(t(lang, "btn_photo_emoji"), "magic", "success", callback_data=f"ph:em:{token}")],
    )


@router.message(F.photo | F.document.mime_type.startswith(IMAGE_MIME_PREFIX))
async def on_photo(message: Message, lang: str, tokens: TokenStore) -> None:
    source = _image_file(message)
    if source is None or first_emoji(message.caption):
        raise SkipHandler()  # GIF или подпись-значок — это сразу эмодзи
    file_id, size, ext = source
    if size > MAX_SOURCE:
        await message.answer(t(lang, "file_too_big"))
        return
    token = tokens.put({"file_id": file_id, "ext": ext})  # только ссылка на файл — качаем по кнопке
    await message.reply(t(lang, "photo_actions"), reply_markup=photo_actions_kb(lang, token))


async def _send_result(call: CallbackQuery, bot: Bot, lang: str, source: dict, action: str) -> None:
    status = await call.message.answer(t(lang, f"photo_wait_{action}"))
    try:
        data = await read_file(bot, source["file_id"])
        if action == "bg":
            png = await asyncio.to_thread(remove_background, data)
            caption, filename = t(lang, "photo_cutout_done"), "no_background.png"
        else:
            png, (width, height) = await asyncio.to_thread(enhance, data)
            caption, filename = t(lang, "photo_enhance_done", width=width, height=height), "enhanced.png"
        await call.message.answer_document(
            BufferedInputFile(png, filename=filename), caption=caption, disable_content_type_detection=True
        )
    except Exception:
        log.exception("Не удалось обработать фото (%s)", action)
        await call.message.answer(t(lang, "photo_failed"))
    finally:
        with suppress(TelegramBadRequest):
            await status.delete()


@router.callback_query(F.data.startswith("ph:"))
async def cb_photo_action(
    call: CallbackQuery,
    bot: Bot,
    lang: str,
    tokens: TokenStore,
    blobs: TokenStore,
    limits: Limits,
    is_admin: bool,
    db: Storage,
) -> None:
    _, action, token = ([*(call.data or "").split(":", 2), "", ""])[:3]
    source = tokens.get(token)
    if not source or action not in ("bg", "up", "em") or not isinstance(call.message, Message):
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    if not await heavy_ok(call, limits, lang, is_admin):
        return
    await call.answer()
    if action == "em":
        await make_emoji(call.message, bot, lang, source["file_id"], "static", source["ext"], "⭐", blobs)
        return
    await _send_result(call, bot, lang, source, action)
    await db.log_event("photo", call.from_user.id)
