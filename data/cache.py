import time
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class CacheItem(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, CacheItem[T]] = {}

    def get(self, key: str) -> T | None:
        item = self._items.get(key)
        if not item:
            return None
        if item.expires_at < time.time():
            self._items.pop(key, None)
            return None
        return item.value

    def set(self, key: str, value: T) -> None:
        self._items[key] = CacheItem(value=value, expires_at=time.time() + self.ttl_seconds)

    def clear(self) -> None:
        self._items.clear()
