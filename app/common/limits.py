"""Ограничение частоты: скользящее окно на пользователя."""

import math
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._hits: defaultdict[int, deque[float]] = defaultdict(deque)

    def hit(self, key: int, now: float | None = None) -> int:
        """Засчитывает попытку. 0 — можно, иначе сколько секунд подождать."""
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return max(1, math.ceil(self.window - (now - hits[0])))
        hits.append(now)
        return 0


class Limits:
    """heavy — рендеры, перекраска, создание эмодзи и паков; flood — любые апдейты."""

    def __init__(self, heavy_limit: int, heavy_window: float, flood_limit: int = 25, flood_window: float = 10):
        self.heavy = RateLimiter(heavy_limit, heavy_window)
        self.flood = RateLimiter(flood_limit, flood_window)
