from __future__ import annotations

import os
import sys


def _supports_color() -> bool:
    """Check if stdout supports ANSI color."""
    if os.getenv("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    return True


_COLOR = _supports_color()

# ANSI escape codes
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_BLUE = "\033[34m"
_WHITE = "\033[97m"


def _wrap(code: str, text: str) -> str:
    if not _COLOR:
        return text
    return f"{code}{text}{_RESET}"


def bold(text: str) -> str:
    return _wrap(_BOLD, text)


def dim(text: str) -> str:
    return _wrap(_DIM, text)


def green(text: str) -> str:
    return _wrap(_GREEN, text)


def yellow(text: str) -> str:
    return _wrap(_YELLOW, text)


def red(text: str) -> str:
    return _wrap(_RED, text)


def cyan(text: str) -> str:
    return _wrap(_CYAN, text)


def magenta(text: str) -> str:
    return _wrap(_MAGENTA, text)


def blue(text: str) -> str:
    return _wrap(_BLUE, text)


def white(text: str) -> str:
    return _wrap(_WHITE, text)


def status_color(status: int) -> str:
    """Return status code wrapped in appropriate color."""
    if status >= 500:
        return red(str(status))
    if status >= 400:
        return yellow(str(status))
    if status >= 300:
        return cyan(str(status))
    if status >= 200:
        return green(str(status))
    return str(status)
