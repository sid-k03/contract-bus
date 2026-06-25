# Spec: `contract-bus` — a shared MCP message bus for cross-session Claude Code

## 1. Purpose

A tiny MCP server that lets **two independent Claude Code sessions** (e.g. one in a
frontend repo, one in a backend repo) exchange messages — typically API contracts —
**without a human copy-pasting between windows**.

Session A calls `post_message(...)`. Session B calls `read_messages(...)` and sees it.
Messages persist in SQLite, so they survive restarts and give a durable history.

This is a **store-and-forward bus**. It does NOT validate message contents, does NOT
push/notify, does NOT do auth. Those are explicit non-goals (see §9).

## 2. Why a daemon (not stdio), and why this matters

**Critical architectural constraint:** an MCP server registered over **stdio** is spawned
as a *separate subprocess per client*. Two Claude sessions would each get their own
isolated copy — they would NOT share state. That defeats the purpose.

Therefore this server runs as **one long-lived HTTP daemon**. Both sessions connect to it
as HTTP clients over localhost, so they share the same process and the same SQLite file.

```
  ┌────────────────────┐         ┌────────────────────┐
  │ Claude session A   │         │ Claude session B   │
  │ (backend repo)     │         │ (frontend repo)    │
  └─────────┬──────────┘         └──────────┬─────────┘
            │  HTTP (MCP)                    │  HTTP (MCP)
            └───────────────┬────────────────┘
                            ▼
                ┌───────────────────────┐
                │  contract-bus daemon  │
                │  FastMCP, port 9100   │
                │   └── bus.sqlite3     │
                └───────────────────────┘
```

## 3. Tech stack

- **Python 3.11+**
- **FastMCP** (the MCP Python framework) — HTTP transport
- **SQLite** via the stdlib `sqlite3` module (no ORM, no extra dep)
- No other runtime dependencies

> Version note for the builder: FastMCP's HTTP transport arg has changed across versions
> (`transport="http"` in current 2.x; older builds used `"streamable-http"`). Confirm
> against the installed version and use whichever that version documents. The MCP client
> URL is correspondingly `http://127.0.0.1:9100/mcp`.

## 4. Repository layout

```
contract-bus/
├── README.md
├── pyproject.toml          # or requirements.txt — deps: fastmcp
├── bus_server.py           # the entire server (~120 lines)
└── bus.sqlite3             # created at runtime, gitignored
```

Keep it one file. Resist splitting until there's a reason.

## 5. Data model (SQLite)

One table.

```sql
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- monotonic, used as the read cursor
    channel    TEXT    NOT NULL,                    -- logical thread, e.g. "feature-checkout"
    author     TEXT    NOT NULL,                    -- free-form, e.g. "backend" / "frontend"
    body       TEXT    NOT NULL,                    -- the message / contract (markdown or JSON string)
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel, id);
```

**Concurrency:** enable WAL mode at startup (`PRAGMA journal_mode=WAL;`). Because the
daemon is single-process and FastMCP handles requests on one event loop, write contention
is minimal; WAL plus SQLite's default locking is sufficient. Open the connection with
`check_same_thread=False` only if FastMCP runs handlers off-thread; otherwise a single
module-level connection is fine.

**Cursor model:** `id` is the only thing a reader needs to track. A reader remembers the
highest `id` it has seen and passes it as `since_id` to get only newer messages. No
read/unread flags, no per-reader state stored server-side — the cursor lives in the
reading session's own context. This keeps the server stateless about consumers.

## 6. MCP tool surface

Exactly three tools. Signatures and exact return shapes:

### `post_message(channel: str, author: str, body: str) -> dict`
Insert one message. Returns the new row's id and timestamp.
```json
{ "id": 42, "channel": "feature-checkout", "created_at": "2026-06-25 14:03:11" }
```

### `read_messages(channel: str, since_id: int = 0, limit: int = 50) -> list[dict]`
Return messages on `channel` with `id > since_id`, oldest first, capped at `limit`.
The caller advances its cursor to the max `id` returned.
```json
[
  { "id": 41, "channel": "feature-checkout", "author": "backend",
    "body": "POST /api/v1/checkout takes {cart_id:int}, returns {order_id:int,status:str}",
    "created_at": "2026-06-25 14:01:02" },
  { "id": 42, "channel": "feature-checkout", "author": "frontend",
    "body": "Ack. Need status enum values — what are they?",
    "created_at": "2026-06-25 14:03:11" }
]
```

### `list_channels() -> list[dict]`
Discovery helper so a session can see active threads without guessing channel names.
```json
[ { "channel": "feature-checkout", "message_count": 2, "last_id": 42 } ]
```

> Tool docstrings matter: the model decides whether to call a tool from its description.
> Make each docstring state plainly when to use it (e.g. read_messages: "Call at the start
> of work on a shared feature, and again with the last id you saw to fetch replies").

## 7. Server behavior

- On startup: open/create the SQLite file, set WAL, create table + index if absent.
- `post_message`: trivial INSERT; return the generated id + created_at.
- `read_messages`: parameterized SELECT (`WHERE channel = ? AND id > ? ORDER BY id LIMIT ?`).
  Always use bound parameters — never string-format SQL.
- `list_channels`: `SELECT channel, COUNT(*), MAX(id) ... GROUP BY channel`.
- Validate inputs minimally: non-empty `channel` and `body`; clamp `limit` to a sane max
  (e.g. 200). On bad input return a clear error dict, don't raise raw.
- Bind to `127.0.0.1` only (localhost). Do not expose on `0.0.0.0` — there's no auth.

## 8. Running and registering it

**Run the daemon** (once, leave it running in a terminal or as a background service):
```bash
python bus_server.py        # listens on 127.0.0.1:9100
```

**Register in each Claude Code session.** In each repo's `.mcp.json` (or via
`claude mcp add`):
```json
{
  "mcpServers": {
    "contract-bus": { "url": "http://127.0.0.1:9100/mcp" }
  }
}
```
Both repos point at the same URL → same daemon → shared bus.

**Usage convention** (document in README): agree on a channel name per feature
(e.g. `feature-checkout`). Backend session posts the contract; frontend session reads it,
posts questions; backend reads replies. Each session tracks the last `id` it saw.

## 9. Out of scope (deliberately)

- **Validation** of message contents against any schema/OpenAPI — dropped as overhead.
- **Push / notifications** — ~~neither session is woken; each must call `read_messages` on
  its own turn.~~ **Amended:** the foreseen `wait_for_message` long-poll was implemented
  (plus a `watch_channel` directive tool and a curl-able `/wait` route). It's bounded
  long-poll, not MCP `notifications/*` (which a turn-based agent can't act on), and adds no
  consumer state — the `since_id` cursor model is unchanged. See CLAUDE.md → Architecture.
- **Auth / multi-user / remote access** — localhost, single trusted user.
- **Deletion / editing** of messages — append-only log.
- **Multiple machines** — same-host only (shared localhost daemon).

## 10. Acceptance criteria (smoke test)

1. Start the daemon. Confirm `bus.sqlite3` is created and the process listens on 9100.
2. From a Python REPL or an MCP client, `post_message("t", "backend", "hello")` → returns `id: 1`.
3. `read_messages("t", since_id=0)` → returns the one message.
4. `read_messages("t", since_id=1)` → returns `[]` (cursor works).
5. `post_message("t", "frontend", "hi back")`; `read_messages("t", since_id=1)` → returns only the 2nd message.
6. `list_channels()` → shows `t` with `message_count: 2, last_id: 2`.
7. Restart the daemon; `read_messages("t", 0)` → still returns both messages (persistence works).
8. Register in two real Claude Code sessions in different repos; confirm one can read what the other posted.

## 11. Reference implementation sketch (non-binding)

```python
import sqlite3
from fastmcp import FastMCP

DB = "bus.sqlite3"
mcp = FastMCP("contract-bus")

def _db():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _init():
    with _db() as c:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("""CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_ch_id ON messages(channel, id);")

@mcp.tool()
def post_message(channel: str, author: str, body: str) -> dict:
    """Publish a message/contract to a channel so the other session can read it."""
    if not channel or not body:
        return {"error": "channel and body are required"}
    with _db() as c:
        cur = c.execute(
            "INSERT INTO messages(channel, author, body) VALUES (?,?,?)",
            (channel, author, body))
        row = c.execute("SELECT id, created_at FROM messages WHERE id=?",
                        (cur.lastrowid,)).fetchone()
    return {"id": row["id"], "channel": channel, "created_at": row["created_at"]}

@mcp.tool()
def read_messages(channel: str, since_id: int = 0, limit: int = 50) -> list[dict]:
    """Read messages on a channel newer than since_id (oldest first). Track the max id you see."""
    limit = max(1, min(limit, 200))
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE channel=? AND id>? ORDER BY id LIMIT ?",
            (channel, since_id, limit)).fetchall()
    return [dict(r) for r in rows]

@mcp.tool()
def list_channels() -> list[dict]:
    """List active channels with message counts, to discover ongoing threads."""
    with _db() as c:
        rows = c.execute(
            "SELECT channel, COUNT(*) n, MAX(id) last_id FROM messages GROUP BY channel"
        ).fetchall()
    return [{"channel": r["channel"], "message_count": r["n"], "last_id": r["last_id"]}
            for r in rows]

if __name__ == "__main__":
    _init()
    mcp.run(transport="http", host="127.0.0.1", port=9100)  # confirm transport arg vs FastMCP version
```

## 12. README must document

- One-line install + run command.
- The `.mcp.json` snippet for registering in a session.
- The channel-naming convention and the cursor (`since_id`) workflow.
- That the daemon must be running before either session starts a shared-feature task.
