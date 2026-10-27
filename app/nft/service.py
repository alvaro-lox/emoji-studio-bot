"""Загрузка, рендер и кэширование анимаций подарков."""

import asyncio
import logging
import time
from collections import OrderedDict, defaultdict
from pathlib import Path

import aiohttp

from app.config import Config
from app.nft.parser import GiftInfo, fetch_assets, fetch_gift
from app.nft.render import GiftStyle, RenderResult, make_gif, render_gift

log = logging.getLogger(__name__)

VARIANTS = ("f", "n")  # f — с фоном, n — без фона
CACHE_TTL = 7 * 24 * 3600
FALLBACK_TTL = 600


class GiftService:
    def __init__(self, http: aiohttp.ClientSession, config: Config):
        self.http = http
        self.config = config
        self.cache_dir = config.data_dir / "gifts"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._slots = asyncio.Semaphore(config.render_workers)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._gifts: OrderedDict[str, tuple[GiftInfo, bytes, bytes | None]] = OrderedDict()
        self._loaded: dict[str, float] = {}

    def cleanup(self) -> None:
        """Удаляет старые рендеры: в Telegram они уже лежат по file_id."""
        now = time.time()
        for path in self.cache_dir.iterdir():
            if path.is_file() and now - path.stat().st_mtime > CACHE_TTL:
                path.unlink(missing_ok=True)

    async def load(self, slug: str) -> tuple[GiftInfo, bytes, bytes | None]:
        key = slug.lower()
        async with self._locks[f"load:{key}"]:
            cached = self._gifts.get(key)
            # данные с запасного источника держим недолго — вдруг t.me уже отвечает
            if cached and (cached[0].source == "page" or time.monotonic() - self._loaded.get(key, 0) < FALLBACK_TTL):
                self._gifts.move_to_end(key)
                return cached
            info = await fetch_gift(self.http, slug)
            model, pattern = await fetch_assets(self.http, info)
            self._gifts[key] = (info, model, pattern)
            self._loaded[key] = time.monotonic()
            if len(self._gifts) > 200:
                self._gifts.popitem(last=False)
            return self._gifts[key]

    async def info(self, slug: str) -> GiftInfo:
        return (await self.load(slug))[0]

    async def fresh_info(self, slug: str) -> GiftInfo:
        """Без кэша — для проверки владельца."""
        info = await fetch_gift(self.http, slug)
        cached = self._gifts.get(slug.lower())
        if cached:
            self._gifts[slug.lower()] = (info, cached[1], cached[2])
        return info

    async def model(self, slug: str) -> bytes:
        return (await self.load(slug))[1]

    def _video_path(self, info: GiftInfo, variant: str, style: GiftStyle) -> Path:
        return self.cache_dir / f"{info.slug.lower()}_{variant}_{style.key()}.mp4"

    async def video(self, slug: str, variant: str, style: GiftStyle | None = None) -> RenderResult:
        style = style or GiftStyle()
        info, model, pattern = await self.load(slug)
        path = self._video_path(info, variant, style)
        async with self._locks[f"video:{path.name}"], self._slots:
            return await asyncio.to_thread(
                render_gift,
                info,
                model,
                pattern,
                path,
                scale=self.config.gift_scale,
                with_backdrop=variant == "f",
                font_path=self.config.font_path,
                style=style,
            )

    async def gif(self, slug: str, variant: str) -> Path:
        info = await self.info(slug)
        mp4 = self._video_path(info, variant, GiftStyle())
        if not mp4.exists():
            await self.video(slug, variant)
        gif = mp4.with_suffix(".gif")
        async with self._locks[f"gif:{gif.name}"]:
            if not gif.exists():
                async with self._slots:
                    await asyncio.to_thread(make_gif, mp4, gif)
        return gif
