"""Подарки: анимация 60 fps, оформление (формат, свой фон, подпись), GIF, .tgs, слежка."""

import logging
from collections import OrderedDict
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Document,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    Message,
    PhotoSize,
)

from app.common.files import read_file
from app.common.i18n import t
from app.common.icons import icons
from app.common.limits import Limits
from app.common.storage import Storage
from app.common.ui import heavy_ok, kb, menu_button_kb
from app.config import Config
from app.nft.parser import GiftInfo, GiftNotFound
from app.nft.render import GiftStyle, format_number
from app.nft.service import VARIANTS, GiftService
from app.nft.states import GiftStyleForm

log = logging.getLogger(__name__)
router = Router()

ASPECT_CODES = {"32": "3:2", "11": "1:1", "916": "9:16"}
MAX_CUSTOM_TEXT = 40
MAX_BACKGROUND = 15 * 1024 * 1024

# Оформление, которое пользователь собирает для подарка: (user_id, slug) → GiftStyle
_styles: OrderedDict[tuple[int, str], GiftStyle] = OrderedDict()
MAX_STYLES = 2000  # в стиле может лежать картинка фона — не копим бесконечно


def _style(user_id: int, slug: str) -> GiftStyle:
    key = (user_id, slug.lower())
    if key not in _styles:
        _styles[key] = GiftStyle()
        if len(_styles) > MAX_STYLES:
            _styles.popitem(last=False)
    _styles.move_to_end(key)
    return _styles[key]


def gift_caption(info: GiftInfo, lang: str) -> str:
    lines = [t(lang, "gift_caption", title=escape(info.title), number=format_number(info.number))]
    for key, attr in (("gift_model", info.model), ("gift_backdrop", info.backdrop), ("gift_symbol", info.symbol)):
        if attr:
            rarity = f" · {escape(attr.rarity)}" if attr.rarity else ""
            lines.append(f"{t(lang, key)}: <b>{escape(attr.name)}</b>{rarity}")
    if info.owner:
        lines.append(f"{t(lang, 'gift_owner')}: {escape(info.owner)}")
    return "\n".join(lines)


def gift_kb(info: GiftInfo, variant: str, lang: str, watching: bool = False) -> InlineKeyboardMarkup:
    slug = info.slug
    toggle = None
    if info.has_backdrop:
        toggle = (
            icons.button(t(lang, "btn_nobg"), "image", callback_data=f"g:n:{slug}")
            if variant == "f"
            else icons.button(t(lang, "btn_withbg"), "sparkles", callback_data=f"g:f:{slug}")
        )
    style = icons.button(t(lang, "btn_style"), "brush", "primary", callback_data=f"gs:{slug}")
    watch = (
        icons.button(t(lang, "btn_unwatch"), "bell_off", callback_data=f"uw:{slug}")
        if watching
        else icons.button(t(lang, "btn_watch"), "bell", "danger", callback_data=f"w:{slug}")
    )
    return kb(
        [b for b in (toggle, style) if b],
        [
            icons.button(t(lang, "btn_gif"), "gif", "success", callback_data=f"gif:{variant}:{slug}"),
            icons.button(t(lang, "btn_tgs"), "sticker", "success", callback_data=f"tgs:{slug}"),
        ],
        [watch],
        [icons.button(t(lang, "btn_open"), "link", url=info.link)],
    )


async def _animation(slug: str, variant: str, gifts: GiftService, db: Storage):
    """file_id из кэша или свежий рендер (FSInputFile + размеры)."""
    if file_id := await db.cache_get(f"gift:{slug.lower()}:{variant}"):
        return file_id, {}
    result = await gifts.video(slug, variant)
    media = FSInputFile(result.path, filename=f"{slug}.mp4")
    return media, {"width": result.width, "height": result.height, "duration": result.duration}


async def _remember(db: Storage, slug: str, variant: str, message: Message | bool, info: GiftInfo) -> None:
    # упрощённую версию с запасного источника не запоминаем — потом нарисуем полную
    if isinstance(message, Message) and info.source == "page":
        sent = message.animation or message.document or message.video
        if sent:
            await db.cache_set(f"gift:{slug.lower()}:{variant}", sent.file_id)


async def send_gift(
    bot: Bot, chat_id: int, user_id: int, slug: str, lang: str, gifts: GiftService, db: Storage
) -> None:
    status = await bot.send_message(chat_id, t(lang, "gift_wait", slug=escape(slug)))
    try:
        info = await gifts.info(slug)
        variant = "f" if info.has_backdrop else "n"
        media, extra = await _animation(info.slug, variant, gifts, db)
        watching = await db.is_watching(user_id, info.slug)
        sent = await bot.send_animation(
            chat_id,
            media,
            caption=gift_caption(info, lang),
            reply_markup=gift_kb(info, variant, lang, watching),
            **extra,
        )
        await _remember(db, info.slug, variant, sent, info)
        await db.log_event("gift", user_id)
    except GiftNotFound:
        await status.edit_text(t(lang, "gift_not_found", slug=escape(slug)))
        return
    except Exception:
        log.exception("Не удалось отправить подарок %s", slug)
        with suppress(TelegramBadRequest):
            await status.edit_text(t(lang, "gift_failed"))
        return
    with suppress(TelegramBadRequest):
        await status.delete()


def _parse(data: str, parts: int) -> list[str] | None:
    items = data.split(":", parts - 1)
    return items if len(items) == parts else None


@router.callback_query(F.data.startswith("g:"))
async def cb_toggle_backdrop(call: CallbackQuery, lang: str, gifts: GiftService, db: Storage) -> None:
    parsed = _parse(call.data or "", 3)
    if not parsed or parsed[1] not in VARIANTS:
        await call.answer()
        return
    _, variant, slug = parsed
    await call.answer(t(lang, "gift_toast"))
    try:
        info = await gifts.info(slug)
        media, _ = await _animation(slug, variant, gifts, db)
        watching = await db.is_watching(call.from_user.id, slug)
        edited = await call.message.edit_media(
            InputMediaAnimation(media=media, caption=gift_caption(info, lang)),
            reply_markup=gift_kb(info, variant, lang, watching),
        )
        await _remember(db, slug, variant, edited, info)
    except Exception:
        log.exception("Не удалось переключить фон подарка %s", slug)
        await call.message.answer(t(lang, "gift_failed"))


@router.callback_query(F.data.startswith("gif:"))
async def cb_gif(
    call: CallbackQuery, lang: str, gifts: GiftService, db: Storage, limits: Limits, is_admin: bool
) -> None:
    parsed = _parse(call.data or "", 3)
    if not parsed or parsed[1] not in VARIANTS:
        await call.answer()
        return
    _, variant, slug = parsed
    key = f"giffile:{slug.lower()}:{variant}"
    cached = await db.cache_get(key)
    if not cached and not await heavy_ok(call, limits, lang, is_admin):
        return
    await call.answer(t(lang, "gif_wait"))
    try:
        document = cached or FSInputFile(await gifts.gif(slug, variant), filename=f"{slug}.gif")
        sent = await call.message.answer_document(document, disable_content_type_detection=True)
        if sent.document:
            await db.cache_set(key, sent.document.file_id)
    except Exception:
        log.exception("Не удалось сделать GIF %s", slug)
        await call.message.answer(t(lang, "gift_failed"))


@router.callback_query(F.data.startswith("tgs:"))
async def cb_tgs(call: CallbackQuery, lang: str, gifts: GiftService, db: Storage) -> None:
    slug = call.data.removeprefix("tgs:")
    await call.answer()
    key = f"tgs:{slug.lower()}"
    try:
        document = await db.cache_get(key) or BufferedInputFile(await gifts.model(slug), filename=f"{slug}.tgs")
        sent = await call.message.answer_document(document, disable_content_type_detection=True)
        if sent.document:
            await db.cache_set(key, sent.document.file_id)
    except Exception:
        log.exception("Не удалось отправить модель %s", slug)
        await call.message.answer(t(lang, "gift_failed"))


# ---------- оформление ----------


def style_text(lang: str, info: GiftInfo, style: GiftStyle) -> str:
    text_label = {
        "default": t(lang, "style_text_default"),
        "none": t(lang, "style_text_none"),
        "custom": f"«{escape(style.custom_text)}»",
    }[style.text]
    return t(
        lang,
        "style_title",
        title=escape(info.title),
        number=format_number(info.number),
        aspect=style.aspect,
        background=t(lang, "style_bg_custom") if style.background else t(lang, "style_bg_gift"),
        text=text_label,
    )


def style_kb(lang: str, slug: str, style: GiftStyle) -> InlineKeyboardMarkup:
    def mark(label: str, on: bool) -> str:
        return f"{label} ✓" if on else label

    aspects = [
        icons.button(
            mark(t(lang, f"aspect_{code}"), style.aspect == aspect),
            style="primary" if style.aspect == aspect else None,
            callback_data=f"ga:{code}:{slug}",
        )
        for code, aspect in ASPECT_CODES.items()
    ]
    background = (
        icons.button(t(lang, "btn_bg_remove"), "trash", callback_data=f"gbr:{slug}")
        if style.background
        else icons.button(t(lang, "btn_bg_custom"), "image", callback_data=f"gbg:{slug}")
    )
    return kb(
        aspects,
        [background],
        [icons.button(mark(t(lang, "btn_text_custom"), style.text == "custom"), "edit", callback_data=f"gtx:{slug}")],
        [
            icons.button(mark(t(lang, "btn_text_default"), style.text == "default"), callback_data=f"gdt:{slug}"),
            icons.button(mark(t(lang, "btn_text_none"), style.text == "none"), callback_data=f"gnt:{slug}"),
        ],
        [icons.button(t(lang, "btn_render"), "magic", "success", callback_data=f"gap:{slug}")],
        [icons.button(t(lang, "btn_close"), "cross", callback_data="close")],
    )


async def _show_style(call_or_msg, lang: str, info: GiftInfo, style: GiftStyle, edit: bool) -> None:
    text, markup = style_text(lang, info, style), style_kb(lang, info.slug, style)
    if edit:
        with suppress(TelegramBadRequest):
            await call_or_msg.edit_text(text, reply_markup=markup)
    else:
        await call_or_msg.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("gs:"))
async def cb_style_open(call: CallbackQuery, lang: str, gifts: GiftService) -> None:
    slug = call.data.removeprefix("gs:")
    try:
        info = await gifts.info(slug)
    except Exception:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    await _show_style(call.message, lang, info, _style(call.from_user.id, slug), edit=False)
    await call.answer()


@router.callback_query(F.data.regexp(r"^(ga|gbr|gdt|gnt):"))
async def cb_style_change(call: CallbackQuery, lang: str, gifts: GiftService) -> None:
    action, _, rest = call.data.partition(":")
    if action == "ga":
        code, _, slug = rest.partition(":")
        if code not in ASPECT_CODES:
            await call.answer()
            return
    else:
        slug = rest
    style = _style(call.from_user.id, slug)
    if action == "ga":
        style.aspect = ASPECT_CODES[code]
    elif action == "gbr":
        style.background = None
    elif action == "gdt":
        style.text = "default"
    elif action == "gnt":
        style.text = "none"
    info = await gifts.info(slug)
    await _show_style(call.message, lang, info, style, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("gbg:"))
async def cb_style_background(call: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(GiftStyleForm.background)
    await state.update_data(slug=call.data.removeprefix("gbg:"))
    await call.message.answer(t(lang, "ask_background"))
    await call.answer()


@router.callback_query(F.data.startswith("gtx:"))
async def cb_style_text(call: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(GiftStyleForm.text)
    await state.update_data(slug=call.data.removeprefix("gtx:"))
    await call.message.answer(t(lang, "ask_text", n=MAX_CUSTOM_TEXT))
    await call.answer()


@router.message(StateFilter(GiftStyleForm.background), F.photo | F.document)
async def on_style_background(message: Message, bot: Bot, lang: str, state: FSMContext, gifts: GiftService) -> None:
    file: PhotoSize | Document
    if message.photo:
        file = message.photo[-1]
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file = message.document
    else:
        await message.answer(t(lang, "ask_background"))
        return
    if (file.file_size or 0) > MAX_BACKGROUND:
        await message.answer(t(lang, "file_too_big"))
        return
    slug = (await state.get_data()).get("slug", "")
    await state.clear()
    style = _style(message.from_user.id, slug)
    style.background = await read_file(bot, file.file_id)
    await _show_style(message, lang, await gifts.info(slug), style, edit=False)


@router.message(StateFilter(GiftStyleForm.text), F.text, ~F.text.startswith("/"))
async def on_style_text(message: Message, lang: str, state: FSMContext, gifts: GiftService) -> None:
    slug = (await state.get_data()).get("slug", "")
    await state.clear()
    style = _style(message.from_user.id, slug)
    style.text, style.custom_text = "custom", message.text.strip()[:MAX_CUSTOM_TEXT]
    await _show_style(message, lang, await gifts.info(slug), style, edit=False)


@router.message(StateFilter(GiftStyleForm.background, GiftStyleForm.text), ~F.text.startswith("/"))
async def on_style_wrong_input(message: Message, lang: str, state: FSMContext) -> None:
    current = await state.get_state()
    await message.answer(
        t(lang, "ask_background" if current == GiftStyleForm.background.state else "ask_text", n=MAX_CUSTOM_TEXT)
    )


@router.callback_query(F.data.startswith("gap:"))
async def cb_style_render(
    call: CallbackQuery, lang: str, gifts: GiftService, limits: Limits, is_admin: bool, db: Storage
) -> None:
    slug = call.data.removeprefix("gap:")
    if not await heavy_ok(call, limits, lang, is_admin):
        return
    style = _style(call.from_user.id, slug)
    await call.answer(t(lang, "gift_toast"))
    status = await call.message.answer(t(lang, "gift_wait", slug=escape(slug)))
    try:
        info = await gifts.info(slug)
        variant = "f" if info.has_backdrop else "n"
        result = await gifts.video(slug, variant, style)
        markup = kb(
            [icons.button(t(lang, "btn_style_again"), "brush", "primary", callback_data=f"gs:{slug}")],
            [icons.button(t(lang, "btn_open"), "link", url=info.link)],
        )
        await call.message.answer_animation(
            FSInputFile(result.path, filename=f"{slug}_{style.aspect.replace(':', 'x')}.mp4"),
            width=result.width,
            height=result.height,
            duration=result.duration,
            caption=gift_caption(info, lang),
            reply_markup=markup,
        )
        await db.log_event("gift", call.from_user.id)
    except Exception:
        log.exception("Не удалось отрисовать оформление %s", slug)
        await call.message.answer(t(lang, "gift_failed"))
    finally:
        with suppress(TelegramBadRequest):
            await status.delete()


@router.callback_query(F.data == "close")
async def cb_close(call: CallbackQuery) -> None:
    with suppress(TelegramBadRequest):
        await call.message.delete()
    await call.answer()


# ---------- слежка ----------


def watches_view(lang: str, rows: list[tuple[str, str | None]]) -> tuple[str, InlineKeyboardMarkup]:
    if not rows:
        return t(lang, "watches_empty"), menu_button_kb(lang)
    buttons = [
        [
            icons.button(title or slug, "gift", url=f"https://t.me/nft/{slug}"),
            icons.button(t(lang, "btn_unwatch_short"), "bell_off", callback_data=f"uwl:{slug}"),
        ]
        for slug, title in rows
    ]
    buttons.append([icons.button(t(lang, "btn_menu"), "home", callback_data="menu")])
    return t(lang, "watches_title", n=len(rows)), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("watches"))
async def cmd_watches(message: Message, lang: str, db: Storage) -> None:
    text, markup = watches_view(lang, await db.user_watches(message.from_user.id))
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "watches")
async def cb_watches(call: CallbackQuery, lang: str, db: Storage) -> None:
    text, markup = watches_view(lang, await db.user_watches(call.from_user.id))
    with suppress(TelegramBadRequest):
        await call.message.edit_text(text, reply_markup=markup)
    await call.answer()


async def _refresh_gift_kb(call: CallbackQuery, lang: str, gifts: GiftService, slug: str, watching: bool) -> bool:
    """Обновляет кнопку слежки под анимацией подарка. False — это было другое сообщение."""
    message = call.message
    if not getattr(message, "animation", None) or not message.reply_markup:
        return False
    variant = (
        "f"
        if any(b.callback_data == f"g:n:{slug}" for row in message.reply_markup.inline_keyboard for b in row)
        else "n"
    )
    info = await gifts.info(slug)
    if not info.has_backdrop:
        variant = "n"
    with suppress(TelegramBadRequest):
        await message.edit_reply_markup(reply_markup=gift_kb(info, variant, lang, watching))
    return True


@router.callback_query(F.data.startswith("w:"))
async def cb_watch(call: CallbackQuery, lang: str, gifts: GiftService, db: Storage, config: Config) -> None:
    slug = call.data.removeprefix("w:")
    if len(await db.user_watches(call.from_user.id)) >= config.max_watches:
        await call.answer(t(lang, "watch_limit", n=config.max_watches), show_alert=True)
        return
    try:
        info = await gifts.info(slug)
    except Exception:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    await db.add_watch(call.from_user.id, slug)
    if not await db.get_owner(slug):
        await db.set_owner(slug, info.title, info.owner)
    await _refresh_gift_kb(call, lang, gifts, slug, True)
    await call.answer(t(lang, "watch_on"), show_alert=True)


@router.callback_query(F.data.startswith("uw:"))
async def cb_unwatch(call: CallbackQuery, lang: str, gifts: GiftService, db: Storage) -> None:
    slug = call.data.removeprefix("uw:")
    await db.remove_watch(call.from_user.id, slug)
    await _refresh_gift_kb(call, lang, gifts, slug, False)
    await call.answer(t(lang, "watch_off"))


@router.callback_query(F.data.startswith("uwl:"))
async def cb_unwatch_from_list(call: CallbackQuery, lang: str, db: Storage) -> None:
    await db.remove_watch(call.from_user.id, call.data.removeprefix("uwl:"))
    text, markup = watches_view(lang, await db.user_watches(call.from_user.id))
    with suppress(TelegramBadRequest):
        await call.message.edit_text(text, reply_markup=markup)
    await call.answer(t(lang, "watch_off"))
