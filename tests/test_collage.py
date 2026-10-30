import io
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.nft.collage import (
    FRAMES,
    CollageGift,
    CollageStyle,
    build_overlay,
    grid_shape,
    preview_collage,
    puzzle_rows,
    render_collage,
)
from app.nft.collage_handlers import ASPECT_CODES, settings_kb
from app.nft.parser import parse_gift_page

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def gifts() -> list[CollageGift]:
    first = parse_gift_page((FIXTURES / "gift_page.html").read_text(encoding="utf-8"), "resistancedog-14072")
    second = parse_gift_page((FIXTURES / "gift71_page.html").read_text(encoding="utf-8"), "resistancedog-14071")
    return [
        CollageGift(first, (FIXTURES / "gift_model.tgs").read_bytes(), (FIXTURES / "gift_pattern.webp").read_bytes()),
        CollageGift(
            second, (FIXTURES / "gift71_model.tgs").read_bytes(), (FIXTURES / "gift71_pattern.webp").read_bytes()
        ),
    ]


@pytest.mark.parametrize("count", range(1, 13))
def test_puzzle_has_no_empty_places(count):
    rows = puzzle_rows(count, 1080, 1080)
    assert sum(rows) == count
    assert max(rows) - min(rows) <= 1


def test_grid_fits_all_gifts():
    columns, rows, side = grid_shape(5, 1000, 1000, 0.08, 0.16)
    assert columns * rows >= 5 and side > 200


@pytest.mark.parametrize("frame", FRAMES)
def test_every_shape_places_all_models_inside_canvas(gifts, frame):
    style = CollageStyle(frame=frame, title="Розыгрыш")
    overlay, tiles = build_overlay(gifts * 2, style, 540, 540, None)
    assert overlay.size == (540, 540) and len(tiles) == 4
    for tile in tiles:
        assert tile.model_size > 60
        assert tile.model_left >= 0 and tile.model_left + tile.model_size <= 540
        assert tile.model_top >= 0 and tile.model_top + tile.model_size <= 540


def test_puzzle_covers_whole_canvas(gifts):
    overlay, _ = build_overlay(gifts * 3, CollageStyle(frame="puzzle"), 400, 400, None)
    alpha = np.asarray(overlay.getchannel("A"))
    assert alpha.min() == 255  # пазл закрывает холст полностью — фон под ним не нужен


def test_preview_is_jpeg_of_requested_aspect(gifts):
    preview = Image.open(io.BytesIO(preview_collage(gifts, CollageStyle(aspect="9:16"))))
    assert preview.format == "JPEG" and preview.size == (540, 960)


def test_render_collage_with_living_gradient(gifts, tmp_path):
    style = CollageStyle(background="animated", palette="ocean")
    result = render_collage(gifts, style, tmp_path / "c.mp4", scale=0.25)
    assert result.path.stat().st_size > 1000
    assert (result.width, result.height) == (270, 270)


def test_style_key_depends_on_photo_only_for_photo_background():
    assert CollageStyle(photo=b"a").key() == CollageStyle(photo=b"b").key()
    assert CollageStyle(background="photo", photo=b"a").key() != CollageStyle(background="photo", photo=b"b").key()


def test_settings_keyboard_fits_telegram_limits():
    for frame in FRAMES:
        for background in ("gradient", "animated", "dark", "photo"):
            keyboard = settings_kb("ru", "abcdefgh", CollageStyle(frame=frame, background=background, title="x"))
            for row in keyboard.inline_keyboard:
                styles = {b.style for b in row}
                # строка выбора — синие + одна зелёная; бесцветные с цветными не смешиваются
                assert styles <= {"primary", "success"} or len(styles) == 1
                for button in row:
                    assert button.callback_data is None or len(button.callback_data.encode()) <= 64
    assert set(ASPECT_CODES.values()) == {"1:1", "9:16", "16:9"}


def test_gift_archive_contents(gifts, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from app.nft.service import GiftService

    config = SimpleNamespace(data_dir=tmp_path, render_workers=1, gift_scale=1, font_path=None)
    service = GiftService(None, config)  # type: ignore[arg-type]
    gift = gifts[0]

    async def fake_load(slug):
        return gift.info, gift.model, gift.pattern

    service.load = fake_load  # type: ignore[method-assign]
    data = asyncio.run(service.archive(gift.info.slug))
    names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    base = "resistancedog-14072/resistancedog-14072"
    assert {f"{base}.mp4", f"{base}.tgs", f"{base}.png", f"{base}_model.png"} <= names
    model = Image.open(io.BytesIO(zipfile.ZipFile(io.BytesIO(data)).read(f"{base}_model.png")))
    assert model.mode == "RGBA" and model.getpixel((0, 0))[3] == 0  # модель без фона
