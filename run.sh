#!/usr/bin/env bash
# iPad Desk Monitor — arranque simple (sin venv, sin dependencias).
set -euo pipefail
cd "$(dirname "$0")"
exec python3 server.py --host "${HOST:-0.0.0.0}" --port "${PORT:-8772}" "$@"
