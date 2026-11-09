"""Hermes coach plugin entry point.

Registers ``coach_ask``, ``coach_decide``, the ``/coach`` command and the
coach hooks. Registration always fails open so an older host or a duplicate
registration cannot break the plugin loader.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# The one list of hooks this plugin wires. plugin.yaml declares the same set for
# the host, and tests/test_manifest.py asserts both directions so a deleted or
# misspelled entry cannot leave a hook advertised but never registered.
HOOKS: tuple[tuple[str, str], ...] = (
    ("pre_verify", "on_pre_verify"),
    ("post_tool_call", "on_post_tool_call"),
    ("post_llm_call", "on_post_llm_call"),
    ("pre_llm_call", "on_pre_llm_call"),
    ("subagent_start", "on_subagent_start"),
    ("subagent_stop", "on_subagent_stop"),
    ("on_session_end", "on_session_end"),
)


def register(ctx):
    try:
        from .coach.plugin import (
            ASK_DESCRIPTION,
            ASK_SCHEMA,
            DECIDE_DESCRIPTION,
            DECIDE_SCHEMA,
            build_plugin,
        )
    except Exception:
        log.exception("coach: could not import plugin")
        return

    try:
        plugin = build_plugin(ctx)
    except Exception:
        log.exception("coach: could not build plugin")
        return

    register_tool = getattr(ctx, "register_tool", None)
    # Handler names, resolved per item: one missing attribute must not stop
    # the other tool, the command, or the hooks below.
    tools = (
        ("coach_ask", ASK_SCHEMA, "tool_ask", ASK_DESCRIPTION),
        ("coach_decide", DECIDE_SCHEMA, "tool_decide", DECIDE_DESCRIPTION),
    )
    if register_tool is None:
        log.warning("coach: host has no register_tool; coach tools unavailable")
    else:
        for name, schema, attr, description in tools:
            try:
                handler = getattr(plugin, attr)
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
    else:
        hooks = HOOKS
        for name, attr in hooks:
            try:
                register_hook(name, getattr(plugin, attr))
            except Exception:
                log.exception("coach: could not register %s", name)

    # Unload cleanup registers even when the host has no hook surface: the
    # worker thread must not outlive the plugin.
    on_unload = getattr(ctx, "on_unload", None)
    if not callable(on_unload):
        # The worker thread would outlive the plugin: make the older host
        # visible in the log instead of failing silently.
        log.error("coach: host has no callable on_unload; worker may outlive the plugin")
    else:
        try:
            on_unload(plugin.shutdown)
        except Exception:
            log.exception("coach: could not register on_unload")
