"""Чистая логика без сети: разбор ссылок/сущностей и сборка zip."""

import io
import re
import zipfile
from dataclasses import dataclass

PACK_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(?:addstickers|addemoji)/([A-Za-z0-9_]{1,64})"
    r"|tg://(?:addstickers|addemoji)\?set=([A-Za-z0-9_]{1,64})",
    re.IGNORECASE,
)

GIFT_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|nft\.fragment\.com)/(?:nft|gift)/([A-Za-z0-9]+-\d+)"
    r"|tg://nft\?slug=([A-Za-z0-9]+-\d+)",
    re.IGNORECASE,
)

LIBS = {
    "ptb": "python-telegram-bot",
    "aiogram": "aiogram",
    "telethon": "Telethon",
    "pyrogram": "Pyrogram",
}

# Заголовки zip (локальный + центральный каталог) на каждый файл, с запасом
ZIP_ENTRY_OVERHEAD = 128


@dataclass
class ZipEntry:
    path: str
    data: bytes


def find_pack_names(text: str | None) -> list[str]:
    """Имена паков из ссылок t.me/addstickers/... и t.me/addemoji/..."""
    names: list[str] = []
    for match in PACK_LINK_RE.finditer(text or ""):
        name = match.group(1) or match.group(2)
        if name not in names:
            names.append(name)
    return names


def find_gift_slugs(text: str | None) -> list[str]:
    """Слаги подарков из ссылок t.me/nft/<slug>, например resistancedog-14072."""
    slugs: list[str] = []
    for match in GIFT_LINK_RE.finditer(text or ""):
        slug = match.group(1) or match.group(2)
        if slug.lower() not in (s.lower() for s in slugs):
            slugs.append(slug)
    return slugs


def custom_emojis(text: str | None, entities) -> list[tuple[str, str]]:
    """(custom_emoji_id, исходный эмодзи) без повторов, в порядке появления."""
    raw = (text or "").encode("utf-16-le")
    result: dict[str, str] = {}
    for entity in entities or []:
        if entity.type == "custom_emoji" and entity.custom_emoji_id and entity.custom_emoji_id not in result:
            alt = raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le", "ignore")
            result[entity.custom_emoji_id] = alt
    return list(result.items())


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def emoji_code(lib: str, items: list[tuple[str, str]]) -> str:
    """Готовый код отправки премиум-эмодзи для выбранной библиотеки."""
    if lib in ("ptb", "aiogram"):
        html = "".join(f'<tg-emoji emoji-id="{emoji_id}">{alt}</tg-emoji>' for emoji_id, alt in items)
        call = "message.reply_text" if lib == "ptb" else "message.answer"
        return f"await {call}(\n    '{html}',\n    parse_mode=\"HTML\",\n)"
    if lib == "pyrogram":
        html = "".join(f'<emoji id="{emoji_id}">{alt}</emoji>' for emoji_id, alt in items)
        return (
            "from pyrogram.enums import ParseMode\n\n"
            f"await message.reply(\n    '{html}',\n    parse_mode=ParseMode.HTML,\n)"
        )
    if lib == "telethon":
        text, entities, offset = "", [], 0
        for emoji_id, alt in items:
            length = _utf16_len(alt)
            entities.append(f"    MessageEntityCustomEmoji({offset}, {length}, {emoji_id}),")
            text += alt
            offset += length
        return (
            "from telethon.tl.types import MessageEntityCustomEmoji\n\n"
            f"await message.reply('{text}', formatting_entities=[\n" + "\n".join(entities) + "\n])"
        )
    raise ValueError(f"unknown lib {lib}")


def custom_emoji_ids(entities) -> list[str]:
    """id премиум-эмодзи из entities сообщения, без повторов, в порядке появления."""
    ids: list[str] = []
    for entity in entities or []:
        if entity.type == "custom_emoji" and entity.custom_emoji_id and entity.custom_emoji_id not in ids:
            ids.append(entity.custom_emoji_id)
    return ids


def sticker_ext(sticker) -> str:
    if sticker.is_animated:
        return "tgs"
    if sticker.is_video:
        return "webm"
    return "webp"


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_") or "stickers"


def build_zips(entries: list[ZipEntry], max_bytes: int) -> list[bytes]:
    """Пакует файлы в один или несколько zip, каждый не больше max_bytes.

    tgs/webp/webm уже сжаты, поэтому ZIP_STORED: быстрее и размер предсказуем.
    """
    parts: list[bytes] = []
    buffer = io.BytesIO()
    archive = zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED)
    size = 0

    for entry in entries:
        entry_size = len(entry.data) + 2 * len(entry.path.encode()) + ZIP_ENTRY_OVERHEAD
        if size and size + entry_size > max_bytes:  # текущий архив полон — начинаем следующий
            archive.close()
            parts.append(buffer.getvalue())
            buffer = io.BytesIO()
            archive = zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED)
            size = 0
        archive.writestr(entry.path, entry.data)
        size += entry_size

    archive.close()
    if size:
        parts.append(buffer.getvalue())
    return parts
