"""Данные NFT-подарка со страницы t.me/nft/<slug>: фон, узор, модель, атрибуты."""

import asyncio
import gzip
import json
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from html import unescape

import aiohttp

log = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[A-Za-z0-9]+-\d+$")
PAGE_URL = "https://t.me/nft/{slug}"
FRAGMENT_TGS_URL = "https://nft.fragment.com/gift/{slug}.tgs"

# Холст, в координатах которого Telegram размечает узор на странице подарка
CANVAS_W, CANVAS_H = 420, 280


class GiftNotFound(Exception):
    pass


@dataclass
class Attribute:
    name: str
    rarity: str | None = None


@dataclass
class PatternItem:
    x: float
    y: float
    scale: float
    opacity: float


@dataclass
class GiftInfo:
    slug: str
    title: str
    number: int | None
    model: Attribute | None = None
    backdrop: Attribute | None = None
    symbol: Attribute | None = None
    owner: str | None = None
    center_color: str | None = None
    edge_color: str | None = None
    pattern_color: str | None = None
    number_color: str = "#8c8f91"
    pattern_url: str | None = None
    model_url: str | None = None
    pattern: list[PatternItem] = field(default_factory=list)
    source: str = "page"  # page — страница t.me (всё есть), fragment — запасной источник (нет владельца и редкости)
    fragment_card: bytes | None = None  # .tgs-карточка Fragment, из неё берём модель и узор
    pattern_overlay: bytes | None = None  # готовый PNG-слой узора 512×512 из карточки Fragment

    @property
    def has_backdrop(self) -> bool:
        return bool(self.center_color and self.edge_color)

    @property
    def link(self) -> str:
        return PAGE_URL.format(slug=self.slug)


def _attr(cell: str) -> Attribute:
    rarity = re.search(r"<mark>([^<]*)</mark>", cell)
    name = unescape(re.sub(r"<[^>]+>", "", re.sub(r"<mark>.*?</mark>", "", cell))).strip()
    return Attribute(name, rarity.group(1).strip() if rarity else None)


def _owner(cell: str) -> str | None:
    """Имя владельца как на странице (+ @username, если есть ссылка)."""
    name = unescape(re.sub(r"<[^>]+>", "", cell)).strip()
    link = re.search(r'href="https?://t\.me/([A-Za-z0-9_]{4,})"', cell)
    if link and f"@{link.group(1)}" not in name:
        name = f"{name} (@{link.group(1)})" if name else f"@{link.group(1)}"
    return name or None


def parse_gift_page(html: str, slug: str) -> GiftInfo:
    if "tgme_gift_preview" not in html:
        raise GiftNotFound(slug) from None

    og_title = re.search(r'og:title" content="([^"]*)"', html)
    title_full = unescape(og_title.group(1)) if og_title else slug
    title, number = title_full, None
    if m := re.match(r"(.*?)\s*#(\d+)\s*$", title_full):
        title, number = m.group(1), int(m.group(2))

    info = GiftInfo(slug=slug, title=title, number=number)

    for th, td in re.findall(r"<tr><th>([^<]+)</th><td>(.*?)</td></tr>", html, re.S):
        key = th.strip().lower()
        if key in ("model", "backdrop", "symbol"):
            setattr(info, key, _attr(td))
        elif key == "owner":
            info.owner = _owner(td)

    stops = re.findall(r'<stop stop-color="(#[0-9a-fA-F]{6})" offset="(\d+)%"', html)
    if len(stops) >= 2:
        info.center_color, info.edge_color = stops[0][0], stops[-1][0]
    if m := re.search(r'flood-color="(#[0-9a-fA-F]{6})"', html):
        info.pattern_color = m.group(1)
    if m := re.search(r'tgme_gift_number_label" style="color:\s*(#[0-9a-fA-F]{6})', html):
        info.number_color = m.group(1)
    if m := re.search(r'id="giftPattern"[^>]*xlink:href="([^"]+)"', html):
        info.pattern_url = unescape(m.group(1))
    if m := re.search(r'<source type="application/x-tgsticker" srcset="([^"]+)"', html):
        info.model_url = unescape(m.group(1))

    for opacity, x, y, scale in re.findall(
        r'<g opacity="([\d.]+)" transform="translate\(([-\d.]+),\s*([-\d.]+)\)">\s*'
        r'<use xlink:href="#giftPattern" transform="scale\(([\d.]+)\)"',
        html,
    ):
        info.pattern.append(PatternItem(float(x), float(y), float(scale), float(opacity)))
    return info


async def _get(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        return await resp.read()


async def fetch_gift(session: aiohttp.ClientSession, slug: str, retries: int = 2, pause: float = 1.5) -> GiftInfo:
    """Страница t.me; при частых запросах она временно приходит пустой — тогда повторяем,
    а потом берём данные с Fragment. GiftNotFound — только если подарка нет и там."""
    if not SLUG_RE.match(slug):  # slug приходит и из callback_data — не доверяем
        raise GiftNotFound(slug) from None
    for attempt in range(retries + 1):
        try:
            html = (await _get(session, PAGE_URL.format(slug=slug))).decode("utf-8", "replace")
            return parse_gift_page(html, slug)
        except (GiftNotFound, aiohttp.ClientError, TimeoutError) as e:
            log.info("Страница подарка %s пустая/недоступна (попытка %d): %s", slug, attempt + 1, type(e).__name__)
            if attempt < retries:
                await asyncio.sleep(pause * (attempt + 1))
    return await fetch_from_fragment(session, slug)


FRAGMENT_JSON_URL = "https://nft.fragment.com/gift/{slug}.json"


def _fragment_colors(card: dict) -> tuple[str | None, str | None]:
    """Цвета градиента из слоя Background карточки Fragment: (центр, край)."""

    def hex_color(values) -> str:
        return "#" + "".join(f"{round(v * 255):02x}" for v in values)

    for layer in card.get("layers", []):
        if layer.get("nm") != "Background":
            continue
        stack = list(layer.get("shapes", []))
        while stack:
            item = stack.pop()
            if item.get("ty") == "gf":
                stops = item["g"]["k"]["k"]
                count = int(item["g"].get("p") or 2)
                return hex_color(stops[1:4]), hex_color(stops[(count - 1) * 4 + 1 : (count - 1) * 4 + 4])
            stack.extend(item.get("it", []))
    return None, None


def render_fragment_pattern(card_tgs: bytes, size: int = 512) -> bytes | None:
    """Слой узора карточки Fragment отдельной PNG-картинкой (без фона и модели)."""
    import io

    from PIL import Image
    from rlottie_python import LottieAnimation  # тяжёлый импорт — только когда нужен

    data = json.loads(gzip.decompress(card_tgs))
    layers = [layer for layer in data.get("layers", []) if layer.get("nm") == "Pattern"]
    if not layers:
        return None
    data["layers"] = layers
    anim = LottieAnimation.from_data(json.dumps(data))
    try:
        frame = anim.render_pillow_frame(0, width=size, height=size)
    finally:
        anim.lottie_animation_destroy()
    image = Image.frombytes("RGBa", frame.size, frame.tobytes()).convert("RGBA")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


async def fetch_from_fragment(session: aiohttp.ClientSession, slug: str) -> GiftInfo:
    """Карточка .tgs есть у всех подарков, JSON с названием и атрибутами — только у выведенных в TON."""
    try:
        card_tgs = await _get(session, FRAGMENT_TGS_URL.format(slug=slug.lower()))
        card = json.loads(gzip.decompress(card_tgs))
        extract_fragment_model(card_tgs)  # проверяем, что внутри действительно подарок
    except Exception:
        raise GiftNotFound(slug) from None

    name, _, num = slug.rpartition("-")
    info = GiftInfo(
        slug=slug,
        title=re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name[:1].upper() + name[1:]),
        number=int(num),
        source="fragment",
    )
    with suppress(aiohttp.ClientError, TimeoutError, ValueError):
        meta = json.loads(await _get(session, FRAGMENT_JSON_URL.format(slug=slug.lower())))
        if m := re.match(r"(.*?)\s*#(\d+)\s*$", meta.get("name") or ""):
            info.title, info.number = m.group(1), int(m.group(2))
        for attr in meta.get("attributes", []):
            key = str(attr.get("trait_type", "")).lower()
            if key in ("model", "backdrop", "symbol"):
                setattr(info, key, Attribute(str(attr.get("value", ""))))

    info.fragment_card = card_tgs
    info.center_color, info.edge_color = _fragment_colors(card)
    with suppress(Exception):
        info.pattern_overlay = await asyncio.to_thread(render_fragment_pattern, card_tgs)
    log.info("Подарок %s взят с Fragment (t.me не ответил)", slug)
    return info


def extract_fragment_model(tgs: bytes) -> bytes:
    """Fragment отдаёт всю карточку (фон + узор + модель). Оставляем только модель — ассет G.

    Если структура незнакомая, бросаем ValueError, чтобы не нарисовать квадрат с чужим фоном."""
    data = json.loads(gzip.decompress(tgs))
    assets = {a.get("id"): a for a in data.get("assets", [])}
    model = assets.get("G")
    if not model or not model.get("layers"):
        raise ValueError("no model asset in fragment tgs")
    data["layers"] = model["layers"]
    data["assets"] = [a for a in data["assets"] if a.get("id") != "G"]
    return gzip.compress(json.dumps(data, separators=(",", ":")).encode(), compresslevel=9)


async def _download_model(session: aiohttp.ClientSession, info: GiftInfo) -> bytes:
    """Модель со страницы; ссылки там с короткоживущим токеном, поэтому при ошибке
    перечитываем страницу. Последний вариант — модель, вырезанная из карточки Fragment."""
    if info.fragment_card and not info.model_url:
        return extract_fragment_model(info.fragment_card)
    url = info.model_url
    for attempt in range(2):
        if url:
            try:
                return await _get(session, url)
            except (aiohttp.ClientError, TimeoutError) as e:
                log.warning("Модель %s не скачалась (попытка %d): %s", info.slug, attempt + 1, e)
        if attempt == 0:
            with suppress(Exception):
                fresh = await fetch_gift(session, info.slug, retries=0)
                if fresh.fragment_card:
                    return extract_fragment_model(fresh.fragment_card)
                url = fresh.model_url
                info.pattern_url = fresh.pattern_url or info.pattern_url
    return extract_fragment_model(await _get(session, FRAGMENT_TGS_URL.format(slug=info.slug)))


async def fetch_assets(session: aiohttp.ClientSession, info: GiftInfo) -> tuple[bytes, bytes | None]:
    """(модель .tgs, картинка узора или None)."""
    model = await _download_model(session, info)
    pattern = None
    if info.pattern_url and info.has_backdrop:
        for _ in range(2):
            try:
                pattern = await _get(session, info.pattern_url)
                break
            except (aiohttp.ClientError, TimeoutError) as e:
                log.warning("Узор %s не скачался: %s", info.slug, e)
    return model, pattern
