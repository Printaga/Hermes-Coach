"""Hermes coach plugin entry point.

Registers ``coach_ask``, ``coach_decide``, the ``/coach`` command and the
coach hooks. Registration always fails open so an older host or a duplicate
registration cannot break the plugin loader.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def register(ctx):
    from .coach.plugin import (
        ASK_DESCRIPTION,
        ASK_SCHEMA,
        DECIDE_DESCRIPTION,
        DECIDE_SCHEMA,
        build_plugin,
    )

    try:
        plugin = build_plugin(ctx)
    except Exception:
        log.exception("coach: could not build plugin")
        return

    register_tool = getattr(ctx, "register_tool", None)
    tools = (
        ("coach_ask", ASK_SCHEMA, plugin.tool_ask, ASK_DESCRIPTION),
        ("coach_decide", DECIDE_SCHEMA, plugin.tool_decide, DECIDE_DESCRIPTION),
    )
    if register_tool is None:
        log.warning("coach: host has no register_tool; coach tools unavailable")
    else:
        for name, schema, handler, description in tools:
            try:
                register_tool(
                    name, "coach", schema, handler, description=description
                )
            except Exception:
                log.exception("coach: could not register %s", name)

    register_command = getattr(ctx, "register_command", None)
    if register_command is None:
        log.warning("coach: host has no register_command; /coach unavailable")
    else:
        try:
            register_command(
                "coach",
                plugin.command,
                description="Coach settings and status",
            )
        except Exception:
            log.exception("coach: could not register /coach command")

    register_hook = getattr(ctx, "register_hook", None)
    if register_hook is None:
        log.warning("coach: host has no register_hook; ambient delivery unavailable")
        return

    hooks = (
        ("pre_verify", plugin.on_pre_verify),
        ("post_tool_call", plugin.on_post_tool_call),
        ("post_llm_call", plugin.on_post_llm_call),
        ("pre_llm_call", plugin.on_pre_llm_call),
        ("subagent_start", plugin.on_subagent_start),
        ("subagent_stop", plugin.on_subagent_stop),
        ("on_session_end", plugin.on_session_end),
    )
    for name, handler in hooks:
        try:
            register_hook(name, handler)
        except Exception:
            log.exception("coach: could not register %s", name)
