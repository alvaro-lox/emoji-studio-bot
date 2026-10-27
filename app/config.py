"""Настройки из переменных окружения / .env."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    icon_pack: str = "tgiosicons"
    custom_emoji: bool = True
    batch_delay: float = 2.5
    data_dir: Path = Path("data")
    render_workers: int = 2
    gift_scale: int = 3
    font_path: str | None = None
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    heavy_limit: int = 6
    heavy_window: int = 60
    watch_interval: int = 900
    max_watches: int = 20


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit())


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "123456:ABC-your-token":
        raise SystemExit("Укажи BOT_TOKEN в файле .env (см. .env.example)")

    data_dir = Path(os.getenv("DATA_DIR") or "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        token=token,
        icon_pack=os.getenv("ICON_PACK") or "tgiosicons",
        custom_emoji=os.getenv("CUSTOM_EMOJI", "1").lower() not in ("0", "false", "no", "off"),
        batch_delay=float(os.getenv("BATCH_DELAY") or 2.5),
        data_dir=data_dir,
        render_workers=max(1, int(os.getenv("RENDER_WORKERS") or 2)),
        gift_scale=max(1, int(os.getenv("GIFT_SCALE") or 3)),
        font_path=os.getenv("FONT_PATH") or None,
        admin_ids=_ids(os.getenv("ADMIN_IDS", "")),
        heavy_limit=max(1, int(os.getenv("HEAVY_LIMIT") or 6)),
        heavy_window=max(1, int(os.getenv("HEAVY_WINDOW") or 60)),
        watch_interval=max(60, int(os.getenv("WATCH_INTERVAL") or 900)),
        max_watches=max(1, int(os.getenv("MAX_WATCHES") or 20)),
    )
