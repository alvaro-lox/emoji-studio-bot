"""Перекраска, создание эмодзи, превью паков, оформление подарков, лимиты, статистика, слежка."""

import asyncio
import gzip
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import ClassVar

import pytest
from PIL import Image

from app.common.limits import RateLimiter
from app.common.storage import Storage
from app.nft.parser import parse_gift_page
from app.nft.render import GiftStyle, Layout, build_canvas, render_gift
from app.nft.watcher import check_once
from app.stickers.emoji_maker import first_emoji, fit, make_static, make_video, remove_background
from app.stickers.packs import new_set_name, render_grid, set_link, user_pack_name
from app.stickers.recolor import Tint, parse_hex, recolor_image, recolor_lottie, recolor_tgs

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def info():
    return parse_gift_page((FIXTURES / "gift_page.html").read_text("utf-8"), "resistancedog-14072")


def _png(color, size=(64, 64), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


# ---------- перекраска ----------


def test_parse_hex():
    assert parse_hex("#ff8800") == "#FF8800"
    assert parse_hex("00aaff") == "#00AAFF"
    assert parse_hex("red") is None


def test_tint_keeps_lightness_and_sets_hue():
    import colorsys

    tint = Tint("#1E88E5")  # синий
    r, g, b = tint(1.0, 0.2, 0.2)  # был красный
    h, light, _ = colorsys.rgb_to_hls(r, g, b)
    assert abs(h - tint.hue) < 0.01
    assert abs(light - colorsys.rgb_to_hls(1.0, 0.2, 0.2)[1]) < 0.01
    assert tint(1, 1, 1) == pytest.approx((1, 1, 1))  # белые блики остаются белыми


def test_recolor_lottie_static_animated_and_gradient():
    data = {
        "layers": [
            {
                "shapes": [
                    {"ty": "fl", "c": {"a": 0, "k": [1, 0, 0, 1]}},
                    {"ty": "st", "c": {"a": 1, "k": [{"t": 0, "s": [0, 1, 0, 1]}, {"t": 10, "s": [0, 0, 1, 1]}]}},
                    {"ty": "gf", "g": {"p": 2, "k": {"a": 0, "k": [0, 1, 0, 0, 1, 0, 1, 0]}}},
                    {"ty": "fl", "c": {"a": 0, "k": [255, 0, 0]}},  # старый формат 0..255
                ]
            },
            {"ty": 1, "sc": "#ff0000"},
        ]
    }
    recolor_lottie(data, Tint("#43A047"))
    shapes = data["layers"][0]["shapes"]
    fill = shapes[0]["c"]["k"]
    assert fill[1] > fill[0] and fill[3] == 1  # стал зелёным, альфа на месте
    for frame in shapes[1]["c"]["k"]:
        assert frame["s"][1] >= frame["s"][2]
    gradient = shapes[2]["g"]["k"]["k"]
    assert gradient[0] == 0 and gradient[4] == 1  # позиции стопов не тронуты
    assert gradient[2] > gradient[1]
    assert max(shapes[3]["c"]["k"]) > 1  # шкала 0..255 сохранена
    assert data["layers"][1]["sc"] != "#ff0000"


def test_recolor_tgs_roundtrip():
    tgs = (FIXTURES / "gift_model.tgs").read_bytes()
    out = recolor_tgs(tgs, "#E8B923")
    assert json.loads(gzip.decompress(out))["fr"] == 60
    assert out != tgs


def test_recolor_image_keeps_alpha():
    src = Image.new("RGBA", (10, 10), (255, 0, 0, 255))
    src.putpixel((0, 0), (0, 0, 0, 0))
    buf = io.BytesIO()
    src.save(buf, "WEBP", lossless=True)
    out = Image.open(io.BytesIO(recolor_image(buf.getvalue(), "#1E88E5"))).convert("RGBA")
    r, _g, b, a = out.getpixel((5, 5))
    assert b > r and a == 255
    assert out.getpixel((0, 0))[3] == 0


# ---------- создание эмодзи ----------


def test_first_emoji():
    assert first_emoji("вот 🔥 огонь") == "🔥"
    assert first_emoji("👍🏽 круто") == "👍🏽"
    assert first_emoji("без эмодзи") is None
    assert first_emoji(None) is None


def test_remove_background_and_fit():
    img = Image.new("RGB", (60, 40), (255, 255, 255))
    for x in range(20, 40):
        for y in range(10, 30):
            img.putpixel((x, y), (200, 0, 0))
    cut = remove_background(img)
    assert cut.getpixel((0, 0))[3] == 0 and cut.getpixel((30, 20))[3] == 255
    fitted = fit(cut)
    assert fitted.size == (100, 100)
    assert fitted.getpixel((50, 50))[3] == 255  # объект растянут на весь квадрат
    assert fitted.getpixel((0, 0))[3] == 0


def test_make_static_is_100px_png():
    out = Image.open(io.BytesIO(make_static(_png((10, 120, 200), (300, 150)), remove_bg=False)))
    assert out.format == "PNG" and out.size == (100, 100)


def test_make_video_fits_telegram_limits(tmp_path):
    out = make_video(_mp4(tmp_path))
    assert out[:4] == b"\x1a\x45\xdf\xa3"  # WEBM/Matroska
    assert len(out) <= 256 * 1024


def _mp4(tmp_path) -> bytes:
    import subprocess

    import imageio_ffmpeg

    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=30:duration=4",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


# ---------- паки ----------


def test_set_names():
    name = new_set_name("c", "my_bot")
    assert name.startswith("c_") and name.endswith("_by_my_bot") and len(name) <= 64
    assert user_pack_name(42, "my_bot") == "my42_by_my_bot"
    assert set_link("x", "custom_emoji") == "https://t.me/addemoji/x"
    assert set_link("x", "regular") == "https://t.me/addstickers/x"


def test_render_grid():
    images = [Image.new("RGBA", (96, 96), (255, 0, 0, 255)) for _ in range(23)] + [None]
    grid = Image.open(io.BytesIO(render_grid(images, "Test pack", "24 items")))
    assert grid.width == 24 * 2 + 8 * 104 + 7 * 8  # 24 шт. → 8 колонок
    assert grid.height > 3 * 104


# ---------- оформление подарка ----------


@pytest.mark.parametrize("aspect,size", [("3:2", (420, 280)), ("1:1", (360, 360)), ("9:16", (360, 640))])
def test_layout_sizes(aspect, size):
    lay = Layout.make(aspect, 1)
    assert (lay.width, lay.height) == size
    assert lay.width % 2 == 0 and lay.height % 2 == 0


def test_default_style_matches_original_layout(info):
    lay = Layout.make("3:2", 3)
    _, x0, y0, size = build_canvas(info, None, GiftStyle(), lay, True, None)
    assert (lay.k, lay.bx, lay.by) == (3, 0, 0)
    assert (x0, y0, size) == (390, 90, 480)


def test_style_key():
    assert GiftStyle().key() == "d"
    assert GiftStyle(aspect="9:16").key() != GiftStyle(aspect="1:1").key()
    assert GiftStyle(background=b"a").key() != GiftStyle(background=b"b").key()


def test_render_story_with_custom_background_and_text(info, tmp_path):
    style = GiftStyle(aspect="9:16", background=_png((200, 30, 30), (500, 500)), text="custom", custom_text="Моё!")
    result = render_gift(
        info, (FIXTURES / "gift_model.tgs").read_bytes(), None, tmp_path / "s.mp4", scale=1, style=style
    )
    assert (result.width, result.height) == (360, 640)
    assert result.path.stat().st_size > 1000


# ---------- лимиты ----------


def test_rate_limiter_window():
    limiter = RateLimiter(limit=2, window=60)
    assert limiter.hit(1, now=0) == 0
    assert limiter.hit(1, now=1) == 0
    assert limiter.hit(1, now=2) == 58
    assert limiter.hit(2, now=2) == 0  # другой пользователь независим
    assert limiter.hit(1, now=60) == 0  # первая попытка вышла из окна


# ---------- база: бан, статистика, слежка ----------


def test_storage_admin_features(tmp_path):
    async def scenario():
        db = Storage(tmp_path / "bot.db")
        await db.open()
        for uid in (1, 2, 3):
            await db.add_user(uid, "ru" if uid < 3 else "en")
        assert await db.set_banned(2, True)
        assert not await db.set_banned(99, True)
        assert await db.banned_users() == [2]
        await db.mark_blocked(3)
        assert await db.broadcast_targets() == [1]
        await db.touch(3)  # написал снова — разблокирован
        assert sorted(await db.broadcast_targets()) == [1, 3]
        await db.log_event("gift", 1)
        await db.log_event("gift", 1)
        await db.log_event("zip", 3)
        s = await db.stats()
        assert s["users"] == 3 and s["banned"] == 1 and s["langs"] == {"ru": 2, "en": 1}
        assert s["events"]["gift"] == (2, 2, 2) and s["events"]["zip"][2] == 1

        await db.add_watch(1, "Dog-1")
        await db.add_watch(3, "dog-1")
        assert await db.is_watching(1, "dog-1")
        assert await db.watched_slugs() == ["dog-1"]
        await db.set_owner("dog-1", "Dog", "Alice")
        assert await db.get_owner("DOG-1") == ("Dog", "Alice")
        assert await db.user_watches(1) == [("dog-1", "Dog")]
        await db.remove_watch(1, "dog-1")
        assert not await db.is_watching(1, "dog-1")
        await db.close()

    asyncio.run(scenario())


def test_watcher_notifies_on_owner_change(tmp_path, info):
    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.sent.append((chat_id, text))

    class FakeGifts:
        owner = "Alice"

        async def fresh_info(self, slug):
            info.owner = self.owner
            return info

    async def scenario():
        db = Storage(tmp_path / "bot.db")
        await db.open()
        await db.add_user(1, "ru")
        await db.add_watch(1, "resistancedog-14072")
        bot, gifts = FakeBot(), FakeGifts()
        assert await check_once(bot, db, gifts, pause=0) == 0  # первое знакомство — без уведомления
        assert await check_once(bot, db, gifts, pause=0) == 0  # владелец тот же
        gifts.owner = "Bob"
        assert await check_once(bot, db, gifts, pause=0) == 1
        assert bot.sent[0][0] == 1 and "Alice" in bot.sent[0][1] and "Bob" in bot.sent[0][1]
        await db.close()

    asyncio.run(scenario())


# ---------- админы и все ID пака ----------


def test_storage_admins_and_usernames(tmp_path):
    async def scenario():
        db = Storage(tmp_path / "bot.db")
        await db.open()
        await db.add_user(5, "ru", "Somebody")
        await db.touch(7, None)  # неизвестный пользователь — ничего не ломается
        assert await db.find_by_username("@somebody") == 5
        assert await db.find_by_username("nobody") is None
        assert not await db.is_admin(5)
        await db.add_admin(5, 1)
        assert await db.is_admin(5)
        assert await db.admins() == [(5, "somebody")]
        await db.remove_admin(5)
        assert await db.admins() == []
        await db.close()

    asyncio.run(scenario())


def test_pack_ids_json():
    from app.stickers.handlers.content import pack_ids_json

    stickers = [
        NS(emoji="🔗", custom_emoji_id="111", file_id="f1", file_unique_id="u1", is_animated=True, is_video=False)
    ]
    data = json.loads(pack_ids_json(NS(name="p", title="Pack", sticker_type="custom_emoji", stickers=stickers)))
    assert data["items"] == [
        {"index": 1, "emoji": "🔗", "custom_emoji_id": "111", "file_id": "f1", "file_unique_id": "u1", "format": "tgs"}
    ]
    regular = json.loads(pack_ids_json(NS(name="p", title="Pack", sticker_type="regular", stickers=stickers)))
    assert "custom_emoji_id" not in regular["items"][0]


def test_photo_with_caption_reaches_emoji_maker():
    """Подпись к фото (значок эмодзи) не должна перехватываться обработчиком текста."""
    from aiogram.dispatcher.event.bases import SkipHandler

    from app.stickers.handlers import content

    message = NS(text=None, caption="🔥", entities=None, caption_entities=None)
    with pytest.raises(SkipHandler):
        asyncio.run(content.on_text(message, None, "ru", "dl", False, None, None, None, None, None, None))


def test_watcher_ignores_fallback_source(tmp_path, info):
    class FakeBot:
        sent: ClassVar[list] = []

        async def send_message(self, *args, **kwargs):
            self.sent.append(args)

    class FakeGifts:
        async def fresh_info(self, slug):
            info.owner, info.source = None, "fragment"
            return info

    async def scenario():
        db = Storage(tmp_path / "bot.db")
        await db.open()
        await db.add_user(1, "ru")
        await db.add_watch(1, "resistancedog-14072")
        await db.set_owner("resistancedog-14072", "Dog", "Alice")
        assert await check_once(FakeBot(), db, FakeGifts(), pause=0) == 0
        assert await db.get_owner("resistancedog-14072") == ("Dog", "Alice")  # владелец не затёрт
        await db.close()

    asyncio.run(scenario())
    info.source = "page"
