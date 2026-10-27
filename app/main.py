"""Запуск бота: python -m app"""

import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app import menu
from app.admin import handlers as admin
from app.common.icons import IconFallback, icons
from app.common.limits import Limits
from app.common.middlewares import UserMiddleware
from app.common.storage import Storage, TokenStore
from app.config import load_config
from app.nft import handlers as nft
from app.nft.service import GiftService
from app.nft.watcher import watch_loop
from app.stickers.downloader import Downloader
from app.stickers.handlers import content, maker, recolor

log = logging.getLogger(__name__)

# Порядок важен: команды и ожидание ввода (FSM) раньше общих обработчиков текста и медиа
ROUTERS = (menu.router, admin.router, nft.router, recolor.router, content.router, maker.router)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


async def main() -> None:
    config = load_config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bot = Bot(config.token, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    bot.session.middleware(IconFallback())
    db = Storage(config.data_dir / "bot.db")
    await db.open()
    http = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    watcher: asyncio.Task | None = None

    try:
        me = await bot.me()
        log.info("Бот @%s запущен, админов: %d", me.username, len(config.admin_ids))
        await icons.load(bot, config.icon_pack, config.custom_emoji)
        await menu.setup_commands(bot)

        gifts = GiftService(http, config)
        gifts.cleanup()
        limits = Limits(config.heavy_limit, config.heavy_window)
        watcher = asyncio.create_task(watch_loop(bot, db, gifts, config.watch_interval))

        dp = Dispatcher()
        dp.update.outer_middleware(UserMiddleware(db, config.admin_ids, limits))
        dp.include_routers(*ROUTERS)
        await dp.start_polling(
            bot,
            db=db,
            config=config,
            limits=limits,
            downloader=Downloader(bot, delay=config.batch_delay, db=db),
            tokens=TokenStore(),
            blobs=TokenStore(limit=40),  # тяжёлые данные: исходники эмодзи, перекрашенные паки
            gifts=gifts,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        if watcher:
            watcher.cancel()
        await http.close()
        await db.close()
        await bot.session.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
