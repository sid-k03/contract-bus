#!/bin/sh
# Plugin join wrapper (called by /contract-bus:join, which provides CLAUDE_PLUGIN_ROOT +
# CLAUDE_CODE_SESSION_ID). Ensure the shared daemon is up, then join THIS session, deriving the
# handle from the git project root (stable across cd/subdir launch). git/python run inside this
# shell, so one allowed-tools matcher on this script covers the whole flow.
here="$(cd "$(dirname "$0")" && pwd)"
python3 "$here/bus_cli.py" ensure-daemon
root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 "$here/bus_cli.py" join-cli "$CLAUDE_CODE_SESSION_ID" "$root" "$*"
