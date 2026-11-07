"""Host model-override trust flags: one writer for both surfaces.

The CLI command (``/coach grant|revoke``) and the dashboard routes
(``/permissions/grant|revoke``) previously carried two independent copies of
the load→mutate→save flow for the same four config keys, and they could not
keep one fix on both sides (the audit's config-clobber finding lives exactly
on that seam). This module is the single implementation; each surface keeps
its own error shape (CLI message vs HTTP 500).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_TRUST_KEYS = ("allow_provider_override", "allow_model_override")


def set_flags(config: dict[str, Any], value: bool) -> None:
    """Write both model-override trust flags on a loaded host config."""
    plugins = config.setdefault("plugins", {})
    entries = plugins.setdefault("entries", {})
    coach = entries.setdefault("coach", {})
    llm = coach.setdefault("llm", {})
    for key in _TRUST_KEYS:
        llm[key] = value


def read_flags(config: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return the two flags as strict booleans (False when unset/wrong-typed)."""
    coach = (
        config.get("plugins", {}).get("entries", {}).get("coach", {})
        if isinstance(config, Mapping)
        else {}
    )
    llm = coach.get("llm", {}) if isinstance(coach, Mapping) else {}
    return (
        llm.get(_TRUST_KEYS[0]) is True,
        llm.get(_TRUST_KEYS[1]) is True,
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
