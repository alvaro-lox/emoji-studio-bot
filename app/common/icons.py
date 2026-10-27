"""Иконки из пака премиум-эмодзи (по умолчанию tgiosicons) для текста и кнопок.

Кастомные эмодзи в сообщениях и кнопках бота работают, только если у владельца
бота есть Telegram Premium (или куплен username на Fragment). Если Telegram их
отклонит, IconFallback выключит иконки и повторит запрос с обычными эмодзи.
"""

import logging
import re

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

# имя: (номер файла в паке tgiosicons, запасной обычный эмодзи)
ICONS: dict[str, tuple[int, str]] = {
    "home": (245, "🏠"),
    "search": (38, "🔎"),
    "download": (88, "📥"),
    "archive": (101, "🗂"),
    "gift": (203, "🎁"),
    "gift_add": (389, "🎁"),
    "link": (51, "🔗"),
    "code": (36, "💻"),
    "copy": (76, "📋"),
    "globe": (334, "🌐"),
    "translate": (350, "🔤"),
    "back": (27, "⬅️"),
    "check": (65, "✅"),
    "cross": (64, "❌"),
    "info": (59, "ℹ️"),
    "settings": (9, "⚙️"),
    "sparkles": (364, "✨"),
    "gif": (125, "🎞"),
    "film": (252, "🎬"),
    "image": (69, "🖼"),
    "file": (74, "📄"),
    "crown": (383, "👑"),
    "smile": (206, "😊"),
    "sticker": (205, "🎭"),
    "verified": (399, "✅"),
    "hourglass": (356, "⏳"),
    "refresh": (28, "🔄"),
    "star": (204, "⭐"),
    "bulb": (271, "💡"),
    "new": (363, "🆕"),
    "ton": (338, "💎"),
    "brush": (306, "🖌"),
    "magic": (310, "🪄"),
    "pipette": (311, "🧪"),
    "drop": (302, "💧"),
    "eraser": (301, "🧽"),
    "crop": (323, "✂️"),
    "add": (330, "➕"),
    "stats": (331, "📊"),
    "megaphone": (105, "📣"),
    "bell": (113, "🔔"),
    "bell_off": (114, "🔕"),
    "lock": (68, "🔒"),
    "unlock": (67, "🔓"),
    "admin": (96, "🛡"),
    "users": (192, "👥"),
    "user": (201, "👤"),
    "edit": (30, "✏️"),
    "folder_add": (386, "📁"),
    "trash": (77, "🗑"),
    "clock": (141, "🕒"),
}

TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)


class Icons:
    def __init__(self) -> None:
        self.enabled = False
        self._ids: dict[str, str] = {}
        self._alts: dict[str, str] = {}
        self._fallback_by_id: dict[str, str] = {}

    async def load(self, bot: Bot, pack: str, enabled: bool) -> None:
        if not enabled:
            log.info("Кастомные эмодзи выключены (CUSTOM_EMOJI=0)")
            return
        try:
            sticker_set = await bot.get_sticker_set(name=pack)
        except TelegramBadRequest as e:
            log.warning("Пак иконок %s недоступен: %s — работаю с обычными эмодзи", pack, e.message)
            return
        for name, (index, fallback) in ICONS.items():
            if index <= len(sticker_set.stickers):
                sticker = sticker_set.stickers[index - 1]
                if sticker.custom_emoji_id:
                    self._ids[name] = sticker.custom_emoji_id
                    self._alts[name] = sticker.emoji or fallback
                    self._fallback_by_id[sticker.custom_emoji_id] = fallback
        self.enabled = bool(self._ids)
        log.info("Иконок загружено: %d из пака %s", len(self._ids), pack)

    def html(self, name: str) -> str:
        if self.enabled and name in self._ids:
            return f'<tg-emoji emoji-id="{self._ids[name]}">{self._alts[name]}</tg-emoji>'
        return ICONS[name][1]

    def __format__(self, spec: str) -> str:
        # позволяет писать в строках "{i:search} Поиск".format(i=icons)
        return self.html(spec) if spec else ""

    def button(self, text: str, icon: str | None = None, style: str | None = None, **kwargs) -> InlineKeyboardButton:
        if icon and self.enabled and icon in self._ids:
            return InlineKeyboardButton(text=text, icon_custom_emoji_id=self._ids[icon], style=style, **kwargs)
        if icon:
            text = f"{ICONS[icon][1]} {text}"
        return InlineKeyboardButton(text=text, style=style, **kwargs)

    def disable(self) -> None:
        self.enabled = False

    def strip(self, method) -> bool:
        """Убирает кастомные эмодзи из запроса. True, если было что убирать."""
        changed = False
        for attr in ("text", "caption"):
            value = getattr(method, attr, None)
            if isinstance(value, str) and TG_EMOJI_RE.search(value):
                setattr(method, attr, TG_EMOJI_RE.sub(r"\1", value))
                changed = True
        markup = getattr(method, "reply_markup", None)
        if isinstance(markup, InlineKeyboardMarkup):
            for row in markup.inline_keyboard:
                for button in row:
                    if button.icon_custom_emoji_id:
                        fallback = self._fallback_by_id.get(button.icon_custom_emoji_id, "")
                        button.text = f"{fallback} {button.text}".strip()
                        button.icon_custom_emoji_id = None
                        changed = True
        return changed


icons = Icons()


def is_icon_error(error: TelegramBadRequest) -> bool:
    message = error.message.upper()
    return any(word in message for word in ("EMOJI", "ICON", "DOCUMENT_INVALID", "ENTITY"))


class IconFallback(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as e:
            if not icons.enabled or not is_icon_error(e) or not icons.strip(method):
                raise
            log.warning("Telegram отклонил кастомные эмодзи (%s) — выключаю их", e.message)
            icons.disable()
            return await make_request(bot, method)
