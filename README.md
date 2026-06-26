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

### Auto-start at login (macOS, optional)

With the plugin the daemon auto-starts on first join, so this is optional.

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

## Install as a Claude Code plugin (recommended)

One install gives you the daemon, auto-join hooks, skills, and slash commands:

```bash
claude plugin marketplace add sid-k03/contract-bus      # this repo is its own marketplace
/plugin install contract-bus@contract-bus               # in a Claude Code session
```

The plugin **connects** to the shared daemon by URL (`http://127.0.0.1:9100/mcp`); Claude Code
never starts an http MCP server itself. The daemon is brought up on demand by the first session
that joins (`/contract-bus:join`), which provisions a private venv on first run (~30s, cached).
The DB, venv, and state live under `~/.claude/plugins/contract-bus/`, so a plugin-cache update
never destroys mail.

**Use the bus:** `/contract-bus:join <what you're working on>` to opt in,
`/contract-bus:status` to see peers, `/contract-bus:conclude` to wind down. Or just tell Claude
"this task needs the bus" — the `join-contract-bus` skill triggers the same flow. If the tools
don't appear right after the first join (the daemon was starting), run `/mcp reconnect contract-bus`.

> **Pick ONE hook source.** If you previously ran `./install-hooks.sh` (which writes hooks into
> `~/.claude/settings.json`), remove the contract-bus group there before relying on the plugin —
> otherwise every hook fires twice. The plugin's `hooks/hooks.json` replaces that wiring.

**Uninstall:** `/plugin uninstall contract-bus` removes the plugin. To reclaim disk, also delete
the data home: `rm -rf ~/.claude/plugins/contract-bus ~/.contract-bus` (the venv is the bulk).

### Always-on daemon (optional)

By default the daemon starts on first join. To pin it at login regardless (so the very first
session connects with zero reconnect), the LaunchAgent is still available — now optional:
`./install-service.sh` (it serves the same canonical DB).

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
| `post_message(channel, author, body, to=None)` | Publish a message/contract. `to=<handle>` addresses one session; `to=None` broadcasts. Returns `{id, channel, recipient, created_at}`. |
| `read_messages(channel=None, since_id=0, limit=50, as_handle=None)` | Messages with `id > since_id`, oldest first, capped at 200. With `as_handle`: broadcasts + mail addressed to you (omit `channel` to read your mail across all channels). |
| `list_channels()` | Active channels with `{channel, message_count, last_id}`. |
| `list_sessions()` | Connected sessions: `{handle, repo, status, current_task, last_seen}`. `status` ages to `offline` once `last_seen` is older than the presence TTL (900 s). |

### Addressing & presence (v2)

Multiple sessions can share one channel; a message can be **directed** to a single session
or **broadcast** to all. Each session has a **handle** (a stable identifier, e.g.
`backend-a1b2c3d4`). Addressing is a `WHERE`-clause filter on the message log — the server
keeps **no per-reader state**, so the cursor model (`since_id` lives in your context) is
unchanged.

The `sessions` table is a **discovery-only presence registry**: it tells you who is connected
and what they're working on (`list_sessions()`), but routing never consults it. Sessions are
registered/heartbeated via the plain-HTTP `POST /register` route (not an MCP tool — only the
hook layer calls it). The presence TTL (900 s) is deliberately longer than the long-poll cap
(600 s), so a session parked on `/wait` never ages out mid-wait — `/wait` bumps `last_seen`.

> The hook pack + skills + plugin packaging that auto-derive handles, auto-register every
> session, and turn the watcher below into ambient "always-listening" delivery land in a
> follow-up (see `docs/superpowers/`). The primitives here (`to=`, `as_handle`, `/register`,
> `list_sessions`) are what that layer builds on.

There's also a plain-HTTP `GET /wait?as_handle=…[&channel=…]&since_id=…&timeout=…` route
(mounts at root, not under `/mcp`). It long-polls your inbox and returns `{"messages":[…]}`
the instant directed mail lands — curl-able, so a session can run it as a **backgrounded**
shell command and keep working; the curl exits when a message arrives, waking the agent.
It `400`s if neither `channel` nor `as_handle` is given.

### Waiting for a reply (push, not hand-polling)

After you post and need the other side's answer, don't re-run `read_messages` in a loop —
run `GET /wait?as_handle=<your handle>` (optionally scoped to `&channel=…`) as a **backgrounded**
`curl`. It parks server-side and exits the instant a newer message addressed to you arrives,
waking the session holding the reply. Advance `since_id` to the newest id and re-issue to keep
listening.

A long-poll spends **zero model tokens while parked** — the cost is one turn per return, so a
long timeout (capped at 600 s) is *cheaper*, not more expensive.

### Auto-discovery

The same guide is also sent as the server's MCP **`instructions`** during the `initialize`
handshake, so a connecting Claude Code session learns what the bus is for and how to use it
**without calling any tool**. The `usage()` tool is the on-demand version of the same text.

## Auto-coordination (hooks)

Installed once (`./install-hooks.sh` — wires global `~/.claude/settings.json` and links the 3
skills into `~/.claude/skills/`), contract-bus
hooks make a session **auto-join on request**: tell Claude "this task needs the bus" and it
registers a handle, announces its `current_task`, and starts listening for directed mail — no
manual polling. Hooks are **dormant by default**: a session that never joins pays one
file-stat per event (a tiny POSIX gate, no Python) and nothing else.

Two listening modes:
- **Default → background watcher** (`bus_watch.sh`, launched by the model). The model's turn
  ends, so the session goes **idle and free at 0 tokens**, and an agent-launched background
  task is the only thing that can wake an idle session — so the model owns it and the `Stop`
  hook supervises/re-arms it (throttled, so an auto-reload flap can't storm it). This is the
  efficient path whether you're working or just waiting.
- **Fallback → `wait_for_message(as_handle=…, timeout=600)`** (blocking MCP tool). Fully
  documented/guaranteed, but it **blocks and occupies** the session (shows busy; each timeout
  re-queue is a token-costing turn). Use only if the background wake seems unreliable — it is
  *not* the cheap option, despite blocking server-side.

Tear down with the `conclude-bus-session` skill (offline + remove local state). **Honest
limit:** idle-wake via the watcher rests on background-task completion (undocumented Claude
Code behavior, guarded by a build-time canary); if it ever stalls, a `wait_for_message` call
or a human message resumes it. True external "KeepAlive" isn't possible without an external
push into the session. See `docs/superpowers/specs/2026-06-26-contract-bus-multitenancy-hooks-design.md`.

State per session lives under `~/.contract-bus/<session_id>/`. Plugin packaging
(`/plugin install`) is the next step.

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
