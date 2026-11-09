#!/usr/bin/env bash
# Run both gates even when one fails, so a pytest failure cannot hide a
# desktop-page regression (and vice versa).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# Portable default: any python3 works because uv provides pytest itself.
# Override with COACH_PYTHON to pin the Hermes venv interpreter.
PYTHON="${COACH_PYTHON:-python3}"
status=0

# Bash < 4.4 aborts on "${@:-tests}" under `set -u` when there are no
# positional parameters, so the default is applied without touching "$@".
args=("$@")
if [ "$#" -eq 0 ]; then
  args=(tests)
fi

# ruff.toml is a real policy: run it here or it is a config nothing enforces.
uvx ruff check . || status=$?
uv run --python "$PYTHON" --with "pytest==9.2.0" --with "pyyaml==6.0.2" \
  --with "fastapi==0.133.1" --with "httpx==0.28.1" \
  python -m pytest -q "${args[@]}" || status=$?

if [ "${SKIP_DESKTOP_CHECK:-0}" != "1" ]; then
  node scripts/check-desktop-page.mjs || status=$?
else
  echo "WARNING: desktop-page check skipped (SKIP_DESKTOP_CHECK=1)" >&2
fi

exit "$status"
