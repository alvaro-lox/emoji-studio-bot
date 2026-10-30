"""Общие помощники хендлеров."""

import re

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.common.i18n import t
from app.common.icons import TG_EMOJI_RE, icons
from app.common.limits import Limits

TAG_RE = re.compile(r"<[^>]+>")


def plain(text: str) -> str:
    """HTML → обычный текст (для всплывающих уведомлений callback)."""
    return TAG_RE.sub("", TG_EMOJI_RE.sub(r"\1", text))


def tg_emoji(emoji_id: str, alt: str) -> str:
    # Повторить чужой премиум-эмодзи можно при тех же условиях, что и наши иконки
    return f'<tg-emoji emoji-id="{emoji_id}">{alt}</tg-emoji>' if icons.enabled else alt


def keyboard(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


def menu_button_kb(lang: str) -> InlineKeyboardMarkup:
    return keyboard([icons.button(t(lang, "btn_menu"), "home", callback_data="menu")])


async def heavy_ok(event: Message | CallbackQuery, limits: Limits, lang: str, is_admin: bool) -> bool:
    """Лимит на тяжёлые действия. False — пользователь уже предупреждён."""
    if is_admin:
        return True
    if event.from_user is None:  # анонимный админ канала — лимитировать некого
        return True
    wait = limits.heavy.hit(event.from_user.id)
    if not wait:
        return True
    text = t(lang, "slow_down", sec=wait)
    if isinstance(event, CallbackQuery):
        await event.answer(plain(text), show_alert=True)
    else:
        await event.answer(text)
    return False
