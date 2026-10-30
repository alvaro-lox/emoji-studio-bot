"""Скачивание файлов с серверов Telegram."""

import asyncio
import io
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

log = logging.getLogger(__name__)


async def read_file(bot: Bot, file_id: str) -> bytes:
    """Файл целиком в память. Ошибки Telegram пробрасываются как есть."""
    buffer = io.BytesIO()
    await bot.download(file_id, destination=buffer)
    return buffer.getvalue()


async def download(bot: Bot, file_id: str, slots: asyncio.Semaphore, attempts: int = 3) -> bytes | None:
    """Скачивание с повторами: ждёт flood-wait, повторяет сетевые сбои.

    None — если файл недоступен (удалён, слишком большой) или все попытки исчерпаны.
    """
    async with slots:
        for attempt in range(attempts):
            try:
                return await read_file(bot, file_id)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramBadRequest as e:
                log.warning("Файл %s недоступен: %s", file_id, e)
                return None
            except Exception as e:
                log.warning("Ошибка скачивания %s (попытка %d): %s", file_id, attempt + 1, e)
                await asyncio.sleep(1 + attempt)
    return None
