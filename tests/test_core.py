import io
import zipfile
from types import SimpleNamespace as NS

from app.stickers.core import ZipEntry, build_zips, custom_emoji_ids, find_pack_names, safe_name, sticker_ext


def test_find_pack_names_all_link_forms():
    text = (
        "https://t.me/addstickers/Cats_by_x и t.me/addemoji/Dogs "
        "telegram.me/addstickers/Cats_by_x tg://addemoji?set=Birds"
    )
    assert find_pack_names(text) == ["Cats_by_x", "Dogs", "Birds"]


def test_find_pack_names_empty():
    assert find_pack_names(None) == []
    assert find_pack_names("просто текст t.me/durov") == []


def test_custom_emoji_ids_dedup_and_filter():
    entities = [
        NS(type="custom_emoji", custom_emoji_id="1"),
        NS(type="bold", custom_emoji_id=None),
        NS(type="custom_emoji", custom_emoji_id="2"),
        NS(type="custom_emoji", custom_emoji_id="1"),
    ]
    assert custom_emoji_ids(entities) == ["1", "2"]
    assert custom_emoji_ids(None) == []


def test_sticker_ext():
    assert sticker_ext(NS(is_animated=True, is_video=False)) == "tgs"
    assert sticker_ext(NS(is_animated=False, is_video=True)) == "webm"
    assert sticker_ext(NS(is_animated=False, is_video=False)) == "webp"


def test_safe_name():
    assert safe_name("Cats_by_bot") == "Cats_by_bot"
    assert safe_name("../evil name") == "evil_name"
    assert safe_name("///") == "stickers"


def _names(blob: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(blob)).namelist()


def test_build_zips_single_part():
    parts = build_zips([ZipEntry("a/001.tgs", b"x" * 10), ZipEntry("002.webp", b"y")], max_bytes=10_000)
    assert len(parts) == 1
    archive = zipfile.ZipFile(io.BytesIO(parts[0]))
    assert archive.namelist() == ["a/001.tgs", "002.webp"]
    assert archive.read("a/001.tgs") == b"x" * 10


def test_build_zips_splits_by_size():
    entries = [ZipEntry(f"{i}.tgs", b"z" * 1000) for i in range(5)]
    parts = build_zips(entries, max_bytes=2500)
    assert [_names(p) for p in parts] == [["0.tgs", "1.tgs"], ["2.tgs", "3.tgs"], ["4.tgs"]]
    assert all(len(p) <= 2500 for p in parts)


def test_build_zips_oversized_file_gets_own_part():
    parts = build_zips([ZipEntry("big.webm", b"b" * 5000), ZipEntry("s.tgs", b"s")], max_bytes=1000)
    assert [_names(p) for p in parts] == [["big.webm"], ["s.tgs"]]


def test_build_zips_empty():
    assert build_zips([], max_bytes=100) == []
