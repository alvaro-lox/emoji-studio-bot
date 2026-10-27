"""Перекраска стикеров, эмодзи и целых паков + создание нового пака из результата."""

import asyncio
import logging
import time
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from app.common.files import download
from app.common.i18n import t
from app.common.icons import icons
from app.common.limits import Limits
from app.common.storage import Storage, TokenStore
from app.common.ui import heavy_ok, kb
from app.stickers.core import ZipEntry, build_zips, safe_name, sticker_ext
from app.stickers.downloader import MAX_ZIP_BYTES
from app.stickers.packs import create_set, input_sticker, new_set_name, set_link, sticker_format
from app.stickers.recolor import PRESETS, parse_hex, recolor_image, recolor_tgs
from app.stickers.states import RecolorForm

log = logging.getLogger(__name__)
router = Router()

EMOJI_CHUNK = 200


def palette_kb(lang: str, token: str, extra: list | None = None) -> InlineKeyboardMarkup:
    items = list(PRESETS.items())
    rows = [
        [
            icons.button(f"{emoji} {t(lang, f'color_{key}')}", style=style, callback_data=f"rc:{key}:{token}")
            for key, (_, emoji, style) in items[i : i + 2]
        ]
        for i in range(0, len(items), 2)
    ]
    rows.append([icons.button(t(lang, "btn_color_hex"), "pipette", callback_data=f"rch:{token}")])
    rows.extend(extra or [])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_palette(message: Message, lang: str, target: dict, tokens: TokenStore) -> None:
    """target: {"items": [Sticker | custom_emoji_id]} или {"pack": имя}."""
    await message.reply(t(lang, "palette_title"), reply_markup=palette_kb(lang, tokens.put(target)))


async def _resolve(bot: Bot, target: dict) -> tuple[list, str, str | None]:
    """(стикеры, название, тип набора)."""
    if "pack" in target:
        sticker_set = await bot.get_sticker_set(name=target["pack"])
        return list(sticker_set.stickers), sticker_set.title, sticker_set.sticker_type
    items = target["items"]
    ids = [i for i in items if isinstance(i, str)]
    by_id = {}
    for start in range(0, len(ids), EMOJI_CHUNK):
        with suppress(TelegramBadRequest):
            for s in await bot.get_custom_emoji_stickers(custom_emoji_ids=ids[start : start + EMOJI_CHUNK]):
                by_id[s.custom_emoji_id] = s
    stickers = [by_id.get(i) if isinstance(i, str) else i for i in items]
    stickers = [s for s in stickers if s is not None]
    types = {s.type for s in stickers}
    return stickers, "Recolor", types.pop() if len(types) == 1 else None


def _recolor_one(sticker, data: bytes, hex_color: str) -> tuple[bytes, bool]:
    """(данные, перекрашено ли). Видео-стикеры оставляем как есть."""
    if sticker.is_animated:
        return recolor_tgs(data, hex_color), True
    if not sticker.is_video:
        return recolor_image(data, hex_color), True
    return data, False


async def run_recolor(
    call_or_msg,
    bot: Bot,
    chat_id: int,
    user_id: int,
    lang: str,
    target: dict,
    target_token: str,
    hex_color: str,
    color_name: str,
    tokens: TokenStore,
    blobs: TokenStore,
    db: Storage,
) -> None:
    status = await bot.send_message(chat_id, t(lang, "recolor_wait"))
    try:
        stickers, title, sticker_type = await _resolve(bot, target)
    except TelegramBadRequest:
        await status.edit_text(t(lang, "recolor_failed"))
        return
    if not stickers:
        await status.edit_text(t(lang, "recolor_failed"))
        return

    slots = asyncio.Semaphore(8)
    done, last = 0, time.monotonic()

    async def work(index: int, sticker):
        nonlocal done, last
        data = await download(bot, sticker.file_id, slots)
        result = await asyncio.to_thread(_recolor_one, sticker, data, hex_color) if data else None
        done += 1
        if time.monotonic() - last > 2:
            last = time.monotonic()
            with suppress(TelegramBadRequest):
                await status.edit_text(t(lang, "recolor_progress", done=done, total=len(stickers)))
        return index, sticker, result

    results = [r for r in await asyncio.gather(*(work(i, s) for i, s in enumerate(stickers, 1))) if r[2]]
    if not results:
        await status.edit_text(t(lang, "recolor_failed"))
        return
    skipped = sum(1 for _, _, (_, changed) in results if not changed)
    again = icons.button(t(lang, "btn_color_again"), "brush", callback_data=f"rcp:{target_token}")

    if len(stickers) == 1:
        _, sticker, (data, _) = results[0]
        ext = sticker_ext(sticker)
        file_token = blobs.put({"data": data, "name": f"recolor_{color_name}.{ext}"})
        markup = kb([icons.button(t(lang, "btn_file"), "file", callback_data=f"rcf:{file_token}"), again])
        try:
            await bot.send_sticker(chat_id, BufferedInputFile(data, filename=f"sticker.{ext}"), reply_markup=markup)
        except TelegramBadRequest:  # эмодзи 100×100 стикером не отправить — шлём файлом
            await bot.send_document(
                chat_id,
                BufferedInputFile(data, filename=f"recolor_{color_name}.{ext}"),
                reply_markup=kb([again]),
                disable_content_type_detection=True,
            )
    else:
        folder = safe_name(target.get("pack") or "recolor")
        entries = [ZipEntry(f"{folder}/{i:03d}.{sticker_ext(s)}", data) for i, s, (data, _) in results]
        set_token = None
        if sticker_type:
            set_token = blobs.put(
                {
                    "title": f"{title} • {t(lang, f'color_{color_name}') if color_name in PRESETS else color_name}",
                    "type": sticker_type,
                    "stickers": [
                        (data, sticker_format(s), [s.emoji] if s.emoji else None, f"{i}.{sticker_ext(s)}")
                        for i, s, (data, _) in results
                    ],
                }
            )
        rows = []
        if set_token:
            rows.append(
                [icons.button(t(lang, "btn_make_pack"), "folder_add", "success", callback_data=f"rcs:{set_token}")]
            )
        rows.append([again])
        caption = t(lang, "recolor_done", n=len(results))
        if skipped:
            caption += "\n" + t(lang, "recolor_skipped", n=skipped)
        parts = build_zips(entries, MAX_ZIP_BYTES)
        for n, part in enumerate(parts, 1):
            last_part = n == len(parts)
            name = f"{folder}_{color_name}.zip" if len(parts) == 1 else f"{folder}_{color_name}_part{n}.zip"
            await bot.send_document(
                chat_id,
                BufferedInputFile(part, filename=name),
                caption=caption if last_part else None,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if last_part else None,
            )
    await db.log_event("recolor", user_id)
    with suppress(TelegramBadRequest):
        await status.delete()


@router.callback_query(F.data.startswith("rc:"))
async def cb_recolor(
    call: CallbackQuery,
    bot: Bot,
    lang: str,
    tokens: TokenStore,
    blobs: TokenStore,
    db: Storage,
    limits: Limits,
    is_admin: bool,
) -> None:
    _, key, token = ([*call.data.split(":", 2), "", ""])[:3]
    target = tokens.get(token)
    if key not in PRESETS or not target:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    if not await heavy_ok(call, limits, lang, is_admin):
        return
    await call.answer(t(lang, "recolor_toast"))
    await run_recolor(
        call, bot, call.message.chat.id, call.from_user.id, lang, target, token, PRESETS[key][0], key, tokens, blobs, db
    )


@router.callback_query(F.data.startswith("rch:"))
async def cb_recolor_hex(call: CallbackQuery, lang: str, state: FSMContext, tokens: TokenStore) -> None:
    token = call.data.removeprefix("rch:")
    if not tokens.get(token):
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    await state.set_state(RecolorForm.hex)
    await state.update_data(token=token)
    await call.message.answer(t(lang, "ask_hex"))
    await call.answer()


@router.message(StateFilter(RecolorForm.hex), F.text, ~F.text.startswith("/"))
async def on_recolor_hex(
    message: Message,
    bot: Bot,
    lang: str,
    state: FSMContext,
    tokens: TokenStore,
    blobs: TokenStore,
    db: Storage,
    limits: Limits,
    is_admin: bool,
) -> None:
    hex_color = parse_hex(message.text or "")
    if not hex_color:
        await message.answer(t(lang, "ask_hex"))
        return
    token = (await state.get_data()).get("token", "")
    await state.clear()
    target = tokens.get(token)
    if not target:
        await message.answer(t(lang, "expired"))
        return
    if not await heavy_ok(message, limits, lang, is_admin):
        return
    await run_recolor(
        message,
        bot,
        message.chat.id,
        message.from_user.id,
        lang,
        target,
        token,
        hex_color,
        hex_color.lstrip("#").lower(),
        tokens,
        blobs,
        db,
    )


@router.callback_query(F.data.startswith("rcp:"))
async def cb_palette_again(call: CallbackQuery, lang: str, tokens: TokenStore) -> None:
    token = call.data.removeprefix("rcp:")
    if not tokens.get(token):
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    await call.message.answer(t(lang, "palette_title"), reply_markup=palette_kb(lang, token))
    await call.answer()


@router.callback_query(F.data.startswith("rcf:"))
async def cb_recolor_file(call: CallbackQuery, lang: str, blobs: TokenStore) -> None:
    blob = blobs.get(call.data.removeprefix("rcf:"))
    if not blob:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    await call.answer()
    await call.message.answer_document(
        BufferedInputFile(blob["data"], filename=blob["name"]), disable_content_type_detection=True
    )


@router.callback_query(F.data.startswith("rcs:"))
async def cb_make_pack(
    call: CallbackQuery, bot: Bot, lang: str, blobs: TokenStore, limits: Limits, is_admin: bool
) -> None:
    blob = blobs.get(call.data.removeprefix("rcs:"))
    if not blob:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    if not await heavy_ok(call, limits, lang, is_admin):
        return
    await call.answer()
    me = await bot.me()
    name = new_set_name("c", me.username or "")
    status = await call.message.answer(t(lang, "pack_creating", done=0, total=len(blob["stickers"])))
    last = time.monotonic()

    async def progress(done: int, total: int) -> None:
        nonlocal last
        if time.monotonic() - last > 3:
            last = time.monotonic()
            with suppress(TelegramBadRequest):
                await status.edit_text(t(lang, "pack_creating", done=done, total=total))

    stickers = [input_sticker(data, fmt, emoji, filename) for data, fmt, emoji, filename in blob["stickers"]]
    try:
        added = await create_set(bot, call.from_user.id, name, blob["title"], blob["type"], stickers, progress)
    except TelegramBadRequest as e:
        log.warning("Не удалось создать пак %s: %s", name, e.message)
        with suppress(TelegramBadRequest):
            await status.edit_text(t(lang, "pack_failed", error=escape(e.message)))
        return
    with suppress(TelegramBadRequest):
        await status.edit_text(
            t(lang, "pack_created", title=escape(blob["title"][:64]), n=added),
            reply_markup=kb(
                [icons.button(t(lang, "btn_open_pack"), "archive", "success", url=set_link(name, blob["type"]))]
            ),
        )
