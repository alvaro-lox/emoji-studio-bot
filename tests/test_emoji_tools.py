import asyncio
import re
import string
from types import SimpleNamespace as NS

from aiogram.types import InlineKeyboardMarkup

from app.common.i18n import TEXTS, t
from app.common.icons import ICONS, Icons, icons
from app.common.storage import Storage, TokenStore, User
from app.menu import SECTION_GIFTS, SECTION_STICKERS, menu_kb, stickers_kb
from app.stickers.core import custom_emojis, emoji_code


def test_custom_emojis_extracts_alt_with_utf16_offsets():
    text = "a😀b🔗"  # 😀 занимает 2 UTF-16 единицы
    entities = [
        NS(type="custom_emoji", custom_emoji_id="1", offset=1, length=2),
        NS(type="custom_emoji", custom_emoji_id="2", offset=4, length=2),
        NS(type="custom_emoji", custom_emoji_id="1", offset=1, length=2),
        NS(type="bold", custom_emoji_id=None, offset=0, length=1),
    ]
    assert custom_emojis(text, entities) == [("1", "😀"), ("2", "🔗")]


def test_emoji_code_snippets():
    items = [("5776078972659962594", "🔗")]
    assert emoji_code("ptb", items) == (
        "await message.reply_text(\n"
        "    '<tg-emoji emoji-id=\"5776078972659962594\">🔗</tg-emoji>',\n"
        '    parse_mode="HTML",\n)'
    )
    assert "message.answer(" in emoji_code("aiogram", items)
    assert '<emoji id="5776078972659962594">🔗</emoji>' in emoji_code("pyrogram", items)
    telethon = emoji_code("telethon", [("1", "😀"), ("2", "🔗")])
    assert "MessageEntityCustomEmoji(0, 2, 1)" in telethon
    assert "MessageEntityCustomEmoji(2, 2, 2)" in telethon
    assert len(emoji_code("ptb", items)) <= 256  # влезает в кнопку «Скопировать»


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_translations_have_same_keys_and_placeholders():
    assert TEXTS["ru"].keys() == TEXTS["en"].keys()
    for key in TEXTS["ru"]:
        assert _fields(TEXTS["ru"][key]) == _fields(TEXTS["en"][key]), key


def test_all_icon_placeholders_exist():
    for lang in TEXTS:
        for text in TEXTS[lang].values():
            for spec in re.findall(r"\{i:(\w+)\}", text):
                assert spec in ICONS, spec


def test_t_renders_fallback_icons():
    assert not icons.enabled
    assert t("ru", "dl_done", n=3) == "✅ Файлов: 3"
    assert t("en", "dl_done", n=3) == "✅ Files: 3"


def _loaded_icons() -> Icons:
    ic = Icons()
    ic._ids = {"search": "111"}
    ic._alts = {"search": "🔍"}
    ic._fallback_by_id = {"111": "🔎"}
    ic.enabled = True
    return ic


def test_icons_html_and_button():
    ic = _loaded_icons()
    assert ic.html("search") == '<tg-emoji emoji-id="111">🔍</tg-emoji>'
    assert ic.html("gift") == "🎁"  # не загружена — обычный эмодзи
    button = ic.button("Поиск", "search", "primary", callback_data="x")
    assert (button.text, button.icon_custom_emoji_id, button.style) == ("Поиск", "111", "primary")
    assert ic.button("Подарок", "gift", callback_data="y").text == "🎁 Подарок"


def test_icons_strip_request():
    ic = _loaded_icons()
    method = NS(
        text='<tg-emoji emoji-id="111">🔍</tg-emoji> Поиск',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[ic.button("Поиск", "search", callback_data="x")]]),
    )
    assert ic.strip(method)
    assert method.text == "🔍 Поиск"
    button = method.reply_markup.inline_keyboard[0][0]
    assert (button.text, button.icon_custom_emoji_id) == ("🔎 Поиск", None)
    assert not ic.strip(method)


def test_main_menu_has_two_sections():
    data = [b.callback_data for row in menu_kb("ru").inline_keyboard for b in row]
    assert data[:2] == [SECTION_STICKERS, SECTION_GIFTS]
    assert "admin" not in data and "admin" in [
        b.callback_data for row in menu_kb("ru", is_admin=True).inline_keyboard for b in row
    ]


def test_stickers_section_marks_current_mode():
    kb = stickers_kb("ru", "id")
    styles = [b.style for row in kb.inline_keyboard for b in row]
    assert styles[:3] == ["primary", "success", "danger"]
    assert kb.inline_keyboard[0][0].text.endswith("✓")
    assert kb.inline_keyboard[-1][0].callback_data == "menu"


def test_storage_roundtrip(tmp_path):
    async def scenario():
        db = Storage(tmp_path / "bot.db")
        await db.open()
        assert await db.get_user(1) is None
        await db.add_user(1, "ru")
        assert await db.get_user(1) == User("ru", "dl", False)
        await db.set_mode(1, "id")
        await db.set_lang(1, "en")
        assert await db.get_user(1) == User("en", "id", False)
        await db.cache_set("k", "file1")
        assert await db.cache_get("k") == "file1"
        await db.close()

    asyncio.run(scenario())


def test_token_store_limit():
    store = TokenStore(limit=2)
    a, b, c = store.put(1), store.put(2), store.put(3)
    assert store.get(a) is None and store.get(b) == 2 and store.get(c) == 3
    assert all(len(f"lib:telethon:t.{x}".encode()) <= 64 for x in (a, b, c))
