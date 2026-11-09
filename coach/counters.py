"""Per-turn counters over ``SessionState.calls``.

Every cap in the plugin keys on ``"<namespace>:<turn_id>"``: consult, decide,
review holds and corrective delivery. This is the one implementation of the
reserve / release / prune protocol; no other module should build that key or
adjust a counter by hand.

Keys without a namespace are session telemetry (``review_skips``,
``decide_unpriced``, ``id_fallbacks``) and are never pruned.
"""

from __future__ import annotations

from collections.abc import MutableMapping

from .coerce import as_int

__all__ = ["TurnCounters"]


class TurnCounters:
    """Namespace-aware view of one state's ``calls`` mapping."""

    def __init__(self, calls: MutableMapping[str, int]) -> None:
        self._calls = calls

    @staticmethod
    def key(namespace: str, turn_id: str) -> str:
        return f"{namespace}:{turn_id}"

    def get(self, namespace: str, turn_id: str) -> int:
        value = self._calls.get(self.key(namespace, turn_id))
        return max(0, as_int(value, 0))

    def reserve(self, namespace: str, turn_id: str) -> int:
        """Increment one counter and return its new value."""
        value = self.get(namespace, turn_id) + 1
        self._calls[self.key(namespace, turn_id)] = value
        return value

    def release(self, namespace: str, turn_id: str) -> int:
        """Decrement one counter, floored at zero, and return it."""
        value = max(0, self.get(namespace, turn_id) - 1)
        self._calls[self.key(namespace, turn_id)] = value
        return value

    def prune(self, turn_ids: set[str]) -> None:
        """Drop per-turn counters for turns no longer on the ledger."""
        for key in list(self._calls):
            text = str(key)
            if ":" not in text:
                # Global telemetry, not a per-turn counter.
                continue
            suffix = text.rsplit(":", 1)[-1]
            if text not in turn_ids and suffix not in turn_ids:
                del self._calls[key]
