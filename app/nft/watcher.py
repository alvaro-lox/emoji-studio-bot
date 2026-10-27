"""Фоновая проверка владельцев подарков, за которыми следят пользователи."""

import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from app.common.i18n import t
from app.common.icons import icons
from app.common.storage import Storage
from app.nft.parser import PAGE_URL, GiftNotFound
from app.nft.service import GiftService

log = logging.getLogger(__name__)


async def check_once(bot: Bot, db: Storage, gifts: GiftService, pause: float = 3.0) -> int:
    """Проверяет все подарки из слежки. Возвращает число отправленных уведомлений."""
    sent = 0
    for slug in await db.watched_slugs():
        try:
            info = await gifts.fresh_info(slug)
        except GiftNotFound:
            continue
        except Exception as e:
            log.warning("Слежка: не удалось проверить %s: %s", slug, e)
            continue
        if info.source != "page":  # запасной источник не знает владельца — это не смена
            continue
        known = await db.get_owner(slug)
        await db.set_owner(slug, info.title, info.owner)
        if known and known[1] != info.owner:
            for user_id, lang in await db.watchers(slug):
                text = t(
                    lang,
                    "watch_changed",
                    title=escape(info.title),
                    number=escape(str(info.number or "")),
                    old=escape(known[1] or "—"),
                    new=escape(info.owner or "—"),
                )
                markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [icons.button(t(lang, "btn_open"), "link", url=PAGE_URL.format(slug=slug))],
                        [icons.button(t(lang, "btn_unwatch"), "bell_off", callback_data=f"uw:{slug}")],
                    ]
                )
                try:
                    await bot.send_message(user_id, text, reply_markup=markup)
                    sent += 1
                except TelegramForbiddenError:
                    await db.mark_blocked(user_id)
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except Exception as e:
                    log.warning("Слежка: не удалось уведомить %s: %s", user_id, e)
        await asyncio.sleep(pause)
    return sent


async def watch_loop(bot: Bot, db: Storage, gifts: GiftService, interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            sent = await check_once(bot, db, gifts)
            if sent:
                log.info("Слежка: отправлено уведомлений %d", sent)
        except Exception:
            log.exception("Слежка упала, попробую в следующий раз")
