"""Host model-override trust flags: one writer for both surfaces.

The CLI command (``/coach grant|revoke``) and the dashboard routes
(``/permissions/grant|revoke``) previously carried two independent copies of
the load→mutate→save flow for the same four config keys, and they could not
keep one fix on both sides (the audit's config-clobber finding lives exactly
on that seam). This module is the single implementation; each surface keeps
its own error shape (CLI message vs HTTP 500).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

_TRUST_KEYS = ("allow_provider_override", "allow_model_override")


def set_flags(config: dict[str, Any], value: bool) -> None:
    """Write both model-override trust flags on a loaded host config.

    Every node on the path is typed on the way down: a hand-edited config with
    ``plugins.entries.coach`` as a list must fail with a message naming the
    node, not an opaque AttributeError from chained ``setdefault``.
    """
    node: dict[str, Any] = config
    path: list[str] = []
    for key in ("plugins", "entries", "coach", "llm"):
        path.append(key)
        current = node.setdefault(key, {})
        if not isinstance(current, dict):
            raise TypeError(
                f"host config node {'.'.join(path)!r} is not a mapping"
            )
        node = current
    for key in _TRUST_KEYS:
        node[key] = value


def read_flags(config: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return the two flags as strict booleans (False when unset/wrong-typed)."""
    node: Any = config
    for key in ("plugins", "entries", "coach", "llm"):
        node = node.get(key, {}) if isinstance(node, Mapping) else {}
    return (
        node.get(_TRUST_KEYS[0]) is True,
        node.get(_TRUST_KEYS[1]) is True,
    )


def write(value: bool) -> str:
    """Load→mutate→save the host config, best effort, with a user message.

    The load result is passed to ``save_config`` unchanged: the host refuses
    reads that carry its failure marker, and multiplying or converting it
    here would turn a transient read failure into a config clobber (see the
    audit's P1). The load-fails-but-file-writeable case therefore refuses the
    write instead of replacing the config with coach-only values.
    """
    try:
        from hermes_cli.config import load_config, save_config

        config = load_config()
        if not isinstance(config, Mapping):
            raise TypeError("host config is not a mapping")
        set_flags(config, value)
        save_config(config)
    except Exception as exc:
        # The traceback is the only way to tell a read failure from a save
        # failure or a malformed config node; the caller gets the short form.
        log.warning("coach: trust flag %s failed", value, exc_info=True)
        verb = "grant" if value else "revoke"
        flag = "true" if value else "false"
        return (
            f"Could not {verb} model overrides automatically ({type(exc).__name__}). "
            "Set plugins.entries.coach.llm."
            f"{_TRUST_KEYS[0]} and {_TRUST_KEYS[1]} to {flag}."
        )
    if value:
        return "Granted per-mode model overrides for the coach plugin."
    return "Revoked per-mode model overrides for the coach plugin."
