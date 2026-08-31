"""Fast operations with C extension fallback.

All hot-path operations delegated to C for maximum performance.
Falls back to pure Python when C extension is unavailable.
"""

from __future__ import annotations

import gzip as _py_gzip
import mimetypes as _py_mimetypes
import threading

try:
    from ssserve._fastops import CCache as _CCache
    from ssserve._fastops import etag as _c_etag
    from ssserve._fastops import fast_gzip as _c_gzip
    from ssserve._fastops import guess_type as _c_guess_type
    from ssserve._fastops import sendfile as _c_sendfile
    _HAS_C = True
except ImportError:
    _HAS_C = False


class CCache:
    """C-backed LRU cache with TTL. Falls back to Python dict+OrderedDict."""

    def __init__(self, max_size: int = 1024, ttl: float = 60.0) -> None:
        if _HAS_C:
            self._impl = _CCache(max_size=max_size, ttl=ttl)
            self.get = self._impl.get
            self.set = self._impl.set
            self.clear = self._impl.clear
            self.stats = self._impl.stats
        else:
            from collections import OrderedDict
            self._store: dict[str, tuple] = {}
            self._order: OrderedDict[str, None] = OrderedDict()
            self._max = max_size
            self._ttl = ttl
            self._lock = threading.Lock()
            self._hits = 0
            self._misses = 0

    if not _HAS_C:
        def get(self, key: str):
            import time
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, ts = entry
            if time.monotonic() - ts > self._ttl:
                with self._lock:
                    self._store.pop(key, None)
                    self._order.pop(key, None)
                self._misses += 1
                return None
            self._hits += 1
            with self._lock:
                self._order.move_to_end(key, last=True)
            return value

        def set(self, key: str, value) -> None:
            import time
            with self._lock:
                if key in self._store:
                    self._order.move_to_end(key, last=True)
                else:
                    if len(self._store) >= self._max:
                        oldest, _ = self._order.popitem(last=False)
                        del self._store[oldest]
                    self._order[key] = None
                self._store[key] = (value, time.monotonic())

        def clear(self) -> None:
            with self._lock:
                self._store.clear()
                self._order.clear()

        def stats(self) -> dict:
            return {"hits": self._hits, "misses": self._misses, "size": len(self._store)}


def etag(mtime: float, size: int) -> str:
    if _HAS_C:
        return _c_etag(mtime, size)
    return f'"{int(mtime)}-{size}"'


def fast_gzip(data: bytes, level: int = 6) -> bytes:
    if _HAS_C:
        return _c_gzip(data, level=level)
    return _py_gzip.compress(data, compresslevel=level)


def sendfile(out_fd: int, in_fd: int, count: int) -> int | None:
    if _HAS_C:
        result = _c_sendfile(out_fd, in_fd, count)
        if result is not None:
            return result
    return None


def guess_type(path: str) -> str:
    if _HAS_C:
        return _c_guess_type(path)
    mime, _ = _py_mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def has_c_extension() -> bool:
    return _HAS_C
