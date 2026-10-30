"""Загрузка, рендер и кэширование анимаций подарков."""

import asyncio
import hashlib
import io
import json
import logging
import time
import zipfile
from collections import OrderedDict, defaultdict
from pathlib import Path

import aiohttp

from app.config import Config
from app.nft.collage import CollageGift, CollageStyle, preview_collage, render_collage
from app.nft.parser import GiftInfo, fetch_assets, fetch_gift
from app.nft.render import GiftStyle, RenderResult, make_gif, render_gift, render_model_png, render_still

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
        self._renders: dict[str, RenderResult] = {}  # готовые файлы: второй раз не рендерим

    async def _render_once(self, path: Path, render) -> RenderResult:
        """Один рендер на файл: параллельные запросы ждут первый, повторные берут готовое."""
        async with self._locks[f"render:{path.name}"]:
            done = self._renders.get(path.name)
            if done and path.exists():
                return done
            async with self._slots:
                result = await asyncio.to_thread(render)
            self._renders[path.name] = result
            return result

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
        return await self._render_once(
            path,
            lambda: render_gift(
                info,
                model,
                pattern,
                path,
                scale=self.config.gift_scale,
                with_backdrop=variant == "f",
                font_path=self.config.font_path,
                style=style,
            ),
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

    # ---------- скачать подарок целиком ----------

    async def archive(self, slug: str) -> bytes:
        """Zip: анимация MP4, модель .tgs, кадр с фоном и модель без фона в PNG, данные подарка."""
        info, model, pattern = await self.load(slug)
        variant = "f" if info.has_backdrop else "n"
        video = await self.video(slug, variant)
        async with self._slots:
            still, model_png = await asyncio.gather(
                asyncio.to_thread(render_still, info, model, pattern, self.config.gift_scale, info.has_backdrop),
                asyncio.to_thread(render_model_png, model),
            )
        details = {
            "title": info.title,
            "number": info.number,
            "link": info.link,
            "owner": info.owner,
            "model": info.model.__dict__ if info.model else None,
            "backdrop": info.backdrop.__dict__ if info.backdrop else None,
            "symbol": info.symbol.__dict__ if info.symbol else None,
            "colors": {"center": info.center_color, "edge": info.edge_color, "pattern": info.pattern_color},
        }
        name = info.slug.lower()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(video.path, f"{name}/{name}.mp4", compress_type=zipfile.ZIP_STORED)
            archive.writestr(f"{name}/{name}.tgs", model, compress_type=zipfile.ZIP_STORED)
            archive.writestr(f"{name}/{name}.png", still, compress_type=zipfile.ZIP_STORED)
            archive.writestr(f"{name}/{name}_model.png", model_png, compress_type=zipfile.ZIP_STORED)
            if pattern:
                archive.writestr(f"{name}/{name}_symbol.webp", pattern, compress_type=zipfile.ZIP_STORED)
            archive.writestr(f"{name}/info.json", json.dumps(details, ensure_ascii=False, indent=2))
        return buffer.getvalue()

    # ---------- коллаж ----------

    async def collage_gifts(self, slugs: list[str]) -> list[CollageGift]:
        """Все подарки грузятся параллельно; порядок как в сообщении."""
        loaded = await asyncio.gather(*(self.load(slug) for slug in slugs))
        return [CollageGift(info, model, pattern) for info, model, pattern in loaded]

    def _collage_path(self, slugs: list[str], style: CollageStyle) -> Path:
        digest = hashlib.sha1("|".join(s.lower() for s in slugs).encode()).hexdigest()[:10]
        return self.cache_dir / f"collage_{digest}_{style.key()}.mp4"

    async def collage_preview(self, slugs: list[str], style: CollageStyle) -> bytes:
        gifts = await self.collage_gifts(slugs)
        return await asyncio.to_thread(preview_collage, gifts, style, self.config.font_path)

    async def collage(self, slugs: list[str], style: CollageStyle) -> RenderResult:
        gifts = await self.collage_gifts(slugs)
        path = self._collage_path(slugs, style)
        return await self._render_once(path, lambda: render_collage(gifts, style, path, self.config.font_path))

    async def collage_gif(self, slugs: list[str], style: CollageStyle) -> Path:
        mp4 = self._collage_path(slugs, style)
        if not mp4.exists():
            await self.collage(slugs, style)
        gif = mp4.with_suffix(".gif")
        async with self._locks[f"gif:{gif.name}"]:
            if not gif.exists():
                async with self._slots:
                    await asyncio.to_thread(make_gif, mp4, gif, 720)
        return gif
