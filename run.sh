#!/usr/bin/env bash
# Launch Midnight Signal (uses the .venv created by scripts/install.sh if present).
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
if [ -x .venv/bin/python ]; then exec .venv/bin/python midnight_signal.py "$@"; fi
exec python3 midnight_signal.py "$@"
