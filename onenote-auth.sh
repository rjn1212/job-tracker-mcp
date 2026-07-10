#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

# Usage: ./onenote-auth.sh [client-id]
#
# The client id may be passed as an argument (it is then remembered), set as
# JOB_TRACKER_MS_CLIENT_ID, or omitted if a previous run already saved it.
#
# This is the ONLY interactive entry point. The MCP server can never do this: it
# is a stdio JSON-RPC process with no TTY whose stdout carries the protocol.
exec .venv/bin/python -m job_tracker_mcp.onenote.auth "$@"
