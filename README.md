# Emoji Studio

[Русская версия](README.ru.md)

A Telegram bot for premium emoji, stickers and NFT gifts: get emoji IDs with ready-to-paste code,
download whole packs as zip, recolor any pack while keeping its shading, make your own emoji from images
and videos, and turn a `t.me/nft/…` link into a 60 fps animation that looks like the Telegram gift card.

## Features
The menu has two sections.

**🧩 Emoji & stickers.** A mode decides what happens to the stickers and emoji you send:
- **Emoji ID** — the `custom_emoji_id` of each premium emoji, a Copy button and code snippets for
  python-telegram-bot, aiogram, Telethon and Pyrogram. For a whole pack: the ID list plus a JSON file.
- **Download** — everything you send in a row arrives as one zip (`.tgs` / `.webm` / `.webp`),
  split into several archives to stay under the 50 MB bot upload limit.
- **Recolor** — 10 preset colors or any HEX. Lightness is preserved, so shadows and highlights survive.
  The recolored pack can be published as a new Telegram set in one tap.

In any mode, a pack link gives a preview grid of the whole pack, and an image, GIF or video becomes a
100×100 premium emoji (static PNG or animated WEBM) with optional background removal and a personal pack.

**🎁 NFT gifts.** Send `https://t.me/nft/<slug>` and get:
- a 60 fps MP4 rendered from the gift's Lottie model, backdrop gradient and symbol pattern;
- styling: 3:2, 1:1 (avatar) or 9:16 (story), your own background image, custom caption or none;
- a 50 fps GIF and the raw `.tgs` model;
- owner tracking — the bot messages you when the gift changes hands (`/watches`).

**Admin panel** (`/admin`): statistics, broadcast with preview and progress, bans, admin management.
Per-user rate limiting for heavy jobs and an anti-flood guard; admins are exempt.

> Premium icons in messages and buttons only render if the **bot owner has Telegram Premium**.
> Without it the bot automatically falls back to regular emoji (or set `CUSTOM_EMOJI=0`).

## How the gift render works
There is no public API for gift visuals, so the bot reads the public `t.me/nft/<slug>` page:
the radial gradient stops, the pattern tile and its positions from the inline SVG, and the
short-lived link to the model's `.tgs`. Frames come from rlottie, which returns **premultiplied**
BGRA, so compositing is `out = src + dst · (1 − α)` — the naive formula leaves dark fringes.
Frames are piped straight into ffmpeg (libx264, 60 fps). GIF is capped at 50 fps because GIF frame
delays are stored in hundredths of a second.

Under frequent requests t.me returns an empty page; the bot retries and then falls back to Fragment's
`.tgs` card. That card is the *whole* card (background + pattern + model), so only the model asset is
extracted from it — rendering the card as-is draws an opaque square around the gift.

## Quick start
1. Copy `.env.example` to `.env` and set `BOT_TOKEN` (from @BotFather).
2. Run the bot, send it `/myid`, put that ID into `ADMIN_IDS`, restart.

Only **one instance** can poll with a given token — stop the old one before starting another.

### Docker
```bash
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```
Database, cache and watches live in the `bot-data` volume.

### Without Docker
```bash
sudo apt install python3-venv ffmpeg fonts-inter
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m app
```
A systemd unit is in `deploy/emoji-bot.service`. On Windows, `deploy.bat` uploads the bot to a server
over SSH and starts it in Docker (the password is asked each time and never saved).

## Configuration (`.env`)
| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | token from @BotFather |
| `ADMIN_IDS` | — | comma-separated owner IDs |
| `ICON_PACK` | `tgiosicons` | premium emoji pack used for icons |
| `CUSTOM_EMOJI` | `1` | `0` — plain emoji instead of premium icons |
| `BATCH_DELAY` | `2.5` | seconds to wait for more messages before building a zip |
| `HEAVY_LIMIT` / `HEAVY_WINDOW` | `6` / `60` | heavy jobs per user per window |
| `WATCH_INTERVAL` | `900` | owner check interval, seconds |
| `MAX_WATCHES` | `20` | gifts one user can track |
| `RENDER_WORKERS` | `2` | concurrent renders |
| `GIFT_SCALE` | `3` | output size (3 → 1260×840, 1080×1080, 1080×1920) |
| `FONT_PATH` | — | custom caption font |
| `DATA_DIR` | `data` | SQLite database and cache |

## Project layout
The code is split the same way as the menu.
```
app/
├── main.py            entry point, router order
├── config.py          settings from .env
├── menu.py            main menu and sections, language, help
├── nft/               NFT gifts: page parser + Fragment fallback, 60 fps renderer, cache, owner watcher, handlers
├── stickers/          emoji & stickers: links and zip, downloader, recolor, emoji maker, pack previews, handlers/
├── admin/             statistics, broadcast, bans, admins
└── common/            SQLite storage, rate limits, RU/EN texts, icons, keyboards
```

## Development
```bash
pip install -r requirements.txt pytest ruff mypy
python -m pytest -q
python -m ruff check app tests
python -m mypy app
```

## License
[MIT](LICENSE). Telegram, NFT gifts and the icon pack belong to their respective owners;
this project is not affiliated with Telegram.
