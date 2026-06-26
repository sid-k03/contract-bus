---
description: Conclude this session's contract-bus work and remove its local state.
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/bus_conclude.sh:*)"]
---
Tear down this session's bus participation (offline + remove local state):

```!
"${CLAUDE_PLUGIN_ROOT}/bus_conclude.sh"
```

Then stop any background watcher you launched for this session, and tell the human what was cleaned up.
