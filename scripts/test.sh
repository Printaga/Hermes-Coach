#!/usr/bin/env bash
# Run both gates even when one fails, so a pytest failure cannot hide a
# desktop-page regression (and vice versa).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# Portable default: any python3 works because uv provides pytest itself.
# Override with COACH_PYTHON to pin the Hermes venv interpreter.
PYTHON="${COACH_PYTHON:-python3}"
status=0

uv run --python "$PYTHON" --with "pytest==9.2.0" --with "pyyaml==6.0.2" \
  --with "fastapi==0.133.1" --with "httpx==0.28.1" \
  python -m pytest tests -q ${1+"$@"} || status=$?

if [ "${SKIP_DESKTOP_CHECK:-0}" != "1" ]; then
  node scripts/check-desktop-page.mjs || status=$?
else
  echo "WARNING: desktop-page check skipped (SKIP_DESKTOP_CHECK=1)" >&2
fi

exit "$status"
