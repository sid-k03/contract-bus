# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Implemented. `bus_server.py` (logic + 5 MCP tools + `/wait` and `/register` HTTP routes),
`test_bus_server.py` (41 tests, all passing), `requirements.txt`, `README.md`, `.gitignore`,
and macOS auto-start scripts
(`install-service.sh` / `uninstall-service.sh`). The spec `mcp-contract-bus-spec.md`
remains the v1 design source of truth. Git repo, remote
`https://github.com/sid-k03/contract-bus`.

**v2 (multitenancy + presence) — server layer landed.** Messages can be directed to one
session (`recipient` column; `to=`/`as_handle` filters) or broadcast; a discovery-only
`sessions` presence registry (`list_sessions`, `POST /register`) tracks who's connected and
their `current_task`. Design + plans live in `docs/superpowers/`
(`specs/2026-06-26-contract-bus-multitenancy-hooks-design.md`,
`plans/2026-06-26-contract-bus-v2-server.md`). The **hook pack + skills + Claude-plugin
packaging** that auto-derive handles, auto-register every session, and run the ambient
"always-listening" watcher are the next plan (Plan 2) — not yet built.

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

- **Stateless about routing.** The server stores no read/unread flags and no per-reader
  cursor state. The autoincrement `id` is the only cursor: a reader remembers the highest
  `id` it has seen and passes it as `since_id` to fetch newer messages. The cursor lives in
  the reading session's own context. Addressing (`recipient`) is a **`WHERE`-clause filter**
  (`recipient IS NULL OR recipient = ?`), not server state; the `sessions` presence registry
  is **discovery-only and never consulted by routing**. Do not add server-side read tracking
  or route via the registry — it breaks this model.

- **Append-only log.** No edit, no delete. SQLite table `messages(id, channel, author, body,
  created_at, recipient)` — `recipient` NULL = broadcast, else the addressee's handle. Plus a
  `sessions(handle, repo, status, current_task, last_seen, registered_at)` presence table.
  Indexes on `(channel, id)`, `(channel, recipient, id)`, `(recipient, id)`. WAL at startup.
  `_init` migrates a v1 DB in place (adds the `recipient` column if absent) — idempotent.

- **Tools (lean 5-tool surface).** `post_message(channel, author, body, to=None)`,
  `read_messages(channel=None, since_id=0, limit=50, as_handle=None)`, `list_channels()`,
  `list_sessions()`, plus the non-spec `usage()` self-doc tool. (Exact `post`/`read`/`channels`
  return shapes still match spec §6, now with `recipient`.) The old `wait_for_message` and
  `watch_channel` MCP **tools were removed** — push is now the `/wait` route only (below), keeping
  the model-facing surface minimal. Tool docstrings are load-bearing: the model decides whether
  to call a tool from its description, so docstrings must state *when* to call.

- **Push via bounded long-poll (deliberately relaxes spec §9's "no push" non-goal).** A
  turn-based agent can't act on MCP `notifications/*` — the model only acts when it gets a
  turn, and a notification arriving mid-idle wakes nothing. The only thing that turns "new
  message" into "agent acts" is a tool/HTTP result, because a result IS a turn. So push is a
  **long-poll**: `_wait_for_message` re-runs the inbox `read_messages` query every `poll`
  seconds (default 0.5) until matching mail exists or `MAX_WAIT`=600s elapses, returning [].
  It uses `await asyncio.sleep` so a parked waiter does NOT starve other sessions'
  posts/reads (verified e2e), and bumps the waiter's `last_seen` each poll so a parked-but-alive
  session never ages out of presence. Crucially this stays **stateless about routing** — the
  cursor is still the caller's `since_id`. Exposed via one surface:
  - `/wait` **plain-HTTP route** (mounts at root `/wait`, NOT `/mcp/wait` — confirmed on
    fastmcp 3.4.2 via `@mcp.custom_route`). `GET /wait?as_handle=<h>[&channel=…]&since_id=…&timeout=…`;
    `400` if neither `channel` nor `as_handle` given. Curl-able, so a session runs it as a
    backgrounded shell command and keeps working; the curl exits when mail lands, waking the
    agent ("background like a long bash command"). 0 model tokens while parked — cost is one
    turn per return, so a LONG timeout is cheaper (fewer re-entries), not more expensive. This
    is hook-only (the model never curls); it's a route, not a tool, to keep the surface lean.

- **Presence registry (`POST /register`).** Discovery-only: upserts a `sessions` row (handle,
  repo, status, current_task) and heartbeats `last_seen`. `list_sessions()` reports each
  session's effective status — `offline` once `last_seen` is older than `PRESENCE_TTL`=900s,
  deliberately > `MAX_WAIT` so a long-polling session never flips offline mid-wait. `/register`
  is a POST route (it mutates) and **not** an MCP tool — only the hook layer registers, never
  the model.

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
  tool-surface changes (e.g. the new `list_sessions`) appear without restarting the session.
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

Unit tests alone CANNOT prove the `/wait` and `/register` routes mount where you think or
that a parked long-poll doesn't starve other requests (the helper test passes regardless).
Those are build-time e2e checks: boot the daemon on an alt port + temp DB (so the live
LaunchAgent on 9100 is undisturbed), then (a) `POST /register` and assert `{"handle","status"}`,
(b) `GET /wait` with neither `channel` nor `as_handle` → `400`, and (c) post a directed message
and assert `GET /wait?as_handle=…` wakes with it. (See Plan 1 Task 6 Step 5 for the exact
one-liner.) Re-run these if the fastmcp version or the route/async wiring changes.

## Non-negotiables from the spec

- Bind to `127.0.0.1` only, never `0.0.0.0` — there is no auth.
- Always use bound SQL parameters; never string-format SQL.
- Validate minimally: non-empty `channel` and `body`; clamp `limit` to a max (e.g. 200).
  On bad input return a clear error dict, don't raise raw.
- Explicit non-goals (do not add): content/schema validation, auth, message deletion/editing,
  multi-machine support. **Two exceptions, consciously taken** (do NOT delete either as "spec
  drift" — both are intentional):
  1. **Push/notifications** (spec §9 lists it as out) — relaxed via the bounded long-poll
     above (the `/wait` route). Least-invasive: no consumer cursor state, append-only intact,
     localhost/no-auth unchanged.
  2. **Multitenancy / per-session addressing** (spec's "multi-user" non-goal) — relaxed via
     the `recipient` filter + discovery-only `sessions` presence registry. Routing stays
     stateless (addressing is a WHERE clause; the registry is never consulted by routing),
     append-only intact, still single-host/localhost/no-auth.

  The OTHER non-goals (content/schema validation, auth, edit/delete, multi-machine) still stand.
