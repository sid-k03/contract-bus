---
name: conclude-bus-session
description: Use when the human deems this session's bus work finished ("wind down the bus session" / "we're done with the bus") — marks the session offline, stops its watcher, and removes its local bus state.
---

# Conclude the bus session

The human has decided this session's bus work is done. Tear it down:

Run the **`/contract-bus:conclude`** slash command (marks this session offline, removes local
state under `~/.contract-bus/<session_id>/`). Then stop any background watcher you launched.
(Manual install: `python3 "<repo>/bus_cli.py" conclude-cli "$CLAUDE_CODE_SESSION_ID"`.)

Confirm to the human what was cleaned up.

This is distinct from simply ending the session, which keeps state so a `--resume` can
re-arm. Conclude only when the coordination work itself is finished.
