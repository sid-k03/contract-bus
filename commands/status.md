---
description: Show who is connected to the contract bus and what they're working on.
---
Call `list_sessions()` and report each peer's handle, status (online/offline), and current_task.
If the contract-bus tools are unavailable the daemon is down — run `/contract-bus:join` to bring
it up (or `/mcp reconnect contract-bus` if it just started), or tell the human the bus isn't running.
