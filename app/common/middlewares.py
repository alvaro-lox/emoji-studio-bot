"""Middleware апдейтов: пользователь, язык, режим, права, бан и антифлуд."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, Update

from app.common.i18n import default_lang, t
from app.common.limits import Limits
from app.common.storage import Storage

log = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    """Подкладывает в хендлеры lang, mode, is_admin, is_owner; отсекает забаненных и флуд.

    Владельцы — ADMIN_IDS из .env (могут назначать админов), админы — добавленные через панель."""

    def __init__(self, db: Storage, owner_ids: frozenset[int], limits: Limits):
        self.db = db
        self.owner_ids = owner_ids
        self.limits = limits

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):  # висит на dp.update, но пусть будет честно
            return await handler(event, data)
        user = data.get("event_from_user")
        lang, mode, is_owner, is_admin = "en", "dl", False, False
        if user:
            is_owner = user.id in self.owner_ids
            is_admin = is_owner or await self.db.is_admin(user.id)
            record = await self.db.get_user(user.id)
            if record is None:
                lang = default_lang(user.language_code)
                await self.db.add_user(user.id, lang, user.username)
            else:
                lang, mode = record.lang, record.mode
                await self.db.touch(user.id, user.username)
                if record.banned and not is_admin:
                    if isinstance(event.event, CallbackQuery):
                        await event.event.answer(t(lang, "banned"), show_alert=True)
                    return None
            if not is_admin and self.limits.flood.hit(user.id):
                if isinstance(event.event, CallbackQuery):
                    await event.event.answer(t(lang, "flood"))
                return None
        data.update(lang=lang, mode=mode, is_admin=is_admin, is_owner=is_owner)
        return await handler(event, data)
