# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Implemented. `bus_server.py` (logic + 4 MCP tools), `test_bus_server.py` (16 tests, all
passing), `requirements.txt`, `README.md`, `.gitignore`, and macOS auto-start scripts
(`install-service.sh` / `uninstall-service.sh`). The spec `mcp-contract-bus-spec.md`
remains the design source of truth. Git repo on `main`, remote
`https://github.com/sid-k03/contract-bus`.

The auto-start scripts generate a per-user LaunchAgent `com.blocksurvey.contract-bus`
(`~/Library/LaunchAgents/…plist`) with `RunAtLoad`+`KeepAlive`, pinning an absolute
`CONTRACT_BUS_DB` and logging to `~/Library/Logs/com.blocksurvey.contract-bus.log`. The
plist is generated from the install script (not committed) so paths resolve at install
time — re-run after moving the repo. LaunchAgent (login session, user perms), deliberately
not a LaunchDaemon, since the bus is localhost-only and single-user.

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
  pinned in spec §6, match them) — plus a non-spec `usage()` self-doc tool. Tool docstrings
  are load-bearing: the model decides whether to call a tool from its description, so
  docstrings must state *when* to call.

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
Already-open sessions must reconnect (`/mcp`) to pick up the server.

## Verifying changes

`test_bus_server.py` covers the spec §10 acceptance criteria as unit tests against a temp
SQLite db (cursor, limit clamp, validation, per-channel isolation, persistence) — run
`.venv/bin/pytest`. Tests import the pure `_helpers`, NOT the `@mcp.tool` wrappers (in
fastmcp 3.x the decorated tool is a non-callable `Tool` object), so keep logic in the
helpers. For an end-to-end check, run the daemon and drive it with `fastmcp.Client(
"http://127.0.0.1:9100/mcp")`.

## Non-negotiables from the spec

- Bind to `127.0.0.1` only, never `0.0.0.0` — there is no auth.
- Always use bound SQL parameters; never string-format SQL.
- Validate minimally: non-empty `channel` and `body`; clamp `limit` to a max (e.g. 200).
  On bad input return a clear error dict, don't raise raw.
- Explicit non-goals (do not add): content/schema validation, push/notifications, auth,
  multi-user, message deletion/editing, multi-machine support.
