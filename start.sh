#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

# Invoke the venv interpreter directly. `source .venv/bin/activate` only mutates
# PATH, which `exec python` then reads -- one more moving part for no benefit.
# Claude Desktop launches this with a minimal environment, so nothing here may
# depend on PATH or a shell profile.
exec .venv/bin/python -m job_tracker_mcp.server
