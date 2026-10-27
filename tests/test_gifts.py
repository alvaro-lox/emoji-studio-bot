import asyncio
import io
import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image

from app.nft.handlers import gift_caption, gift_kb
from app.nft.parser import GiftNotFound, parse_gift_page
from app.nft.render import format_number, render_gift
from app.stickers.core import find_gift_slugs

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def info():
    return parse_gift_page((FIXTURES / "gift_page.html").read_text("utf-8"), "resistancedog-14072")


def test_find_gift_slugs():
    text = (
        "https://t.me/nft/resistancedog-14072 и t.me/nft/PlushPepe-1 "
        "tg://nft?slug=Coconut-77 https://nft.fragment.com/gift/resistancedog-14072.webp t.me/nft/bad"
    )
    assert find_gift_slugs(text) == ["resistancedog-14072", "PlushPepe-1", "Coconut-77"]


def test_parse_gift_page(info):
    assert info.title == "Resistance Dog"
    assert info.number == 14072
    assert (info.model.name, info.model.rarity) == ("Cosa Nostra", "0.4%")
    assert (info.backdrop.name, info.backdrop.rarity) == ("Black", "2.5%")
    assert (info.symbol.name, info.symbol.rarity) == ("Twin Peaks", "0.5%")
    assert (info.center_color, info.edge_color, info.pattern_color) == ("#363738", "#0e0f0f", "#6c6868")
    assert info.number_color == "#8c8f91"
    assert len(info.pattern) == 18
    assert info.pattern[0].scale == 0.3
    assert info.model_url.startswith("https://") and "&amp;" not in info.model_url
    assert info.has_backdrop
    assert info.owner == "🛩"


def test_parse_not_a_gift():
    with pytest.raises(GiftNotFound):
        parse_gift_page("<html>404</html>", "nope-1")


def test_format_number():
    assert format_number(14072) == "#14 072"
    assert format_number(7) == "#7"
    assert format_number(None) == ""


def test_caption_and_keyboard(info):
    caption = gift_caption(info, "ru")
    assert "<b>Resistance Dog</b> #14 072" in caption
    assert "Модель: <b>Cosa Nostra</b> · 0.4%" in caption
    kb = gift_kb(info, "f", "ru")
    datas = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert datas == [
        "g:n:resistancedog-14072",
        "gs:resistancedog-14072",
        "gif:f:resistancedog-14072",
        "tgs:resistancedog-14072",
        "w:resistancedog-14072",
    ]
    watching = [b.callback_data for row in gift_kb(info, "f", "ru", True).inline_keyboard for b in row]
    assert "uw:resistancedog-14072" in watching
    assert all(len(d.encode()) <= 64 for d in datas)
    assert kb.inline_keyboard[-1][0].url == "https://t.me/nft/resistancedog-14072"


def _probe(path: Path) -> str:
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True)
    return result.stderr


def _frame(path: Path, second: float) -> Image.Image:
    raw = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-loglevel",
            "error",
            "-ss",
            str(second),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return Image.open(io.BytesIO(raw)).convert("RGB")


@pytest.mark.parametrize("with_backdrop", [True, False])
def test_render_gift_60fps(info, tmp_path, with_backdrop):
    model = (FIXTURES / "gift_model.tgs").read_bytes()
    pattern = (FIXTURES / "gift_pattern.webp").read_bytes()
    result = render_gift(info, model, pattern, tmp_path / "out.mp4", scale=1, with_backdrop=with_backdrop)

    assert (result.width, result.height, result.duration) == (420, 280, 3)
    probe = _probe(result.path)
    assert "60 fps" in probe and "h264" in probe and "420x280" in probe

    corner = _frame(result.path, 1).getpixel((2, 2))
    if with_backdrop:
        # край радиального градиента близок к edge_color #0e0f0f
        assert all(abs(c - e) <= 8 for c, e in zip(corner, (0x0E, 0x0F, 0x0F), strict=False))
    else:
        assert all(abs(c - e) <= 8 for c, e in zip(corner, (0x17, 0x21, 0x2B), strict=False))


# ---------- баг: квадрат с фоном вокруг модели (resistancedog-14071) ----------


def _corner_alpha(tgs: bytes) -> list[int]:
    import gzip as _gzip

    from rlottie_python import LottieAnimation

    anim = LottieAnimation.from_data(_gzip.decompress(tgs).decode())
    frame = anim.render_pillow_frame(30, width=128, height=128)
    return [frame.getpixel(p)[3] for p in ((1, 1), (126, 1), (1, 126), (126, 126))]


def test_fragment_card_is_opaque_and_extracted_model_is_not():
    from app.nft.parser import extract_fragment_model

    card = (FIXTURES / "gift71_fragment.tgs").read_bytes()
    assert min(_corner_alpha(card)) > 200  # вся карточка с фоном — тот самый квадрат
    model = extract_fragment_model(card)
    assert _corner_alpha(model) == [0, 0, 0, 0]


def test_extract_rejects_unknown_structure():
    import gzip as _gzip
    import json as _json

    from app.nft.parser import extract_fragment_model

    with pytest.raises(ValueError):
        extract_fragment_model(_gzip.compress(_json.dumps({"layers": [], "assets": []}).encode()))


class _Resp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            import aiohttp

            raise aiohttp.ClientError(f"HTTP {self.status}")

    async def read(self):
        return self.body


class _Session:
    """Страница отдаётся, а ссылка на модель с неё — 403 (протухший токен)."""

    def __init__(self):
        self.page = (FIXTURES / "gift71_page.html").read_bytes()
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if url.startswith("https://t.me/nft/"):
            return _Resp(200, self.page)
        if url.startswith("https://nft.fragment.com/"):
            return _Resp(200, (FIXTURES / "gift71_fragment.tgs").read_bytes())
        if "sticker.tgs" in url:
            return _Resp(403, b"")
        return _Resp(200, (FIXTURES / "gift71_pattern.webp").read_bytes())


def test_model_fallback_never_returns_square_card():
    from app.nft.parser import fetch_assets

    page = (FIXTURES / "gift71_page.html").read_text("utf-8")
    info = parse_gift_page(page, "resistancedog-14071")
    session = _Session()
    model, pattern = asyncio.run(fetch_assets(session, info))
    assert _corner_alpha(model) == [0, 0, 0, 0]
    assert pattern is not None
    assert sum("sticker.tgs" in u for u in session.urls) == 2  # повтор со свежей страницы
    assert any(u.startswith("https://nft.fragment.com/") for u in session.urls)


def test_bad_slug_is_rejected_before_any_request():
    from app.nft.parser import fetch_gift

    session = _Session()
    with pytest.raises(GiftNotFound):
        asyncio.run(fetch_gift(session, "../../etc"))
    assert session.urls == []


class _RateLimitedSession(_Session):
    """t.me отдаёт пустую страницу (как при частых запросах) — данные должны прийти с Fragment."""

    def get(self, url, timeout=None):
        self.urls.append(url)
        if url.startswith("https://t.me/nft/"):
            return _Resp(200, b"<html><body>Telegram</body></html>")
        if url.endswith(".json"):
            return _Resp(200, (FIXTURES / "gift71_fragment.json").read_bytes())
        if url.endswith(".tgs"):
            return _Resp(200, (FIXTURES / "gift71_fragment.tgs").read_bytes())
        return _Resp(404, b"")


def test_empty_tme_page_falls_back_to_fragment(tmp_path):
    from app.nft.parser import fetch_assets, fetch_gift

    session = _RateLimitedSession()
    info = asyncio.run(fetch_gift(session, "resistancedog-14071", pause=0))
    assert sum(u.startswith("https://t.me/") for u in session.urls) == 3  # 1 + 2 повтора
    assert (info.title, info.number, info.source) == ("Resistance Dog", 14071, "fragment")
    assert info.model.name == "Ghost Rider" and info.model.rarity is None
    assert (info.center_color, info.edge_color) == ("#664d45", "#47362e")  # как на странице t.me
    assert info.pattern_overlay and info.owner is None

    model, _ = asyncio.run(fetch_assets(session, info))
    assert _corner_alpha(model) == [0, 0, 0, 0]
    result = render_gift(info, model, None, tmp_path / "f.mp4", scale=1)
    frame = _frame(result.path, 1)
    corner = frame.getpixel((2, 2))
    assert all(abs(c - e) <= 10 for c, e in zip(corner, (0x47, 0x36, 0x2E), strict=False))


def test_unknown_gift_everywhere_is_not_found():
    from app.nft.parser import fetch_gift

    class Missing(_RateLimitedSession):
        def get(self, url, timeout=None):
            self.urls.append(url)
            if url.startswith("https://t.me/nft/"):
                return _Resp(200, b"<html></html>")
            return _Resp(404, b"")

    with pytest.raises(GiftNotFound):
        asyncio.run(fetch_gift(Missing(), "nope-123", pause=0))


def test_fragment_without_json_uses_name_from_link():
    from app.nft.parser import fetch_gift

    class NoJson(_RateLimitedSession):
        def get(self, url, timeout=None):
            if url.endswith(".json"):
                self.urls.append(url)
                return _Resp(404, b"")
            return super().get(url, timeout)

    info = asyncio.run(fetch_gift(NoJson(), "ResistanceDog-14071", pause=0))
    assert (info.title, info.number, info.model) == ("Resistance Dog", 14071, None)
    assert info.has_backdrop
