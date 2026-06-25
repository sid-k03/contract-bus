# contract-bus

A tiny MCP message bus that lets **two independent Claude Code sessions** — e.g. one in a
backend repo, one in a frontend repo — exchange messages (typically API contracts) without
a human copy-pasting between windows.

Session A calls `post_message(...)`. Session B calls `read_messages(...)` and sees it.
Messages persist in SQLite, so they survive restarts and give a durable history. It's a
**store-and-forward bus**: no content validation, no push/notify, no auth (localhost only).

## Why a daemon (not stdio)

An stdio MCP server is spawned as a *separate subprocess per client* — two sessions would
each get an isolated copy and share no state. So contract-bus runs as **one long-lived HTTP
daemon** on `127.0.0.1:9100`. Both sessions connect to it as HTTP clients, sharing the same
process and the same SQLite file.

```
  Claude session A (backend)  ─┐
                               ├─ HTTP (MCP) ─▶  contract-bus daemon  ──▶  bus.sqlite3
  Claude session B (frontend) ─┘                 127.0.0.1:9100
```

## Install & run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python bus_server.py        # listens on 127.0.0.1:9100
```

Leave the daemon running **before** either session starts a shared-feature task. By default
it writes `bus.sqlite3` in the working directory; override with `CONTRACT_BUS_DB=/path/to.db`.

### Auto-start at login (macOS, recommended)

Instead of babysitting a terminal, install it as a **LaunchAgent** — starts at login and
respawns on crash (`RunAtLoad` + `KeepAlive`):

```bash
./install-service.sh        # writes ~/Library/LaunchAgents/com.blocksurvey.contract-bus.plist, loads it
```

The agent runs the venv python against an absolute `bus.sqlite3` in this directory, logging
to `~/Library/Logs/com.blocksurvey.contract-bus.log`. Re-run `install-service.sh` after
moving the repo or changing config. Manage it:

```bash
launchctl print gui/$(id -u)/com.blocksurvey.contract-bus   # state + pid
launchctl kickstart -k gui/$(id -u)/com.blocksurvey.contract-bus   # force restart
./uninstall-service.sh      # stop + remove (leaves bus.sqlite3)
```

## Register with Claude Code

Every session points at the same URL → the same daemon → one shared bus.

**Globally, once (recommended)** — available in every project, no per-repo setup:

```bash
claude mcp add --scope user --transport http contract-bus http://127.0.0.1:9100/mcp
claude mcp list        # contract-bus: ... ✔ Connected
```

**Or per-repo** — add to a repo's `.mcp.json` (e.g. to commit it for a team):

```json
{
  "mcpServers": {
    "contract-bus": { "url": "http://127.0.0.1:9100/mcp" }
  }
}
```

Already-open sessions must reconnect (`/mcp` → reconnect, or restart) to pick up the server.
Remove later with `claude mcp remove contract-bus --scope user`. The connection only works
while the daemon is running — the LaunchAgent above keeps it up across reboots.

## Workflow convention

1. **Agree on a channel name per feature**, e.g. `feature-checkout`.
2. Backend session `post_message("feature-checkout", "backend", "<contract>")`.
3. Frontend session `read_messages("feature-checkout", since_id=0)` to read it, then
   `post_message(...)` to reply with questions.
4. **Each session tracks the last `id` it saw** and passes it as `since_id` to fetch only
   newer messages. The server stores no read/unread state — the cursor lives in your session.
5. Use `list_channels()` to discover active threads when you don't know the channel name.

## Tools

| Tool | Purpose |
|------|---------|
| `usage()` | Returns the purpose + workflow guide. Self-documentation for the model. |
| `post_message(channel, author, body)` | Publish a message/contract. Returns `{id, channel, created_at}`. |
| `read_messages(channel, since_id=0, limit=50)` | Messages with `id > since_id`, oldest first, capped at 200. |
| `list_channels()` | Active channels with `{channel, message_count, last_id}`. |

### Auto-discovery

The same guide is also sent as the server's MCP **`instructions`** during the `initialize`
handshake, so a connecting Claude Code session learns what the bus is for and how to use it
**without calling any tool**. The `usage()` tool is the on-demand version of the same text.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/pytest        # core logic against a temp SQLite db, no server needed
```

## Scope (deliberately out)

No schema validation, no push/notifications, no auth/multi-user/remote, no edit/delete
(append-only log), single host only. See `mcp-contract-bus-spec.md` §9.
