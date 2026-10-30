"""Коллаж из нескольких подарков: карточка с живым превью, настройки кнопками, рендер в MP4 и GIF.

Сессия коллажа (список подарков + настройки) живёт в памяти под коротким токеном —
он же стоит в callback_data всех кнопок карточки.
"""

import logging
import secrets
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from app.common.files import read_file
from app.common.i18n import t
from app.common.icons import icons
from app.common.limits import Limits
from app.common.storage import Storage
from app.common.ui import heavy_ok, keyboard
from app.nft.collage import FRAMES, PALETTES, CollageStyle
from app.nft.handlers import send_gift
from app.nft.parser import GiftNotFound
from app.nft.service import GiftService
from app.nft.states import CollageForm

log = logging.getLogger(__name__)
router = Router()

MAX_SESSIONS = 1000
MAX_TITLE = 32
MAX_PHOTO = 15 * 1024 * 1024
SEPARATE_LIMIT = 5  # «каждый отдельно» — не больше стольких рендеров за раз
PALETTE_ORDER = ("auto", *PALETTES)
ASPECT_CODES = {"11": "1:1", "916": "9:16", "169": "16:9"}


@dataclass
class CollageSession:
    user_id: int
    slugs: list[str]
    style: CollageStyle = field(default_factory=CollageStyle)


_sessions: OrderedDict[str, CollageSession] = OrderedDict()


def _new_session(user_id: int, slugs: list[str]) -> str:
    token = secrets.token_urlsafe(6)
    _sessions[token] = CollageSession(user_id, slugs)
    if len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)
    return token


def _session(token: str, user_id: int) -> CollageSession | None:
    session = _sessions.get(token)
    if session and session.user_id == user_id:
        _sessions.move_to_end(token)
        return session
    return None


# ---------- предложение собрать коллаж ----------


async def offer_collage(message: Message, lang: str, slugs: list[str]) -> None:
    """На сообщение с несколькими подарками: коллаж или каждый отдельно."""
    token = _new_session(message.from_user.id, slugs)
    rows = [
        [
            icons.button(
                t(lang, "btn_collage_make", n=len(slugs)), "sparkles", "success", callback_data=f"cl:open:{token}"
            )
        ]
    ]
    if len(slugs) <= SEPARATE_LIMIT:
        rows.append(
            [icons.button(t(lang, "btn_collage_separate"), "gift", "primary", callback_data=f"cl:each:{token}")]
        )
    await message.reply(t(lang, "collage_offer", n=len(slugs)), reply_markup=keyboard(*rows))


# ---------- карточка настроек ----------


def _choice(lang: str, label_key: str, selected: bool, icon: str | None, data: str) -> InlineKeyboardButton:
    """Кнопка выбора: выбранная — зелёная с галочкой, остальные — синие."""
    label = t(lang, label_key) + (" ✓" if selected else "")
    return icons.button(label, icon, "success" if selected else "primary", callback_data=data)


def settings_kb(lang: str, token: str, style: CollageStyle) -> InlineKeyboardMarkup:
    def data(action: str, value: str = "") -> str:
        return f"cl:{action}:{token}:{value}" if value else f"cl:{action}:{token}"

    frame_icons = {"cards": "image", "frames": "crop", "circles": "smile", "puzzle": "sparkles"}
    frames = [
        _choice(lang, f"collage_frame_{name}", style.frame == name, frame_icons[name], data("frame", name))
        for name in FRAMES
    ]
    rows: list[list[InlineKeyboardButton]] = [frames[:2], frames[2:]]

    if style.frame == "frames":
        rows.append(
            [
                _choice(lang, "collage_gap_on", style.gap, None, data("gap", "1")),
                _choice(lang, "collage_gap_off", not style.gap, None, data("gap", "0")),
            ]
        )
    rows.append(
        [
            _choice(lang, "collage_tile_bg_on", style.gift_backdrop, None, data("tilebg", "1")),
            _choice(lang, "collage_tile_bg_off", not style.gift_backdrop, None, data("tilebg", "0")),
        ]
    )
    if style.needs_canvas_background:
        rows.append(
            [
                _choice(lang, "collage_bg_gradient", style.background == "gradient", None, data("bg", "gradient")),
                _choice(lang, "collage_bg_animated", style.background == "animated", None, data("bg", "animated")),
            ]
        )
        rows.append(
            [
                _choice(lang, "collage_bg_dark", style.background == "dark", None, data("bg", "dark")),
                _choice(lang, "collage_bg_photo", style.background == "photo", "image", data("bg", "photo")),
            ]
        )
        if style.background in ("gradient", "animated"):
            palette = t(lang, f"collage_palette_{style.palette}")
            rows.append(
                [
                    icons.button(
                        t(lang, "btn_collage_palette", name=palette), "drop", "primary", callback_data=data("pal")
                    )
                ]
            )
    rows.append(
        [
            _choice(lang, "collage_captions", style.captions, None, data("cap")),
            icons.button(
                t(lang, "btn_collage_title_clear" if style.title else "btn_collage_title"),
                "edit",
                "success" if style.title else "primary",
                callback_data=data("title"),
            ),
        ]
    )
    rows.append(
        [
            _choice(lang, f"aspect_{code}", style.aspect == aspect, None, data("aspect", code))
            for code, aspect in ASPECT_CODES.items()
        ]
    )
    rows.append([icons.button(t(lang, "btn_collage_render"), "magic", "success", callback_data=data("go"))])
    rows.append([icons.button(t(lang, "btn_close"), "cross", callback_data="close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text(lang: str, session: CollageSession) -> str:
    style = session.style
    return t(
        lang,
        "collage_settings",
        n=len(session.slugs),
        frame=t(lang, f"collage_frame_{style.frame}"),
        aspect=style.aspect,
        animated=t(lang, "collage_animated_note")
        if style.background == "animated" and style.needs_canvas_background
        else "",
    )


async def _send_settings(target: Message, lang: str, token: str, session: CollageSession, gifts: GiftService) -> None:
    preview = await gifts.collage_preview(session.slugs, session.style)
    await target.answer_photo(
        BufferedInputFile(preview, filename="collage.jpg"),
        caption=settings_text(lang, session),
        reply_markup=settings_kb(lang, token, session.style),
    )


async def _refresh_settings(
    call: CallbackQuery, lang: str, token: str, session: CollageSession, gifts: GiftService
) -> None:
    preview = await gifts.collage_preview(session.slugs, session.style)
    with suppress(TelegramBadRequest):
        await call.message.edit_media(
            InputMediaPhoto(
                media=BufferedInputFile(preview, filename="collage.jpg"), caption=settings_text(lang, session)
            ),
            reply_markup=settings_kb(lang, token, session.style),
        )


# ---------- кнопки ----------


@router.callback_query(F.data.startswith("cl:"))
async def cb_collage(
    call: CallbackQuery,
    bot: Bot,
    lang: str,
    state: FSMContext,
    gifts: GiftService,
    db: Storage,
    limits: Limits,
    is_admin: bool,
) -> None:
    _, action, token, value = ([*(call.data or "").split(":", 3), "", "", ""])[:4]
    session = _session(token, call.from_user.id)
    if session is None or not isinstance(call.message, Message):
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    style = session.style

    if action == "each":
        await call.answer()
        with suppress(TelegramBadRequest):
            await call.message.delete()
        for slug in session.slugs:
            if await heavy_ok(call, limits, lang, is_admin):
                await send_gift(bot, call.message.chat.id, call.from_user.id, slug, lang, gifts, db)
        return

    if action == "open":
        await call.answer(t(lang, "collage_loading"))
        with suppress(TelegramBadRequest):
            await call.message.delete()
        try:
            await _send_settings(call.message, lang, token, session, gifts)
        except GiftNotFound as e:
            await call.message.answer(t(lang, "gift_not_found", slug=escape(str(e))))
        except Exception:
            log.exception("Не удалось открыть коллаж")
            await call.message.answer(t(lang, "gift_failed"))
        return

    if action == "go":
        await _render(call, lang, token, session, gifts, db, limits, is_admin)
        return
    if action == "gif":
        await _send_gif(call, lang, session, gifts, limits, is_admin)
        return
    if action == "edit":
        await call.answer()
        await _send_settings(call.message, lang, token, session, gifts)
        return

    if action == "frame" and value in FRAMES:
        style.frame = value
    elif action == "gap":
        style.gap = value == "1"
    elif action == "tilebg":
        style.gift_backdrop = value == "1"
    elif action == "bg" and value == "photo":
        await state.set_state(CollageForm.photo)
        await state.update_data(collage=token)
        await call.message.answer(t(lang, "collage_ask_photo"))
        await call.answer()
        return
    elif action == "bg" and value in ("gradient", "animated", "dark"):
        style.background = value
    elif action == "pal":
        style.palette = PALETTE_ORDER[(PALETTE_ORDER.index(style.palette) + 1) % len(PALETTE_ORDER)]
    elif action == "cap":
        style.captions = not style.captions
    elif action == "title":
        if style.title:
            style.title = ""
        else:
            await state.set_state(CollageForm.title)
            await state.update_data(collage=token)
            await call.message.answer(t(lang, "collage_ask_title", n=MAX_TITLE))
            await call.answer()
            return
    elif action == "aspect" and value in ASPECT_CODES:
        style.aspect = ASPECT_CODES[value]
    else:
        await call.answer()
        return
    await call.answer()
    await _refresh_settings(call, lang, token, session, gifts)


async def _render(
    call: CallbackQuery,
    lang: str,
    token: str,
    session: CollageSession,
    gifts: GiftService,
    db: Storage,
    limits: Limits,
    is_admin: bool,
) -> None:
    if not await heavy_ok(call, limits, lang, is_admin):
        return
    await call.answer(t(lang, "gift_toast"))
    status = await call.message.answer(t(lang, "collage_wait", n=len(session.slugs)))
    try:
        result = await gifts.collage(session.slugs, session.style)
        markup = keyboard(
            [
                icons.button(t(lang, "btn_gif"), "gif", "primary", callback_data=f"cl:gif:{token}"),
                icons.button(t(lang, "btn_collage_edit"), "brush", "primary", callback_data=f"cl:edit:{token}"),
            ]
        )
        await call.message.answer_animation(
            FSInputFile(result.path, filename="collage.mp4"),
            width=result.width,
            height=result.height,
            duration=result.duration,
            reply_markup=markup,
        )
        await db.log_event("collage", call.from_user.id)
    except Exception:
        log.exception("Не удалось собрать коллаж")
        await call.message.answer(t(lang, "gift_failed"))
    finally:
        with suppress(TelegramBadRequest):
            await status.delete()


async def _send_gif(
    call: CallbackQuery, lang: str, session: CollageSession, gifts: GiftService, limits: Limits, is_admin: bool
) -> None:
    if not await heavy_ok(call, limits, lang, is_admin):
        return
    await call.answer(t(lang, "gif_wait"))
    try:
        path = await gifts.collage_gif(session.slugs, session.style)
        await call.message.answer_document(
            FSInputFile(path, filename="collage.gif"), disable_content_type_detection=True
        )
    except Exception:
        log.exception("Не удалось сделать GIF коллажа")
        await call.message.answer(t(lang, "gift_failed"))


# ---------- ввод: своё фото и заголовок ----------


@router.message(StateFilter(CollageForm.photo), F.photo | F.document.mime_type.startswith("image/"))
async def on_collage_photo(message: Message, bot: Bot, lang: str, state: FSMContext, gifts: GiftService) -> None:
    token = (await state.get_data()).get("collage", "")
    await state.clear()
    session = _session(token, message.from_user.id)
    if session is None:
        await message.answer(t(lang, "expired"))
        return
    file = message.photo[-1] if message.photo else message.document
    if file is None or (file.file_size or 0) > MAX_PHOTO:
        await message.answer(t(lang, "file_too_big"))
        return
    session.style.photo = await read_file(bot, file.file_id)
    session.style.background = "photo"
    await _send_settings(message, lang, token, session, gifts)


@router.message(StateFilter(CollageForm.title), F.text, ~F.text.startswith("/"))
async def on_collage_title(message: Message, lang: str, state: FSMContext, gifts: GiftService) -> None:
    token = (await state.get_data()).get("collage", "")
    await state.clear()
    session = _session(token, message.from_user.id)
    if session is None:
        await message.answer(t(lang, "expired"))
        return
    session.style.title = (message.text or "").strip()[:MAX_TITLE]
    await _send_settings(message, lang, token, session, gifts)


@router.message(StateFilter(CollageForm.photo, CollageForm.title), ~F.text.startswith("/"))
async def on_collage_wrong_input(message: Message, lang: str, state: FSMContext) -> None:
    waiting_photo = await state.get_state() == CollageForm.photo.state
    await message.answer(t(lang, "collage_ask_photo" if waiting_photo else "collage_ask_title", n=MAX_TITLE))
