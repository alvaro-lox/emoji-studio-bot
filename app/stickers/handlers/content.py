"""Всё, что присылает пользователь: эмодзи, стикеры, ссылки на паки и подарки."""

import asyncio
import json
import logging
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, CopyTextButton, InlineKeyboardMarkup, Message

from app.common.i18n import t
from app.common.icons import icons
from app.common.limits import Limits
from app.common.storage import Storage, TokenStore
from app.common.ui import heavy_ok, keyboard, menu_button_kb, plain, tg_emoji
from app.config import Config
from app.nft.collage import MAX_GIFTS
from app.nft.collage_handlers import offer_collage
from app.nft.handlers import send_gift
from app.nft.service import GiftService
from app.stickers.core import LIBS, custom_emojis, emoji_code, find_gift_slugs, find_pack_names, sticker_ext
from app.stickers.downloader import Downloader
from app.stickers.handlers.recolor import palette_kb, send_palette
from app.stickers.packs import preview_image, render_grid, set_link

log = logging.getLogger(__name__)
router = Router()

MAX_ID_LINES = 40
MAX_CODE_ITEMS = 50
PACK_IDS_PER_MESSAGE = 50
MAX_PACKS_PER_MESSAGE = 3
COPY_TEXT_LIMIT = 256
TOKEN_PREFIX = "t."

_alt_cache: dict[str, str] = {}


# ---------- входящие сообщения ----------


@router.message(F.sticker)
async def on_sticker(message: Message, lang: str, mode: str, downloader: Downloader, tokens: TokenStore) -> None:
    sticker = message.sticker
    if mode == "dl":
        downloader.add(message.chat.id, lang, items=[sticker])
        return
    if mode == "color":
        await send_palette(message, lang, {"items": [sticker]}, tokens)
        return
    info_row = []
    if len(sticker.file_id) <= COPY_TEXT_LIMIT:
        copy_id = CopyTextButton(text=sticker.file_id)
        info_row.append(icons.button(t(lang, "btn_copy_file_id"), "copy", "primary", copy_text=copy_id))
    download_row = [
        icons.button(t(lang, "btn_download"), "download", "success", callback_data=f"dlt:{tokens.put([sticker])}")
    ]
    if sticker.set_name:
        key = downloader.pack_key(sticker.set_name)
        info_row.append(icons.button(t(lang, "btn_pack_ids"), "search", "primary", callback_data=f"pids:{key}"))
        download_row.append(icons.button(t(lang, "btn_zip"), "archive", "success", callback_data=f"pack:{key}"))
    rows = [info_row, download_row] if info_row else [download_row]
    rows.append(
        [
            icons.button(
                t(lang, "btn_recolor"), "brush", "danger", callback_data=f"rcp:{tokens.put({'items': [sticker]})}"
            )
        ]
    )
    text = t(
        lang,
        "sticker_card",
        emoji=sticker.emoji or "",
        pack=escape(sticker.set_name or "—"),
        kind=f".{sticker_ext(sticker)}",
        file_id=sticker.file_id,
    )
    await message.reply(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(F.text | F.caption)
async def on_text(
    message: Message,
    bot: Bot,
    lang: str,
    mode: str,
    is_admin: bool,
    downloader: Downloader,
    tokens: TokenStore,
    gifts: GiftService,
    db: Storage,
    limits: Limits,
    config: Config,
) -> None:
    text = message.text or message.caption
    packs = find_pack_names(text)[:MAX_PACKS_PER_MESSAGE]
    slugs = find_gift_slugs(text)[:MAX_GIFTS]
    if message.caption is not None and not (packs or slugs):
        # картинка/видео с подписью — это заготовка эмодзи, подпись задаёт значок
        raise SkipHandler()
    emojis = custom_emojis(text, message.entities or message.caption_entities)
    if not (emojis or packs or slugs):
        await message.answer(t(lang, "nothing_found"), reply_markup=menu_button_kb(lang))
        return

    for name in packs:
        if await heavy_ok(message, limits, lang, is_admin):
            await send_pack_preview(message, bot, lang, mode, name, downloader, tokens, db, config)
    if packs and mode == "dl":
        downloader.add(message.chat.id, lang, packs=packs)
    if emojis:
        if mode == "dl":
            downloader.add(message.chat.id, lang, items=[emoji_id for emoji_id, _ in emojis])
        elif mode == "color":
            await send_palette(message, lang, {"items": [emoji_id for emoji_id, _ in emojis]}, tokens)
        else:
            await send_id_card(message, bot, lang, emojis, downloader, tokens)
    if len(slugs) > 1:  # несколько подарков — предлагаем коллаж или каждый отдельно
        await offer_collage(message, lang, slugs)
    elif slugs and await heavy_ok(message, limits, lang, is_admin):
        await send_gift(bot, message.chat.id, message.from_user.id, slugs[0], lang, gifts, db)


# ---------- превью пака ----------


async def send_pack_preview(
    message: Message,
    bot: Bot,
    lang: str,
    mode: str,
    name: str,
    downloader: Downloader,
    tokens: TokenStore,
    db: Storage,
    config: Config,
) -> None:
    status = await message.answer(t(lang, "preview_wait", name=escape(name)))
    try:
        sticker_set = await bot.get_sticker_set(name=name)
    except TelegramBadRequest:
        await status.edit_text(t(lang, "dl_nothing") + "\n⚠️ " + t(lang, "dl_err_pack", name=escape(name)))
        return
    try:
        slots = asyncio.Semaphore(8)
        images = await asyncio.gather(*(preview_image(bot, s, slots) for s in sticker_set.stickers))
        kind = t(lang, "kind_emoji" if sticker_set.sticker_type == "custom_emoji" else "kind_stickers")
        link = set_link(name, sticker_set.sticker_type).removeprefix("https://")
        subtitle = f"{kind} · {len(sticker_set.stickers)} · {link}"
        grid = await asyncio.to_thread(render_grid, list(images), sticker_set.title, subtitle, config.font_path)
    except Exception:
        log.exception("Не удалось нарисовать превью %s", name)
        await status.edit_text(t(lang, "error"))
        return

    target_token = tokens.put({"pack": name})
    zip_button = icons.button(
        t(lang, "btn_zip"), "download", "success", callback_data=f"pack:{downloader.pack_key(name)}"
    )
    open_button = icons.button(t(lang, "btn_open_pack"), "link", url=set_link(name, sticker_set.sticker_type))
    if mode == "color":
        markup = palette_kb(lang, target_token, extra=[[zip_button], [open_button]])
    else:
        ids_button = icons.button(
            t(lang, "btn_pack_ids"), "search", "primary", callback_data=f"pids:{downloader.pack_key(name)}"
        )
        recolor_button = icons.button(t(lang, "btn_recolor"), "brush", "danger", callback_data=f"rcp:{target_token}")
        markup = keyboard([ids_button] if mode == "id" else None, [zip_button], [recolor_button], [open_button])
    caption = t(
        lang,
        "preview_caption",
        title=escape(sticker_set.title),
        kind=kind,
        n=len(sticker_set.stickers),
        name=escape(name),
    )
    if mode == "color":
        caption += "\n\n" + t(lang, "palette_hint")
    elif mode == "dl":
        caption += "\n\n" + t(lang, "preview_zip_soon")
    photo = BufferedInputFile(grid, filename=f"{name}.png")
    try:
        await message.answer_photo(photo, caption=caption, reply_markup=markup)
    except TelegramBadRequest:  # слишком большая картинка для фото — шлём файлом
        await message.answer_document(photo, caption=caption, reply_markup=markup)
    await db.log_event("preview", message.from_user.id)
    with suppress(TelegramBadRequest):
        await status.delete()


# ---------- ID эмодзи ----------


async def send_id_card(
    message: Message, bot: Bot, lang: str, emojis, downloader: Downloader, tokens: TokenStore
) -> None:
    for emoji_id, alt in emojis:
        _alt_cache[emoji_id] = alt

    if len(emojis) == 1:
        emoji_id, alt = emojis[0]
        set_name = None
        with suppress(TelegramBadRequest):
            found = await bot.get_custom_emoji_stickers(custom_emoji_ids=[emoji_id])
            set_name = found[0].set_name if found else None
        code_row = [icons.button(t(lang, "btn_code"), "code", "primary", callback_data=f"code:{emoji_id}")]
        download_row = [icons.button(t(lang, "btn_download"), "download", "success", callback_data=f"dl:{emoji_id}")]
        if set_name:
            key = downloader.pack_key(set_name)
            code_row.append(icons.button(t(lang, "btn_pack_ids"), "search", "primary", callback_data=f"pids:{key}"))
            download_row.append(icons.button(t(lang, "btn_zip"), "archive", "success", callback_data=f"pack:{key}"))
        rows = [
            [
                icons.button(t(lang, "btn_original", alt=alt), "link", "primary", copy_text=CopyTextButton(text=alt)),
                icons.button(t(lang, "btn_copy_id"), "copy", "primary", copy_text=CopyTextButton(text=emoji_id)),
            ],
            code_row,
            download_row,
        ]
        rows.append(
            [
                icons.button(
                    t(lang, "btn_recolor"), "brush", "danger", callback_data=f"rcp:{tokens.put({'items': [emoji_id]})}"
                )
            ]
        )
        text = t(lang, "id_line", emoji=tg_emoji(emoji_id, alt), id=emoji_id)
    else:
        lines = [
            f"{n}. {tg_emoji(emoji_id, alt)} <code>{emoji_id}</code>"
            for n, (emoji_id, alt) in enumerate(emojis[:MAX_ID_LINES], 1)
        ]
        if len(emojis) > MAX_ID_LINES:
            lines.append(t(lang, "id_more", n=len(emojis) - MAX_ID_LINES))
        ref = TOKEN_PREFIX + tokens.put(list(emojis))
        rows = [
            [icons.button(t(lang, "btn_code_all"), "code", "primary", callback_data=f"code:{ref}")],
            [
                icons.button(
                    t(lang, "btn_download_all"), "download", "success", callback_data=f"dlt:{ref[len(TOKEN_PREFIX) :]}"
                )
            ],
            [
                icons.button(
                    t(lang, "btn_recolor"),
                    "brush",
                    "danger",
                    callback_data=f"rcp:{tokens.put({'items': [e for e, _ in emojis]})}",
                )
            ],
        ]
        text = t(lang, "id_many", n=len(emojis), lines="\n".join(lines))
    await message.reply(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# ---------- код для библиотек ----------


async def resolve_ref(bot: Bot, ref: str, tokens: TokenStore) -> list[tuple[str, str]] | None:
    if ref.startswith(TOKEN_PREFIX):
        return tokens.get(ref[len(TOKEN_PREFIX) :])
    if not ref.isdigit():
        return None
    if ref not in _alt_cache:
        with suppress(TelegramBadRequest):
            found = await bot.get_custom_emoji_stickers(custom_emoji_ids=[ref])
            if found:
                _alt_cache[ref] = found[0].emoji or "⭐"
    return [(ref, _alt_cache.get(ref, "⭐"))]


def chooser(lang: str, ref: str, items: list[tuple[str, str]]) -> tuple[str, InlineKeyboardMarkup]:
    if len(items) == 1:
        head = f"{tg_emoji(*items[0])} <code>{items[0][0]}</code>"
    else:
        head = "".join(tg_emoji(emoji_id, alt) for emoji_id, alt in items[:20]) + f" ({len(items)})"
    names = list(LIBS.items())
    rows = [
        [icons.button(name, style="primary", callback_data=f"lib:{key}:{ref}") for key, name in names[i : i + 2]]
        for i in range(0, len(names), 2)
    ]
    return f"{head}\n\n{t(lang, 'choose_lib')}", InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("code:"))
async def cb_code(call: CallbackQuery, bot: Bot, lang: str, tokens: TokenStore) -> None:
    ref = call.data.removeprefix("code:")
    items = await resolve_ref(bot, ref, tokens)
    if not items:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    text, markup = chooser(lang, ref, items)
    await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("libs:"))
async def cb_libs(call: CallbackQuery, bot: Bot, lang: str, tokens: TokenStore) -> None:
    ref = call.data.removeprefix("libs:")
    items = await resolve_ref(bot, ref, tokens)
    if not items:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    text, markup = chooser(lang, ref, items)
    with suppress(TelegramBadRequest):
        await call.message.edit_text(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("lib:"))
async def cb_lib(call: CallbackQuery, bot: Bot, lang: str, tokens: TokenStore) -> None:
    parts = call.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in LIBS:
        await call.answer()
        return
    _, lib, ref = parts
    items = await resolve_ref(bot, ref, tokens)
    if not items:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    code = emoji_code(lib, items[:MAX_CODE_ITEMS])  # иначе код не влезет в сообщение
    text = f'{t(lang, "code_title", lib=LIBS[lib])}\n<pre><code class="language-python">{escape(code)}</code></pre>'
    rows = []
    if len(code) <= COPY_TEXT_LIMIT:
        rows.append([icons.button(t(lang, "btn_copy"), "copy", "success", copy_text=CopyTextButton(text=code))])
    rows.append([icons.button(t(lang, "btn_libs"), "back", callback_data=f"libs:{ref}")])
    with suppress(TelegramBadRequest):
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


# ---------- скачивание по кнопкам ----------


@router.callback_query(F.data.startswith("dl:"))
async def cb_download_one(call: CallbackQuery, lang: str, downloader: Downloader) -> None:
    downloader.add(call.message.chat.id, lang, items=[call.data.removeprefix("dl:")])
    await call.answer("📥")


@router.callback_query(F.data.startswith("dlt:"))
async def cb_download_token(call: CallbackQuery, lang: str, downloader: Downloader, tokens: TokenStore) -> None:
    items = tokens.get(call.data.removeprefix("dlt:"))
    if not items:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    downloader.add(call.message.chat.id, lang, items=[i[0] if isinstance(i, tuple) else i for i in items])
    await call.answer("📥")


@router.callback_query(F.data.startswith("pack:"))
async def cb_pack(call: CallbackQuery, lang: str, downloader: Downloader, limits: Limits, is_admin: bool) -> None:
    name = downloader.pack_by_key(call.data.removeprefix("pack:"))
    if not name:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    if not await heavy_ok(call, limits, lang, is_admin):
        return
    await call.answer(t(lang, "dl_pack_wait", name=name))
    downloader.add(call.message.chat.id, lang, packs=[name])


# ---------- все ID пака ----------


def pack_ids_json(sticker_set) -> bytes:
    custom = sticker_set.sticker_type == "custom_emoji"
    items = [
        {
            "index": i,
            "emoji": s.emoji,
            **({"custom_emoji_id": s.custom_emoji_id} if custom else {}),
            "file_id": s.file_id,
            "file_unique_id": s.file_unique_id,
            "format": sticker_ext(s),
        }
        for i, s in enumerate(sticker_set.stickers, 1)
    ]
    data = {"name": sticker_set.name, "title": sticker_set.title, "type": sticker_set.sticker_type, "items": items}
    return json.dumps(data, ensure_ascii=False, indent=2).encode()


@router.callback_query(F.data.startswith("pids:"))
async def cb_pack_ids(call: CallbackQuery, bot: Bot, lang: str, downloader: Downloader) -> None:
    key = call.data.removeprefix("pids:")
    name = downloader.pack_by_key(key)
    if not name:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    try:
        sticker_set = await bot.get_sticker_set(name=name)
    except TelegramBadRequest:
        await call.answer(plain(t(lang, "dl_err_pack", name=name)), show_alert=True)
        return
    await call.answer()

    stickers = sticker_set.stickers
    if sticker_set.sticker_type == "custom_emoji":
        lines = [
            f"{n}. {tg_emoji(s.custom_emoji_id or '', s.emoji or '⭐')} <code>{s.custom_emoji_id}</code>"
            for n, s in enumerate(stickers, 1)
        ]
        for start in range(0, len(lines), PACK_IDS_PER_MESSAGE):
            head = (
                t(lang, "pack_ids_title", title=escape(sticker_set.title), n=len(stickers)) + "\n\n"
                if start == 0
                else ""
            )
            await call.message.answer(head + "\n".join(lines[start : start + PACK_IDS_PER_MESSAGE]))
        caption = t(lang, "pack_ids_file", n=len(stickers))
    else:
        caption = t(lang, "pack_ids_file_stickers", title=escape(sticker_set.title), n=len(stickers))
    await call.message.answer_document(
        BufferedInputFile(pack_ids_json(sticker_set), filename=f"{name}_ids.json"),
        caption=caption,
        reply_markup=keyboard(
            [icons.button(t(lang, "btn_zip"), "download", "success", callback_data=f"pack:{key}")],
            [icons.button(t(lang, "btn_open_pack"), "link", url=set_link(name, sticker_set.sticker_type))],
        ),
    )
