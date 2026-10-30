"""Главное меню, режимы, язык, помощь, разделы."""

from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.common.i18n import LANGS, t
from app.common.icons import icons
from app.common.storage import MODES, Storage
from app.common.ui import keyboard

router = Router()

SECTION_STICKERS = "sec:stickers"
SECTION_GIFTS = "gifts"
SECTION_PHOTO = "sec:photo"

LANG_NAMES = {"ru": "🇷🇺 Русский", "en": "🇺🇸 English"}


def menu_text(lang: str) -> str:
    return t(lang, "menu")


def menu_kb(lang: str, is_admin: bool = False) -> InlineKeyboardMarkup:
    """Главное меню: разделы (цветные) и служебные кнопки (без цвета) — в одной строке стиль один."""
    return keyboard(
        [icons.button(t(lang, "btn_section_stickers"), "sticker", "primary", callback_data=SECTION_STICKERS)],
        [
            icons.button(t(lang, "btn_gifts"), "gift", "primary", callback_data=SECTION_GIFTS),
            icons.button(t(lang, "btn_section_photo"), "image", "primary", callback_data=SECTION_PHOTO),
        ],
        [
            icons.button(t(lang, "btn_lang"), "globe", callback_data="lang"),
            icons.button(t(lang, "btn_help"), "info", callback_data="help"),
        ],
        [icons.button(t(lang, "btn_admin"), "admin", "danger", callback_data="admin")] if is_admin else None,
    )


def stickers_text(lang: str, mode: str) -> str:
    return t(lang, "section_stickers", mode=t(lang, f"mode_{mode}"))


def stickers_kb(lang: str, mode: str) -> InlineKeyboardMarkup:
    """Раздел стикеров и эмодзи: режимы (выбранный — зелёный с галочкой) и свои эмодзи."""

    def mode_button(value: str, icon: str) -> InlineKeyboardButton:
        selected = mode == value
        label = t(lang, f"btn_mode_{value}") + (" ✓" if selected else "")
        return icons.button(label, icon, "success" if selected else "primary", callback_data=f"mode:{value}")

    return keyboard(
        [mode_button("id", "search"), mode_button("dl", "download"), mode_button("color", "brush")],
        [
            icons.button(t(lang, "btn_maker"), "magic", "primary", callback_data="maker"),
            icons.button(t(lang, "btn_mypack"), "folder_add", "primary", callback_data="mypack"),
        ],
        [icons.button(t(lang, "btn_back"), "back", callback_data="menu")],
    )


def gifts_kb(lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            icons.button(t(lang, "btn_collage"), "sparkles", "primary", callback_data="collage_help"),
            icons.button(t(lang, "btn_watches"), "bell", "primary", callback_data="watches"),
        ],
        [icons.button(t(lang, "btn_back"), "back", callback_data="menu")],
    )


def back_kb(lang: str) -> InlineKeyboardMarkup:
    return keyboard([icons.button(t(lang, "btn_back"), "back", callback_data="menu")])


def lang_kb(lang: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            icons.button(LANG_NAMES[code], style="primary" if code == lang else None, callback_data=f"setlang:{code}")
            for code in LANGS
        ],
        [icons.button(t(lang, "btn_back"), "back", callback_data="menu")],
    )


def commands(lang: str) -> list[BotCommand]:
    return [
        BotCommand(command="start", description=t(lang, "cmd_start")),
        BotCommand(command="language", description=t(lang, "cmd_language")),
        BotCommand(command="watches", description=t(lang, "cmd_watches")),
        BotCommand(command="help", description=t(lang, "cmd_help")),
    ]


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(commands("en"))
    await bot.set_my_commands(commands("ru"), language_code="ru")


async def _edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in e.message:  # под фото/анимацией текст не отредактировать
            await call.message.answer(text, reply_markup=markup)


@router.message(CommandStart())
async def cmd_start(message: Message, lang: str, is_admin: bool, state: FSMContext) -> None:
    await state.clear()
    await message.answer(menu_text(lang), reply_markup=menu_kb(lang, is_admin))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, lang: str, is_admin: bool, state: FSMContext) -> None:
    await state.clear()
    await message.answer(t(lang, "cancelled"), reply_markup=menu_kb(lang, is_admin))


@router.message(Command("myid"))
async def cmd_myid(message: Message, lang: str) -> None:
    await message.answer(t(lang, "myid", id=message.from_user.id))


@router.message(Command("help"))
async def cmd_help(message: Message, lang: str) -> None:
    await message.answer(t(lang, "help"), reply_markup=back_kb(lang))


@router.message(Command("language"))
async def cmd_language(message: Message, lang: str) -> None:
    await message.answer(t(lang, "choose_lang"), reply_markup=lang_kb(lang))


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, lang: str, is_admin: bool, state: FSMContext) -> None:
    await state.clear()
    await _edit(call, menu_text(lang), menu_kb(lang, is_admin))
    await call.answer()


@router.callback_query(F.data.startswith("mode:"))
async def cb_mode(call: CallbackQuery, lang: str, db: Storage) -> None:
    mode = call.data.removeprefix("mode:")
    if mode not in MODES:
        await call.answer()
        return
    await db.set_mode(call.from_user.id, mode)
    await _edit(call, stickers_text(lang, mode), stickers_kb(lang, mode))
    await call.answer(t(lang, f"mode_set_{mode}"))


@router.callback_query(F.data == SECTION_STICKERS)
async def cb_stickers(call: CallbackQuery, lang: str, mode: str) -> None:
    await _edit(call, stickers_text(lang, mode), stickers_kb(lang, mode))
    await call.answer()


@router.callback_query(F.data == SECTION_GIFTS)
async def cb_gifts(call: CallbackQuery, lang: str) -> None:
    await _edit(call, t(lang, "gifts_help"), gifts_kb(lang))
    await call.answer()


@router.callback_query(F.data == SECTION_PHOTO)
async def cb_photo(call: CallbackQuery, lang: str) -> None:
    await _edit(call, t(lang, "section_photo"), back_kb(lang))
    await call.answer()


@router.callback_query(F.data == "collage_help")
async def cb_collage_help(call: CallbackQuery, lang: str) -> None:
    markup = keyboard([icons.button(t(lang, "btn_back"), "back", callback_data=SECTION_GIFTS)])
    await _edit(call, t(lang, "collage_help"), markup)
    await call.answer()


@router.callback_query(F.data == "maker")
async def cb_maker(call: CallbackQuery, lang: str) -> None:
    markup = keyboard(
        [icons.button(t(lang, "btn_mypack"), "folder_add", "primary", callback_data="mypack")],
        [icons.button(t(lang, "btn_back"), "back", callback_data=SECTION_STICKERS)],
    )
    await _edit(call, t(lang, "maker_help"), markup)
    await call.answer()


@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery, lang: str) -> None:
    await _edit(call, t(lang, "help"), back_kb(lang))
    await call.answer()


@router.callback_query(F.data == "lang")
async def cb_lang(call: CallbackQuery, lang: str) -> None:
    await _edit(call, t(lang, "choose_lang"), lang_kb(lang))
    await call.answer()


@router.callback_query(F.data.startswith("setlang:"))
async def cb_setlang(call: CallbackQuery, bot: Bot, is_admin: bool, db: Storage) -> None:
    lang = call.data.removeprefix("setlang:")
    if lang not in LANGS:
        await call.answer()
        return
    await db.set_lang(call.from_user.id, lang)
    with suppress(TelegramBadRequest):
        await bot.set_my_commands(commands(lang), scope=BotCommandScopeChat(chat_id=call.message.chat.id))
    await _edit(call, menu_text(lang), menu_kb(lang, is_admin))
    await call.answer(t(lang, "lang_set"))
