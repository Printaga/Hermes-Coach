"""Hermes coach plugin entry point.

Only ``coach_ask`` is registered here for now. Later tasks add the hook
handlers and the ``/coach`` command; registration always fails open so an
older host or a duplicate registration cannot break the plugin loader.
"""

from __future__ import annotations

import logging

from coach.plugin import ASK_DESCRIPTION, ASK_SCHEMA, build_plugin

log = logging.getLogger(__name__)


def register(ctx):
    try:
        plugin = build_plugin(ctx)
    except Exception:
        log.exception("coach: could not build plugin")
        return

    register_tool = getattr(ctx, "register_tool", None)
    if register_tool is None:
        log.warning("coach: host has no register_tool; coach_ask unavailable")
        return

    try:
        register_tool(
            "coach_ask",
            "coach",
            ASK_SCHEMA,
            plugin.tool_ask,
            description=ASK_DESCRIPTION,
        )
    except Exception:
        log.exception("coach: could not register coach_ask")
