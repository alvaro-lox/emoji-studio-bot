"""SQLite: пользователи (язык, режим, бан), статистика, слежка за подарками, кэш file_id."""

import secrets
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

MODES = ("id", "dl", "color")
EVENT_KINDS = ("gift", "collage", "zip", "recolor", "emoji", "photo", "preview")


@dataclass
class User:
    lang: str
    mode: str
    banned: bool


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                lang TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'dl',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS file_cache (
                key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                kind TEXT NOT NULL,
                user_id INTEGER,
                ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts);
            CREATE TABLE IF NOT EXISTS watches (
                user_id INTEGER NOT NULL,
                slug TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, slug)
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS gift_owners (
                slug TEXT PRIMARY KEY,
                title TEXT,
                owner TEXT
            );
            """
        )
        # Колонки, добавленные после первой версии
        async with self._db.execute("PRAGMA table_info(users)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        for column, ddl in (
            ("banned", "INTEGER NOT NULL DEFAULT 0"),
            ("blocked", "INTEGER NOT NULL DEFAULT 0"),
            ("last_seen", "TEXT"),
            ("username", "TEXT"),
        ):
            if column not in columns:
                await self._db.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.open() не вызван")
        return self._db

    async def _one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple = ()) -> list:
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _run(self, sql: str, params: tuple = ()) -> None:
        await self._conn.execute(sql, params)
        await self._conn.commit()

    # ---------- пользователи ----------

    async def get_user(self, user_id: int) -> User | None:
        row = await self._one("SELECT lang, mode, banned FROM users WHERE user_id = ?", (user_id,))
        return User(row[0], row[1], bool(row[2])) if row else None

    async def add_user(self, user_id: int, lang: str, username: str | None = None) -> None:
        await self._run(
            "INSERT OR IGNORE INTO users (user_id, lang, last_seen, username) VALUES (?, ?, CURRENT_TIMESTAMP, ?)",
            (user_id, lang, username.lower() if username else None),
        )

    async def touch(self, user_id: int, username: str | None = None) -> None:
        await self._run(
            "UPDATE users SET last_seen = CURRENT_TIMESTAMP, blocked = 0, username = ? WHERE user_id = ?",
            (username.lower() if username else None, user_id),
        )

    async def find_by_username(self, username: str) -> int | None:
        row = await self._one("SELECT user_id FROM users WHERE username = ?", (username.lstrip("@").lower(),))
        return row[0] if row else None

    async def username_of(self, user_id: int) -> str | None:
        row = await self._one("SELECT username FROM users WHERE user_id = ?", (user_id,))
        return row[0] if row else None

    # ---------- админы (кроме владельцев из ADMIN_IDS) ----------

    async def is_admin(self, user_id: int) -> bool:
        return bool(await self._one("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)))

    async def add_admin(self, user_id: int, added_by: int) -> None:
        await self._run("INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)", (user_id, added_by))

    async def remove_admin(self, user_id: int) -> None:
        await self._run("DELETE FROM admins WHERE user_id = ?", (user_id,))

    async def admins(self) -> list[tuple[int, str | None]]:
        return await self._all(
            "SELECT a.user_id, u.username FROM admins a"
            " LEFT JOIN users u ON u.user_id = a.user_id ORDER BY a.created_at"
        )

    async def set_lang(self, user_id: int, lang: str) -> None:
        await self._run("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))

    async def set_mode(self, user_id: int, mode: str) -> None:
        await self._run("UPDATE users SET mode = ? WHERE user_id = ?", (mode, user_id))

    async def set_banned(self, user_id: int, banned: bool) -> bool:
        """False, если такого пользователя нет."""
        if not await self._one("SELECT 1 FROM users WHERE user_id = ?", (user_id,)):
            return False
        await self._run("UPDATE users SET banned = ? WHERE user_id = ?", (int(banned), user_id))
        return True

    async def banned_users(self, limit: int = 50) -> list[int]:
        return [r[0] for r in await self._all("SELECT user_id FROM users WHERE banned = 1 LIMIT ?", (limit,))]

    async def mark_blocked(self, user_id: int) -> None:
        await self._run("UPDATE users SET blocked = 1 WHERE user_id = ?", (user_id,))

    async def broadcast_targets(self) -> list[int]:
        return [r[0] for r in await self._all("SELECT user_id FROM users WHERE banned = 0 AND blocked = 0")]

    # ---------- статистика ----------

    async def log_event(self, kind: str, user_id: int | None) -> None:
        await self._run("INSERT INTO events (kind, user_id) VALUES (?, ?)", (kind, user_id))

    async def stats(self) -> dict:
        async def count(sql: str, params: tuple = ()) -> int:
            row = await self._one(sql, params)
            return row[0] if row else 0

        day, week = "datetime('now', '-1 day')", "datetime('now', '-7 days')"
        result = {
            "users": await count("SELECT COUNT(*) FROM users"),
            "new_day": await count(f"SELECT COUNT(*) FROM users WHERE created_at >= {day}"),
            "new_week": await count(f"SELECT COUNT(*) FROM users WHERE created_at >= {week}"),
            "active_day": await count(f"SELECT COUNT(*) FROM users WHERE last_seen >= {day}"),
            "active_week": await count(f"SELECT COUNT(*) FROM users WHERE last_seen >= {week}"),
            "banned": await count("SELECT COUNT(*) FROM users WHERE banned = 1"),
            "blocked": await count("SELECT COUNT(*) FROM users WHERE blocked = 1"),
            "watches": await count("SELECT COUNT(*) FROM watches"),
            "langs": dict(await self._all("SELECT lang, COUNT(*) FROM users GROUP BY lang ORDER BY 2 DESC")),
        }
        events: dict[str, tuple[int, int, int]] = {}
        result["events"] = events
        for kind in EVENT_KINDS:
            events[kind] = (
                await count(f"SELECT COUNT(*) FROM events WHERE kind = ? AND ts >= {day}", (kind,)),
                await count(f"SELECT COUNT(*) FROM events WHERE kind = ? AND ts >= {week}", (kind,)),
                await count("SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)),
            )
        return result

    # ---------- слежка за подарками ----------

    async def add_watch(self, user_id: int, slug: str) -> None:
        await self._run("INSERT OR IGNORE INTO watches (user_id, slug) VALUES (?, ?)", (user_id, slug.lower()))

    async def remove_watch(self, user_id: int, slug: str) -> None:
        await self._run("DELETE FROM watches WHERE user_id = ? AND slug = ?", (user_id, slug.lower()))

    async def is_watching(self, user_id: int, slug: str) -> bool:
        return bool(await self._one("SELECT 1 FROM watches WHERE user_id = ? AND slug = ?", (user_id, slug.lower())))

    async def user_watches(self, user_id: int) -> list[tuple[str, str | None]]:
        """[(slug, title)] слежек пользователя."""
        return await self._all(
            "SELECT w.slug, o.title FROM watches w LEFT JOIN gift_owners o ON o.slug = w.slug "
            "WHERE w.user_id = ? ORDER BY w.created_at",
            (user_id,),
        )

    async def watched_slugs(self) -> list[str]:
        return [r[0] for r in await self._all("SELECT DISTINCT slug FROM watches")]

    async def watchers(self, slug: str) -> list[tuple[int, str]]:
        return await self._all(
            "SELECT w.user_id, u.lang FROM watches w JOIN users u ON u.user_id = w.user_id "
            "WHERE w.slug = ? AND u.banned = 0 AND u.blocked = 0",
            (slug.lower(),),
        )

    async def get_owner(self, slug: str) -> tuple[str | None, str | None] | None:
        row = await self._one("SELECT title, owner FROM gift_owners WHERE slug = ?", (slug.lower(),))
        return (row[0], row[1]) if row else None

    async def set_owner(self, slug: str, title: str, owner: str | None) -> None:
        await self._run(
            "INSERT OR REPLACE INTO gift_owners (slug, title, owner) VALUES (?, ?, ?)", (slug.lower(), title, owner)
        )

    # ---------- кэш file_id ----------

    async def cache_get(self, key: str) -> str | None:
        row = await self._one("SELECT file_id FROM file_cache WHERE key = ?", (key,))
        return row[0] if row else None

    async def cache_set(self, key: str, file_id: str) -> None:
        await self._run("INSERT OR REPLACE INTO file_cache (key, file_id) VALUES (?, ?)", (key, file_id))


class TokenStore:
    """Короткие ключи для callback_data (лимит 64 байта), живут в памяти."""

    def __init__(self, limit: int = 5000):
        self._items: OrderedDict[str, object] = OrderedDict()
        self._limit = limit

    def put(self, value) -> str:
        token = secrets.token_urlsafe(6)
        self._items[token] = value
        if len(self._items) > self._limit:
            self._items.popitem(last=False)
        return token

    def get(self, token: str):
        return self._items.get(token)

    def set(self, token: str, value) -> None:
        self._items[token] = value
