from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class LockFreeCache(Generic[T]):
    """Lock-free cache using copy-on-write pattern for concurrent access."""

    def __init__(self, max_size: int = 1024, ttl: float = 60.0) -> None:
        self._max_size = max_size
        self._ttl = ttl
        self._store: dict[str, tuple[T, float]] = {}
        self._access_order: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self._stats_hits = 0
        self._stats_misses = 0

    def get(self, key: str) -> T | None:
        entry = self._store.get(key)
        if entry is None:
            self._stats_misses += 1
            return None
        value, ts = entry
        if time.monotonic() - ts > self._ttl:
            self._evict(key)
            self._stats_misses += 1
            return None
        self._stats_hits += 1
        with self._lock:
            self._access_order.move_to_end(key, last=True)
        return value

    def set(self, key: str, value: T) -> None:
        with self._lock:
            if key in self._store:
                self._access_order.move_to_end(key, last=True)
            else:
                if len(self._store) >= self._max_size:
                    oldest_key, _ = self._access_order.popitem(last=False)
                    del self._store[oldest_key]
                self._access_order[key] = None
            self._store[key] = (value, time.monotonic())

    def _evict(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)
            self._access_order.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._access_order.clear()

    @property
    def stats(self) -> dict[str, int]:
        return {"hits": self._stats_hits, "misses": self._stats_misses, "size": len(self._store)}


class AtomicReference(Generic[T]):
    """Thread-safe atomic reference using copy-on-write."""

    def __init__(self, initial: T) -> None:
        self._value = initial
        self._lock = threading.Lock()

    def get(self) -> T:
        return self._value

    def set(self, value: T) -> None:
        with self._lock:
            self._value = value

    def swap(self, value: T) -> T:
        with self._lock:
            old = self._value
            self._value = value
            return old

    def compare_and_set(self, expected: T, new: T) -> bool:
        with self._lock:
            if self._value is expected:
                self._value = new
                return True
            return False


class ThreadLocalBuffer:
    """Thread-local buffer for per-thread operations."""

    def __init__(self) -> None:
        self._local = threading.local()

    def get_buffer(self, size: int = 65536) -> bytearray:
        if not hasattr(self._local, "buffer") or len(self._local.buffer) < size:
            self._local.buffer = bytearray(size)
        return self._local.buffer

    def get_bytesio(self) -> Any:
        import io
        if not hasattr(self._local, "bytesio"):
            self._local.bytesio = io.BytesIO()
        self._local.bytesio.seek(0)
        self._local.bytesio.truncate()
        return self._local.bytesio
