---
name: conclude-bus-session
description: Use when the human deems this session's bus work finished ("wind down the bus session" / "we're done with the bus") — marks the session offline, stops its watcher, and removes its local bus state.
---

# Conclude the bus session

The human has decided this session's bus work is done. Tear it down:

```bash
python3 "<plugin root>/bus_cli.py" conclude-cli "$CLAUDE_CODE_SESSION_ID"
```

This registers you `offline`, removes the watcher pid, and `rm -rf`s your
`~/.contract-bus/<session_id>/` state dir. Confirm to the human what was cleaned up. If a
watcher is still parked in the background, stop it.

This is distinct from simply ending the session, which keeps state so a `--resume` can
re-arm. Conclude only when the coordination work itself is finished.
