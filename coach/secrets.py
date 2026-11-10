"""Credential reads through the host's profile-scoped secret scope.

Catalog rule 11: a plugin never reads ``.env`` files, ``auth.json`` or
another tool's credential store directly. Declared ``requires_env`` keys are
read with ``agent.secret_scope.get_secret``, which honours the active
profile — a bare ``os.environ`` read can pick up another profile's key when
the multiplexing gateway serves several profiles from one process.

The host import is resolved lazily so unit tests and older single-profile
hosts (which predate both the module and multiplexing, so ``os.environ`` is
their only route and a safe one) keep working.
"""

from __future__ import annotations

import os

__all__ = ["get_secret"]


def get_secret(name: str) -> str:
    """Return the credential ``name`` for the active profile, or ``""``.

    Never raises: under multiplexing with no scope bound the host raises
    ``UnscopedSecretError`` instead of guessing, and the caller (a tool
    handler that must answer JSON, never raise) treats that as unset.
    """
    try:
        from agent.secret_scope import get_secret as host_get
    except ImportError:
        return str(os.environ.get(name) or "")
    try:
        return str(host_get(name, "") or "")
    except Exception:
        return ""
