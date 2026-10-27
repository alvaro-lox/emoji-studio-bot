"""Сквозная проверка Downloader на фейковом боте (без сети)."""

import asyncio
import io
import zipfile
from types import SimpleNamespace as NS

from aiogram.exceptions import TelegramBadRequest

from app.stickers.downloader import Downloader


def sticker(uid, set_name=None, animated=True, video=False, emoji_id=None):
    return NS(
        file_id=f"fid_{uid}",
        file_unique_id=uid,
        set_name=set_name,
        is_animated=animated,
        is_video=video,
        custom_emoji_id=emoji_id,
    )


class FakeBot:
    def __init__(self, sets=None, emojis=None, broken_files=()):
        self.sets = sets or {}
        self.emojis = emojis or {}
        self.broken_files = set(broken_files)
        self.documents = []  # (filename, bytes, caption, reply_markup)
        self.messages = []
        self.edits = []

    async def send_message(self, chat_id, text):
        self.messages.append(text)
        return NS(chat=NS(id=chat_id), message_id=len(self.messages))

    async def edit_message_text(self, text, chat_id, message_id):
        self.edits.append(text)

    async def delete_message(self, chat_id, message_id):
        pass

    async def get_sticker_set(self, name):
        if name not in self.sets:
            raise TelegramBadRequest(method=None, message="STICKERSET_INVALID")
        return NS(name=name, stickers=self.sets[name])

    async def get_custom_emoji_stickers(self, custom_emoji_ids):
        return [self.emojis[i] for i in custom_emoji_ids if i in self.emojis]

    async def download(self, file_id, destination):
        if file_id in self.broken_files:
            raise TelegramBadRequest(method=None, message="file is too big")
        destination.write(f"data:{file_id}".encode())

    async def send_document(self, chat_id, document, caption=None, reply_markup=None):
        self.documents.append((document.filename, document.data, caption, reply_markup))


def run(bot, *adds, delay=0.05):
    async def scenario():
        downloader = Downloader(bot, delay=delay)
        for chat_id, items, packs in adds:
            downloader.add(chat_id, "ru", items=items, packs=packs)
        await asyncio.sleep(delay * 4)
        return downloader

    return asyncio.run(scenario())


def zip_names(blob):
    return zipfile.ZipFile(io.BytesIO(blob)).namelist()


def test_several_messages_end_up_in_one_zip():
    emoji = sticker("e1", set_name="EmojiPack", emoji_id="111")
    bot = FakeBot(emojis={"111": emoji})
    run(bot, (1, [sticker("s1", set_name="Cats", video=True, animated=False)], []), (1, ["111", "111"], []))

    assert len(bot.documents) == 1
    filename, data, caption, markup = bot.documents[0]
    assert filename.startswith("stickers_") and filename.endswith(".zip")
    assert zip_names(data) == ["001.webm", "002.tgs"]
    assert caption == "✅ Файлов: 2"
    # Под архивом предложены оба пака
    labels = [row[0].text for row in markup.inline_keyboard]
    assert labels == ["🗂 Весь пак Cats", "🗂 Весь пак EmojiPack"]


def test_pack_link_downloads_whole_pack_into_folder():
    bot = FakeBot(sets={"Dogs": [sticker("d1", "Dogs"), sticker("d2", "Dogs", animated=False)]})
    run(bot, (1, [], ["Dogs"]))

    filename, data, _caption, markup = bot.documents[0]
    assert filename == "Dogs.zip"
    assert zip_names(data) == ["Dogs/001.tgs", "Dogs/002.webp"]
    assert zipfile.ZipFile(io.BytesIO(data)).read("Dogs/001.tgs") == b"data:fid_d1"
    assert markup is None


def test_single_from_downloaded_pack_is_not_duplicated():
    bot = FakeBot(sets={"Dogs": [sticker("d1", "Dogs")]})
    run(bot, (1, [sticker("d1", "Dogs")], ["Dogs"]))
    assert zip_names(bot.documents[0][1]) == ["Dogs/001.tgs"]


def test_errors_reported_in_caption():
    bot = FakeBot(sets={"Ok": [sticker("a", "Ok"), sticker("b", "Ok")]}, broken_files={"fid_b"})
    run(bot, (1, ["404"], ["Ok", "Missing"]))

    _filename, data, caption, _ = bot.documents[0]
    assert zip_names(data) == ["Ok/001.tgs"]
    assert "пак <code>Missing</code> не найден" in caption
    assert "не найдено эмодзи: 1" in caption
    assert "не скачалось файлов: 1" in caption


def test_nothing_to_download():
    bot = FakeBot()
    run(bot, (1, [], ["Missing"]))
    assert bot.documents == []
    assert bot.edits[-1].startswith("❌ Нечего скачивать.")


def test_chats_are_batched_separately():
    bot = FakeBot()
    run(bot, (1, [sticker("a")], []), (2, [sticker("b")], []))
    assert sorted(zip_names(d[1]) for d in bot.documents) == [["001.tgs"], ["001.tgs"]]


def test_pack_button_key_roundtrip():
    downloader = run(FakeBot(), delay=0.01)
    name = "A" * 64
    key = downloader.pack_key(name)
    assert len(f"pack:{key}".encode()) <= 64
    assert downloader.pack_by_key(key) == name
