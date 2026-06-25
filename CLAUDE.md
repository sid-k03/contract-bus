# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Implemented. `bus_server.py` (logic + 6 MCP tools + a `/wait` HTTP route),
`test_bus_server.py` (21 tests, all passing), `requirements.txt`, `README.md`, `.gitignore`,
and macOS auto-start scripts
(`install-service.sh` / `uninstall-service.sh`). The spec `mcp-contract-bus-spec.md`
remains the design source of truth. Git repo on `main`, remote
`https://github.com/sid-k03/contract-bus`.

The auto-start scripts generate a per-user LaunchAgent `com.blocksurvey.contract-bus`
(`~/Library/LaunchAgents/…plist`) with `RunAtLoad`+`KeepAlive`, pinning an absolute
`CONTRACT_BUS_DB` and logging to `~/Library/Logs/com.blocksurvey.contract-bus.log`. The
plist is generated from the install script (not committed) so paths resolve at install
time — re-run after moving the repo. LaunchAgent (login session, user perms), deliberately
not a LaunchDaemon, since the bus is localhost-only and single-user.

Auto-reload on code edits is handled **in-process**, NOT by launchd `WatchPaths`. The
`_watch_source_and_exit` daemon thread (started in `__main__`) polls this file's mtime every
2s and `os._exit(0)`s on change; `KeepAlive` then respawns the daemon with fresh code.
`WatchPaths` was tried and **empirically does not work here**: it only *starts* a stopped
job, but `KeepAlive` keeps this one always running, so it's inert (don't re-add it). The
in-process approach also survives atomic-rename saves that would orphan a `WatchPaths`
vnode. Reload = process restart (parked long-polls drop; curls re-issue), not hot-swap.
Note launchd throttles respawns to ~once/10s, so an edit made <10s after a (re)start is
delayed to the 10s mark; in normal use (daemon up a long time) reload is immediate.
Connected Claude Code sessions auto-reconnect to the restarted daemon (HTTP transport, see
"MCP reconnect" below) and re-discover tools — no session restart needed.

Deps live in a local `.venv/` (gitignored): `python -m venv .venv && .venv/bin/pip install
-r requirements.txt`. Add `pytest` for tests.

## What this is

`contract-bus` — a tiny MCP server that lets **two independent Claude Code sessions**
(e.g. one in a backend repo, one in a frontend repo) exchange messages — typically API
contracts — without a human copy-pasting between windows. Store-and-forward bus backed by
SQLite. Session A `post_message(...)`; session B `read_messages(...)` sees it.

## Architecture (the parts that require reading the spec to understand)

- **HTTP daemon, NOT stdio — this is the whole point.** An stdio MCP server is spawned as
  a separate subprocess *per client*, so two sessions would each get an isolated copy and
  share no state. Therefore the server runs as one long-lived HTTP daemon on
  `127.0.0.1:9100`; both sessions connect as HTTP clients to the same process + same
  SQLite file. Do not "simplify" this to stdio.

- **Stateless about consumers.** The server stores no read/unread flags and no per-reader
  state. The autoincrement `id` is the only cursor: a reader remembers the highest `id`
  it has seen and passes it as `since_id` to fetch newer messages. The cursor lives in the
  reading session's own context. Do not add server-side read tracking — it breaks this model.

- **Append-only log.** No edit, no delete. One SQLite table `messages(id, channel, author,
  body, created_at)` with index on `(channel, id)`. WAL mode enabled at startup.

- **Tools:** the three spec tools — `post_message(channel, author, body)`,
  `read_messages(channel, since_id=0, limit=50)`, `list_channels()` (exact return shapes
  pinned in spec §6, match them) — plus a non-spec `usage()` self-doc tool and the two
  push tools below (`wait_for_message`, `watch_channel`). Tool docstrings are load-bearing:
  the model decides whether to call a tool from its description, so docstrings must state
  *when* to call.

- **Push via bounded long-poll (deliberately relaxes spec §9's "no push" non-goal).** A
  turn-based agent can't act on MCP `notifications/*` — the model only acts when it gets a
  turn, and a notification arriving mid-idle wakes nothing. The only thing that turns "new
  message" into "agent acts" is a tool/HTTP result, because a result IS a turn. So push is a
  **blocking long-poll**: `_wait_for_message` re-runs the `read_messages` query every `poll`
  seconds (default 0.5) until `id > since_id` exists or `MAX_WAIT`=600s elapses, returning
  []. It uses `await asyncio.sleep` so a parked waiter does NOT starve other sessions'
  posts/reads (verified e2e). Crucially this stays **stateless about consumers** — the
  cursor is still the caller's `since_id`, no server-side read state. Two surfaces share the
  one helper:
  - `wait_for_message` MCP tool — synchronous; blocks the calling session until a reply.
  - `/wait` **plain-HTTP route** (mounts at root `/wait`, NOT `/mcp/wait` — confirmed on
    fastmcp 3.4.2 via `@mcp.custom_route`) — curl-able, so a session can run it as a
    backgrounded shell command and keep working; the curl exits when a message lands, which
    wakes the agent (this is how you get "background like a long bash command"). 0 model
    tokens are spent while a long-poll is parked — cost is one turn per return, so a LONG
    timeout is cheaper (fewer re-entries), not more expensive.
  - `watch_channel` MCP tool — a **directive tool**: it does no waiting itself, it returns
    the exact backgroundable `curl` command (via `_watch_command`) for the agent to run.
    The server stays dumb; the harness's existing background-bash machinery does the waiting.

- **Self-documentation (two channels, one source).** A single `GUIDE` string is both passed
  as FastMCP `instructions=` (pushed to every client during the `initialize` handshake, so
  a session discovers the bus's purpose/workflow with no tool call) and returned by the
  `usage()` tool (on-demand pull). Keep them one source — edit `GUIDE`, not two copies.

- **Keep it one file.** `bus_server.py`. Resist splitting until there's a reason.

## Stack

Python 3.11+, FastMCP (HTTP transport), stdlib `sqlite3` (no ORM). No other runtime deps.
Confirmed against **fastmcp 3.4.2**.

> FastMCP's HTTP transport arg has changed across versions. On 3.x (and current 2.x) use
> `mcp.run(transport="http", host=..., port=...)`; older builds used `"streamable-http"`.
> Re-confirm if the installed version changes. MCP client URL is
> `http://127.0.0.1:9100/mcp`.

## Commands

Run the daemon (leave running before either session starts a shared-feature task):
```bash
.venv/bin/python bus_server.py        # listens on 127.0.0.1:9100
```
`CONTRACT_BUS_DB=/path/to.db` overrides the SQLite location (default `bus.sqlite3` in cwd).

Run tests:
```bash
.venv/bin/pytest
```

Register with Claude Code (all sessions point at the same URL → shared bus). Global, once:
```bash
claude mcp add --scope user --transport http contract-bus http://127.0.0.1:9100/mcp
```
Or per-repo `.mcp.json`:
```json
{ "mcpServers": { "contract-bus": { "url": "http://127.0.0.1:9100/mcp" } } }
```

**MCP reconnect (verified against Claude Code 2.1.191, official docs).** Because the bus is
**HTTP** transport, a running session does NOT need a full restart to recover the connection:
- When the daemon restarts (auto-reload, reinstall, crash-respawn), connected sessions
  **auto-reconnect** with exponential backoff (5 attempts, ~31s) and re-run `tools/list`, so
  new tools (`wait_for_message`, `watch_channel`) appear without restarting the session.
- Force it immediately with the slash command **`/mcp reconnect contract-bus`** (a space —
  it is NOT `/mcp-reconnect`).
- The server also supports `list_changed`, so tool-list changes can propagate with no
  reconnect at all.
- This auto-reconnect is HTTP/SSE-only; stdio MCP servers do not get it — another reason
  this server is an HTTP daemon (see Architecture). *First-time* registration (a brand-new
  `claude mcp add` / new `.mcp.json` entry) still needs the session to pick up the server.

## Verifying changes

`test_bus_server.py` covers the spec §10 acceptance criteria as unit tests against a temp
SQLite db (cursor, limit clamp, validation, per-channel isolation, persistence) — run
`.venv/bin/pytest`. Tests import the pure `_helpers`, NOT the `@mcp.tool` wrappers (in
fastmcp 3.x the decorated tool is a non-callable `Tool` object), so keep logic in the
helpers. Async helpers (`_wait_for_message`) are tested from sync tests via `asyncio.run(...)`
with a fast `poll=0.01`, driving the mid-wait insert from an `asyncio.create_task` — no
`pytest-asyncio` dependency.

Unit tests alone CANNOT prove the `/wait` route mounts where you think or that a parked
long-poll doesn't starve other requests (the helper test passes regardless). Those are
build-time e2e checks: boot the daemon, then (a) curl `/wait` to confirm the root mount, and
(b) park a backgrounded curl on `/wait` while a `fastmcp.Client` posts — assert the curl
wakes with the message AND a concurrent `list_channels` returns fast. Re-run these if the
fastmcp version or the route/async wiring changes.

## Non-negotiables from the spec

- Bind to `127.0.0.1` only, never `0.0.0.0` — there is no auth.
- Always use bound SQL parameters; never string-format SQL.
- Validate minimally: non-empty `channel` and `body`; clamp `limit` to a max (e.g. 200).
  On bad input return a clear error dict, don't raise raw.
- Explicit non-goals (do not add): content/schema validation, auth, multi-user, message
  deletion/editing, multi-machine support. **Exception, consciously taken:** spec §9 also
  lists "push/notifications" as out — we relaxed *that one* via the bounded long-poll above
  (`wait_for_message` / `watch_channel` / `/wait`). It's the least-invasive form: no
  consumer state, append-only intact, localhost/no-auth unchanged. Do NOT delete it as
  "spec drift" — it is intentional. The OTHER non-goals still stand.
