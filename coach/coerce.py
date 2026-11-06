"""Named lenient-coercion policies for untrusted values.

Settings come from YAML, state from JSON on disk, decisions from a third-party
model. Each reader used to carry its own copy of "float, but not bool, ignore
non-finite", and the copies disagreed: `jev._float(True)` returned 1.0 while
`settings._as_float(True)` returned the default. One module, one policy per
name, and a caller that needs different behaviour says so by choosing another
function rather than by rewriting the body.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["as_bool", "as_finite_float", "as_int", "as_str", "as_text"]

_TRUE_WORDS = {"1", "true", "yes", "on", "enable", "enabled"}
_FALSE_WORDS = {"0", "false", "no", "off", "disable", "disabled"}


def as_str(value: Any) -> str:
    """The value when it is a string, otherwise ``""``."""
    return value if isinstance(value, str) else ""


def as_text(value: Any, default: str = "") -> str:
    """A trimmed non-empty string, otherwise ``default``."""
    if not isinstance(value, str):
        return default
    return value.strip() or default


def as_bool(value: Any, default: bool = False) -> bool:
    """A bool, or one of the documented words; anything else is ``default``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return default


def as_int(value: Any, default: int = 0) -> int:
    """An int; bool is rejected, an integral float is accepted, a bad string is not."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else default
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def as_finite_float(value: Any, default: float = 0.0) -> float:
    """A finite float; bool is rejected and overflow is not an exception."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            return default
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return default
    else:
        return default
    return number if math.isfinite(number) else default
