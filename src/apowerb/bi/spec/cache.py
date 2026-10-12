"""
bi/spec/cache.py
----------------
Small thread-safe TTL + LRU cache shared by the spec engine (materialised
sources, forecast outputs, computed responses).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Hashable


class TTLCache:
    """Bounded cache: entries expire after ``ttl`` seconds, the least recently
    used one is dropped beyond ``maxsize``. ``ttl <= 0`` disables the cache."""

    def __init__(self, maxsize: int, ttl: float) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Any | None:
        if self.ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            stamp, value = item
            if now - stamp > self.ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Hashable, value: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
