#!/bin/sh
# Plugin conclude wrapper (called by /contract-bus:conclude). Marks this session offline and
# removes its local state under ~/.contract-bus/<session_id>/.
here="$(cd "$(dirname "$0")" && pwd)"
python3 "$here/bus_cli.py" conclude-cli "$CLAUDE_CODE_SESSION_ID"
