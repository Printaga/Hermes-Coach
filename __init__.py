"""Hermes coach plugin entry point.

Registers ``coach_ask`` and the ``pre_llm_call`` hook for ambient delivery.
Later tasks add the remaining hook handlers and the ``/coach`` command;
registration always fails open so an older host or a duplicate registration
cannot break the plugin loader.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def register(ctx):
    from .coach.plugin import ASK_DESCRIPTION, ASK_SCHEMA, build_plugin

    try:
        plugin = build_plugin(ctx)
    except Exception:
        log.exception("coach: could not build plugin")
        return

    register_tool = getattr(ctx, "register_tool", None)
    if register_tool is None:
        log.warning("coach: host has no register_tool; coach_ask unavailable")
    else:
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

    register_hook = getattr(ctx, "register_hook", None)
    if register_hook is None:
        log.warning("coach: host has no register_hook; ambient delivery unavailable")
        return

    hooks = (
        ("post_tool_call", plugin.on_post_tool_call),
        ("post_llm_call", plugin.on_post_llm_call),
        ("pre_llm_call", plugin.on_pre_llm_call),
    )
    for name, handler in hooks:
        try:
            register_hook(name, handler)
        except Exception:
            log.exception("coach: could not register %s", name)
