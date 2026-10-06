import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CacheItem:
    value: Any
    expires_at: float


class AsyncTTLCache:
    """Small in-process cache with per-key request coalescing."""

    def __init__(self, max_items: int = 1024) -> None:
        self._items: dict[str, CacheItem] = {}
        self._locks = [asyncio.Lock() for _ in range(32)]
        self._max_items = max_items

    async def get_or_set(
        self,
        key: str,
        ttl_seconds: int,
        loader: Callable[[], Awaitable[Any]],
    ) -> Any:
        now = time.monotonic()
        cached = self._items.get(key)
        if cached and cached.expires_at > now:
            return cached.value

        lock = self._locks[hash(key) % len(self._locks)]
        async with lock:
            now = time.monotonic()
            cached = self._items.get(key)
            if cached and cached.expires_at > now:
                return cached.value
            value = await loader()
            if len(self._items) >= self._max_items:
                self._items.pop(next(iter(self._items)))
            self._items[key] = CacheItem(value, time.monotonic() + ttl_seconds)
            return value
