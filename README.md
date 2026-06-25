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

Instead of babysitting a terminal, install it as a **LaunchAgent** — starts at login,
respawns on crash (`RunAtLoad` + `KeepAlive`), and **auto-reloads on code edits**:

```bash
./install-service.sh        # writes ~/Library/LaunchAgents/com.blocksurvey.contract-bus.plist, loads it
```

The agent runs the venv python against an absolute `bus.sqlite3` in this directory, logging
to `~/Library/Logs/com.blocksurvey.contract-bus.log`. Editing `bus_server.py` restarts the
daemon automatically: the server watches its own source (in-process) and exits on change, and
`KeepAlive` respawns it with the new code — so changes go live without a manual kickstart.
(launchd `WatchPaths` is deliberately *not* used — it only starts a stopped job and `KeepAlive`
keeps this one always running, so it never fires.) Reload is immediate in normal use; an edit
made within ~10s of a restart is delayed by launchd's respawn throttle. Connected sessions
auto-reconnect to the restarted daemon and re-discover tools (see registration below). Re-run
`install-service.sh` after moving the repo or changing config. Manage it:

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

A session open at *first-time* registration needs to pick up the new server (it appears once
the session connects). After that, you do **not** need to restart the session: contract-bus is
an **HTTP** server, so when the daemon restarts (auto-reload, reinstall, crash) connected
sessions auto-reconnect with backoff and re-discover tools. Force it immediately with
**`/mcp reconnect contract-bus`** (a space — not `/mcp-reconnect`). Verified against Claude
Code 2.1.191; auto-reconnect is HTTP/SSE-only (stdio servers don't get it). Remove later with
`claude mcp remove contract-bus --scope user`. The connection only works while the daemon is
running — the LaunchAgent above keeps it up across reboots.

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
| `wait_for_message(channel, since_id=0, timeout=60)` | **Blocks** until a message newer than `since_id` arrives, then returns it (or `[]` on timeout). Use when you have nothing else to do. |
| `watch_channel(channel, since_id=0)` | **Non-blocking.** Returns a `curl` command to run in the background; it exits when a newer message lands, waking you while you keep working. |

There's also a plain-HTTP `GET /wait?channel=…&since_id=…&timeout=…` route (mounts at root,
not under `/mcp`) — the curl-able endpoint `watch_channel` hands you. It returns
`{"messages":[…]}` and is what makes background-waiting work.

### Waiting for a reply (push, not hand-polling)

After you post and need the other side's answer, don't re-run `read_messages` in a loop:

- **Block now** — nothing else to do: call `wait_for_message(channel, since_id)`. The session
  freezes until a reply lands (or it times out), then you have it.
- **Keep working** — be pinged in the background: call `watch_channel(channel, since_id)`, then
  run the returned command as a **backgrounded** shell command. It parks server-side and exits
  the instant a newer message arrives, waking the session holding the reply. Advance `since_id`
  to the newest id and call `watch_channel` again to keep listening.

A long-poll spends **zero model tokens while parked** — the cost is one turn per return, so a
long timeout (capped at 600 s) is *cheaper*, not more expensive.

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

No schema validation, no auth/multi-user/remote, no edit/delete (append-only log), single
host only. See `mcp-contract-bus-spec.md` §9. (Push *is* supported — via the bounded
long-poll above — which consciously relaxes that one §9 non-goal without adding consumer
state or auth.)
