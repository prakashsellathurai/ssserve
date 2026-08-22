"""Fast operations with C extension fallback.

Provides sendfile, gzip, and ETag operations with automatic fallback
to pure Python implementations when the C extension is unavailable.
"""

from __future__ import annotations

import gzip as _py_gzip
import os
import sys

try:
    from ssserve._fastops import sendfile as _c_sendfile
    from ssserve._fastops import fast_gzip as _c_fast_gzip
    from ssserve._fastops import etag as _c_etag
    _HAS_C = True
except ImportError:
    _HAS_C = False


def sendfile(out_fd: int, in_fd: int, count: int) -> int | None:
    """Zero-copy file-to-socket transfer using Linux sendfile().

    Returns bytes sent, or None if sendfile is not available for these fds
    (caller should fall back to read/write).
    """
    if _HAS_C:
        result = _c_sendfile(out_fd, in_fd, count)
        if result is not None:
            return result
    return None


def fast_gzip(data: bytes, level: int = 6) -> bytes:
    """Compress data using zlib with gzip encoding.

    Falls back to Python's gzip module if C extension is unavailable.
    """
    if _HAS_C:
        return _c_fast_gzip(data, level=level)
    return _py_gzip.compress(data, compresslevel=level)


def etag(mtime: float, size: int) -> str:
    """Compute a fast ETag from file mtime and size.

    Falls back to Python string formatting if C extension is unavailable.
    """
    if _HAS_C:
        return _c_etag(mtime, size)
    return f'"{int(mtime)}-{size}"'


def has_c_extension() -> bool:
    """Check if the C extension is available."""
    return _HAS_C
