"""Delimiter scrubbing shared by guidance and delivery.

Both paths wrap untrusted text in a tag and then strip that tag from the text
itself, so injected content cannot close or reopen its own block. The strip has
to survive obfuscation: split tags, attribute-bearing close tags, zero-width
characters and a bare unterminated opener. One pass is never enough, so the
helpers iterate to a fixpoint.
"""

from __future__ import annotations

import re

__all__ = ["strip_delimited"]

# Zero-width and formatting characters that can split a tag without changing
# how a reader (or a model) perceives it.
_INVISIBLE = re.compile("[\u200b-\u200d\u2060\ufeff]")

# A full tag: bounded attribute text, no nested angle brackets, optional close.
_FULL = r"</?\s*{tag}\b[^<>]{{0,200}}>"
# A bare token with no closing angle at all. Left in place it can pair with the
# wrapper's own closing tag and swallow the text after it. The lookahead keeps
# it from eating the token of a well-formed tag that the full pattern removes
# on the next iteration.
_BARE = r"</?\s*{tag}\b(?!\s*>)"


def strip_delimited(text: object, tag: str) -> str:
    """Remove every form of ``tag`` from ``text``, iterating to a fixpoint."""
    cleaned = _INVISIBLE.sub("", str(text or ""))
    full = re.compile(_FULL.format(tag=re.escape(tag)), re.IGNORECASE)
    bare = re.compile(_BARE.format(tag=re.escape(tag)), re.IGNORECASE)
    while True:
        updated = bare.sub("", full.sub("", cleaned))
        if updated == cleaned:
            return updated
        cleaned = updated
