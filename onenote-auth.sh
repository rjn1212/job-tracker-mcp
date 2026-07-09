#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

# The ONLY interactive entry point. The MCP server can never do this: it is a
# stdio JSON-RPC process with no TTY whose stdout carries the protocol.
exec .venv/bin/python -m job_tracker_mcp.onenote.auth
