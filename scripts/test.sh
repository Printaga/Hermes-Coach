#!/usr/bin/env bash
# Run both gates even when one fails, so a pytest failure cannot hide a
# desktop-page regression (and vice versa).
set -uo pipefail
cd "$(dirname "$0")/.."

PYTHON="${COACH_PYTHON:-/home/lenovo/.hermes/hermes-agent/.venv/bin/python}"
status=0

uv run --python "$PYTHON" --with pytest --with pyyaml \
  python -m pytest tests -q "$@" || status=$?

if [ "${SKIP_DESKTOP_CHECK:-0}" != "1" ]; then
  node scripts/check-desktop-page.mjs || status=$?
fi

exit "$status"
