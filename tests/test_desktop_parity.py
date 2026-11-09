"""The settings page's hand-written tables must match coach.settings.

The page renders its own option lists and number bounds; `GET /config` already
ships the authoritative `choices`, and nothing compared the two. This test is
that comparison: add a reasoning level or move a clamp in settings.py and the
suite fails until the page follows.
"""

import re
from pathlib import Path

from coach.settings import _CHOICES, _MAXIMUMS, _MINIMUMS

PAGE = Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"

_CHOICE_BLOCK = re.compile(
    r"options:\s*\[(.*?)\]\s*,\s*onChange:\s*\(next\) => state\.save\(\"([a-z_]+)\"",
    re.S,
)
_NUMBER_BLOCK = re.compile(
    r"min:\s*(-?[\d.]+),\s*\n\s*max:\s*(-?[\d.]+),(?:.*?)"
    r"onChange:\s*\(next\) => state\.save\(\"([a-z_]+)\"",
    re.S,
)


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_every_choice_list_matches_settings():
    found = {}
    for body, key in _CHOICE_BLOCK.findall(_page()):
        found[key] = re.findall(r'value:\s*"([^"]*)"', body)
    for key, values in found.items():
        assert set(values) == set(_CHOICES[key]), key
    # A dropdown naming a setting the backend does not define is drift too.
    assert set(found) <= set(_CHOICES)


def test_every_number_bound_matches_settings():
    found = {}
    for low, high, key in _NUMBER_BLOCK.findall(_page()):
        found[key] = (float(low), float(high))
    for key, (low, high) in found.items():
        if key in _MINIMUMS and key in _MAXIMUMS:
            assert low == float(_MINIMUMS[key]), key
            assert high == float(_MAXIMUMS[key]), key
