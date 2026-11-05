#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --python /home/lenovo/.hermes/hermes-agent/.venv/bin/python --with pytest --with pyyaml python -m pytest tests -q "$@"
node scripts/check-desktop-page.mjs
