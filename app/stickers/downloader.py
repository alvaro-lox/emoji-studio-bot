"""Очередь запросов по чатам, скачивание стикеров/эмодзи и отправка zip."""

import asyncio
import hashlib
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

from app.common.files import download
from app.common.i18n import t
from app.common.icons import icons
from app.stickers.core import ZipEntry, build_zips, safe_name, sticker_ext

log = logging.getLogger(__name__)

MAX_ZIP_BYTES = 45 * 1024 * 1024  # бот может отправить файл до 50 МБ
EMOJI_CHUNK = 200  # максимум id за один getCustomEmojiStickers
PROGRESS_EVERY = 2.0  # секунд между обновлениями прогресса
MAX_PACK_BUTTONS = 8
MAX_ERROR_LINES = 5


@dataclass
class Batch:
    lang: str = "en"
    # Sticker (присланный стикер) или str (custom_emoji_id) — в порядке получения
    items: list = field(default_factory=list)
    packs: list[str] = field(default_factory=list)
    timer: asyncio.Task | None = None


class Downloader:
    """Копит всё, что пользователь прислал подряд, и через `delay` секунд тишины
    отдаёт одним архивом."""

    def __init__(self, bot: Bot, delay: float = 2.5, concurrency: int = 8, db=None):
        self.bot = bot
        self.db = db  # Storage для статистики (необязательно)
        self.delay = delay
        self._slots = asyncio.Semaphore(concurrency)
        self._batches: dict[int, Batch] = {}
        self._tasks: set[asyncio.Task] = set()
        self._pack_keys: dict[str, str] = {}

    def add(self, chat_id: int, lang: str, items=(), packs=()) -> None:
        batch = self._batches.setdefault(chat_id, Batch())
        batch.lang = lang
        batch.items.extend(items)
        for name in packs:
            if name not in batch.packs:
                batch.packs.append(name)
        if batch.timer:
            batch.timer.cancel()
        batch.timer = self._spawn(self._fire(chat_id))

    def pack_key(self, name: str) -> str:
        # callback_data ограничен 64 байтами, а имя пака может быть длиной 64
        key = hashlib.sha1(name.encode()).hexdigest()[:16]
        self._pack_keys[key] = name
        return key

    def pack_by_key(self, key: str) -> str | None:
        return self._pack_keys.get(key)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _fire(self, chat_id: int) -> None:
        await asyncio.sleep(self.delay)
        # После pop новые сообщения попадут в новую пачку и этот таймер не отменят
        batch = self._batches.pop(chat_id)
        try:
            await self.process(chat_id, batch)
        except Exception:
            log.exception("Не удалось обработать пачку для чата %s", chat_id)
            with suppress(Exception):
                await self.bot.send_message(chat_id, t(batch.lang, "error"))

    async def process(self, chat_id: int, batch: Batch) -> None:
        lang = batch.lang
        status = await self.bot.send_message(chat_id, t(lang, "dl_collect"))
        errors: list[str] = []
        jobs: list[tuple[str, object]] = []  # (путь в архиве, Sticker)

        for name in batch.packs:
            try:
                sticker_set = await self.bot.get_sticker_set(name=name)
            except TelegramBadRequest:
                errors.append(t(lang, "dl_err_pack", name=escape(name)))
                continue
            folder = safe_name(sticker_set.name)
            jobs += [(f"{folder}/{i:03d}.{sticker_ext(s)}", s) for i, s in enumerate(sticker_set.stickers, 1)]

        # Одиночные из паков, которые и так качаются целиком, не дублируем
        singles = [s for s in await self._resolve(batch.items, errors, lang) if s.set_name not in batch.packs]
        jobs += [(f"{i:03d}.{sticker_ext(s)}", s) for i, s in enumerate(singles, 1)]

        if not jobs:
            await self._edit(status, self._with_errors(t(lang, "dl_nothing"), errors, lang))
            return

        done = 0
        last_progress = time.monotonic()

        async def fetch(path: str, sticker) -> ZipEntry | None:
            nonlocal done, last_progress
            data = await self._download(sticker.file_id)
            done += 1
            if time.monotonic() - last_progress > PROGRESS_EVERY:
                last_progress = time.monotonic()
                await self._edit(status, t(lang, "dl_progress", done=done, total=len(jobs)))
            return ZipEntry(path, data) if data is not None else None

        entries = [e for e in await asyncio.gather(*(fetch(p, s) for p, s in jobs)) if e]
        if len(entries) < len(jobs):
            errors.append(t(lang, "dl_err_files", n=len(jobs) - len(entries)))
        if not entries:
            await self._edit(status, self._with_errors(t(lang, "dl_failed"), errors, lang))
            return

        await self._edit(status, t(lang, "dl_pack"))
        parts = build_zips(entries, MAX_ZIP_BYTES)
        if len(batch.packs) == 1 and not singles:
            base = safe_name(batch.packs[0])
        else:
            base = f"stickers_{datetime.now():%Y%m%d_%H%M%S}"
        caption = self._with_errors(t(lang, "dl_done", n=len(entries)), errors, lang)
        markup = self._pack_offers(singles, batch.packs, lang)

        for n, data in enumerate(parts, 1):
            last = n == len(parts)
            filename = f"{base}.zip" if len(parts) == 1 else f"{base}_part{n}.zip"
            await self.bot.send_document(
                chat_id,
                BufferedInputFile(data, filename=filename),
                caption=caption if last else None,
                reply_markup=markup if last else None,
            )
        if self.db:
            await self.db.log_event("zip", chat_id)
        with suppress(TelegramBadRequest):
            await self.bot.delete_message(chat_id, status.message_id)

    async def _resolve(self, items: list, errors: list[str], lang: str) -> list:
        """Превращает custom_emoji_id в Sticker, убирает повторы, сохраняет порядок."""
        ids = list(dict.fromkeys(i for i in items if isinstance(i, str)))
        by_id = {}
        for start in range(0, len(ids), EMOJI_CHUNK):
            try:
                found = await self.bot.get_custom_emoji_stickers(custom_emoji_ids=ids[start : start + EMOJI_CHUNK])
            except TelegramBadRequest as e:
                log.warning("getCustomEmojiStickers: %s", e)
                continue
            by_id.update({s.custom_emoji_id: s for s in found})
        if len(by_id) < len(ids):
            errors.append(t(lang, "dl_err_emoji", n=len(ids) - len(by_id)))

        result, seen = [], set()
        for item in items:
            sticker = by_id.get(item) if isinstance(item, str) else item
            if sticker is not None and sticker.file_unique_id not in seen:
                seen.add(sticker.file_unique_id)
                result.append(sticker)
        return result

    async def _download(self, file_id: str) -> bytes | None:
        return await download(self.bot, file_id, self._slots)

    def _pack_offers(self, singles: list, downloaded_packs: list[str], lang: str) -> InlineKeyboardMarkup | None:
        names = list(dict.fromkeys(s.set_name for s in singles if s.set_name and s.set_name not in downloaded_packs))
        if not names:
            return None
        buttons = [
            [
                icons.button(
                    t(lang, "dl_pack_btn", name=name), "archive", "success", callback_data=f"pack:{self.pack_key(name)}"
                )
            ]
            for name in names[:MAX_PACK_BUTTONS]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    async def _edit(self, message, text: str) -> None:
        with suppress(TelegramBadRequest):
            await self.bot.edit_message_text(text, chat_id=message.chat.id, message_id=message.message_id)

    @staticmethod
    def _with_errors(head: str, errors: list[str], lang: str) -> str:
        lines = [f"⚠️ {e}" for e in errors[:MAX_ERROR_LINES]]
        if len(errors) > MAX_ERROR_LINES:
            lines.append(t(lang, "dl_more", n=len(errors) - MAX_ERROR_LINES))
        return "\n".join([head, *lines])
