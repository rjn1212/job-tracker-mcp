#!/bin/bash
# Start the local, read-only job-tracker web viewer.
#   ./start.sh            # http://127.0.0.1:8787
#   PORT=9000 ./start.sh  # pick a port
set -euo pipefail

cd "$(dirname "$0")"

PY="../.venv/bin/python"
[ -x "$PY" ] || PY="python3"

exec "$PY" server.py
