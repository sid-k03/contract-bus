"""contract-bus — a shared MCP message bus for cross-session Claude Code.

A store-and-forward bus: one long-lived HTTP daemon backed by SQLite. Two independent
Claude Code sessions (e.g. a backend repo and a frontend repo) connect to the same
daemon over localhost and exchange messages — typically API contracts — without a human
copy-pasting between windows.

Why a daemon, not stdio: an stdio MCP server is spawned per-client, so two sessions get
isolated copies and share no state. One HTTP daemon = one process = one shared SQLite file.

Run:      python bus_server.py            # listens on 127.0.0.1:9100
Register: {"mcpServers": {"contract-bus": {"url": "http://127.0.0.1:9100/mcp"}}}
"""
import os
import sqlite3

from fastmcp import FastMCP

DB = os.environ.get("CONTRACT_BUS_DB", "bus.sqlite3")
HOST = "127.0.0.1"  # localhost only — there is no auth (see spec §9)
PORT = 9100
MAX_LIMIT = 200

# Sent to every client during the MCP `initialize` handshake (FastMCP `instructions`),
# AND returned by the `usage` tool. So a session discovers what this bus is for the moment
# it connects — no tool call required — and can re-read the guide on demand mid-task.
GUIDE = """\
contract-bus — a shared message bus for coordinating TWO Claude Code sessions working on
the same feature in DIFFERENT repos (e.g. backend + frontend), without a human relaying
messages. It is a durable store-and-forward log (SQLite); it does not validate, notify, or
authenticate.

When to reach for it: you're implementing one side of a contract the other repo's session
also needs — an API shape, a schema, an enum, an answer to their question. Post it here
instead of asking the human to copy-paste.

Workflow:
- Agree on a `channel` name per feature, e.g. "feature-checkout". `author` is free-form
  ("backend"/"frontend").
- post_message(channel, author, body): publish a contract/message/answer.
- read_messages(channel, since_id=0, limit=50): at the start of shared work read with
  since_id=0; then track the highest `id` you've seen and pass it as since_id to fetch only
  NEW replies. The server stores no read state — the cursor lives in your context.
- list_channels(): discover active threads when you don't know the channel name.

Etiquette: read before you start a shared-feature task, post decisions as you make them,
and re-read with your last seen id before assuming the other side hasn't replied."""

mcp = FastMCP("contract-bus", instructions=GUIDE)


# --- storage helpers (pure logic, unit-tested against a temp DB) -----------

def _connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init(db: str = DB) -> None:
    """Create the messages table + index and enable WAL. Idempotent."""
    with _connect(db) as c:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute(
            """CREATE TABLE IF NOT EXISTS messages(
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                channel    TEXT NOT NULL,
                author     TEXT NOT NULL,
                body       TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')))"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel, id)"
        )


def _post_message(db: str, channel: str, author: str, body: str) -> dict:
    if not channel or not body:
        return {"error": "channel and body are required"}
    with _connect(db) as c:
        cur = c.execute(
            "INSERT INTO messages(channel, author, body) VALUES (?,?,?)",
            (channel, author, body),
        )
        row = c.execute(
            "SELECT id, created_at FROM messages WHERE id=?", (cur.lastrowid,)
        ).fetchone()
    return {"id": row["id"], "channel": channel, "created_at": row["created_at"]}


def _read_messages(db: str, channel: str, since_id: int = 0, limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, MAX_LIMIT))
    with _connect(db) as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE channel=? AND id>? ORDER BY id LIMIT ?",
            (channel, since_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _usage() -> str:
    return GUIDE


def _list_channels(db: str) -> list[dict]:
    with _connect(db) as c:
        rows = c.execute(
            "SELECT channel, COUNT(*) n, MAX(id) last_id "
            "FROM messages GROUP BY channel ORDER BY channel"
        ).fetchall()
    return [
        {"channel": r["channel"], "message_count": r["n"], "last_id": r["last_id"]}
        for r in rows
    ]


# --- MCP tool surface (thin wrappers; docstrings tell the model when to call) ----

@mcp.tool()
def usage() -> str:
    """Explain what the contract-bus is for and how to use it. Call this first if you're
    unsure whether/how to use this server — it returns the purpose, the channel/since_id
    workflow, and when to post vs read."""
    return _usage()


@mcp.tool()
def post_message(channel: str, author: str, body: str) -> dict:
    """Publish a message/contract to a channel so the other Claude session can read it.

    Use when you've decided something the other session needs — an API contract, a
    schema, an answer to a question. `channel` is the shared feature thread (agree on a
    name, e.g. "feature-checkout"). `author` is free-form ("backend"/"frontend").
    Returns the new message's id and created_at.
    """
    return _post_message(DB, channel, author, body)


@mcp.tool()
def read_messages(channel: str, since_id: int = 0, limit: int = 50) -> list[dict]:
    """Read messages on a channel newer than since_id (oldest first), capped at limit.

    Call at the start of work on a shared feature with since_id=0, then again later with
    the highest id you've already seen to fetch only replies. Track that max id yourself
    — the server stores no read state.
    """
    return _read_messages(DB, channel, since_id, limit)


@mcp.tool()
def list_channels() -> list[dict]:
    """List active channels with message counts and last id, to discover ongoing threads.

    Use when you don't know the channel name, to see what conversations are in flight.
    """
    return _list_channels(DB)


if __name__ == "__main__":
    _init()
    mcp.run(transport="http", host=HOST, port=PORT)
