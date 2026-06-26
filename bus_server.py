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
import asyncio
import os
import sqlite3
import threading
import time
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

DB = os.environ.get("CONTRACT_BUS_DB", "bus.sqlite3")
HOST = "127.0.0.1"  # localhost only — there is no auth (see spec §9)
PORT = 9100
MAX_LIMIT = 200
MAX_WAIT = 600  # cap (seconds) on a single long-poll; clients re-issue to keep waiting
PRESENCE_TTL = 900  # seconds; a session whose last_seen is older reports offline.
                    # > MAX_WAIT so a session parked on a long-poll never ages out mid-wait.

# Sent to every client during the MCP `initialize` handshake (FastMCP `instructions`),
# AND returned by the `usage` tool. So a session discovers what this bus is for the moment
# it connects — no tool call required — and can re-read the guide on demand mid-task.
GUIDE = """\
contract-bus: a durable SQLite message bus for coordinating independent Claude Code sessions
across different repos (e.g. backend + frontend) without a human relaying messages.

- post_message(channel, author, body, to=None): publish to a channel. Address one session
  with to=<handle>, or broadcast with to=None.
- read_messages(channel, since_id=0, as_handle=None): messages newer than since_id, oldest
  first. Pass as_handle=<your handle> to get broadcasts + mail addressed to you. Track the
  highest id you've seen as since_id — the server keeps no read state.
- list_channels(): active channels. list_sessions(): who's connected, their status and
  current_task.

Call usage() to re-read this."""

mcp = FastMCP("contract-bus", instructions=GUIDE)


# --- storage helpers (pure logic, unit-tested against a temp DB) -----------

def _connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init(db: str = DB) -> None:
    """Create tables + indexes, enable WAL, and apply the v2 migration. Idempotent."""
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
        # v2: add the recipient column if an older DB predates it (idempotent migration).
        cols = {row["name"] for row in c.execute("PRAGMA table_info(messages)")}
        if "recipient" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN recipient TEXT")  # NULL = broadcast
        c.execute(
            """CREATE TABLE IF NOT EXISTS sessions(
                handle        TEXT PRIMARY KEY,
                repo          TEXT,
                status        TEXT NOT NULL DEFAULT 'online',
                current_task  TEXT,
                last_seen     TEXT NOT NULL DEFAULT (datetime('now')),
                registered_at TEXT NOT NULL DEFAULT (datetime('now')))"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel, id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_recipient_id ON messages(channel, recipient, id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_recipient_id ON messages(recipient, id)")


def _post_message(db: str, channel: str, author: str, body: str, recipient: str | None = None) -> dict:
    if not channel or not body:
        return {"error": "channel and body are required"}
    with _connect(db) as c:
        cur = c.execute(
            "INSERT INTO messages(channel, author, body, recipient) VALUES (?,?,?,?)",
            (channel, author, body, recipient),
        )
        row = c.execute(
            "SELECT id, created_at FROM messages WHERE id=?", (cur.lastrowid,)
        ).fetchone()
    return {"id": row["id"], "channel": channel, "recipient": recipient, "created_at": row["created_at"]}


def _read_messages(db: str, channel: str | None = None, since_id: int = 0,
                   limit: int = 50, as_handle: str | None = None) -> list[dict]:
    """Messages with id > since_id, oldest first. With `as_handle`: broadcast + mail
    addressed to that handle (channel given), or directed-only across all channels
    (channel omitted; broadcasts excluded — backs the ambient watcher). Without
    `as_handle`: v1 behavior (everything on `channel`)."""
    limit = max(1, min(limit, MAX_LIMIT))
    conds = ["id > ?"]
    params: list = [since_id]
    if channel is not None:
        conds.append("channel = ?")
        params.append(channel)
    if as_handle is not None:
        if channel is not None:
            conds.append("(recipient IS NULL OR recipient = ?)")
        else:
            conds.append("recipient = ?")
        params.append(as_handle)
    sql = "SELECT * FROM messages WHERE " + " AND ".join(conds) + " ORDER BY id LIMIT ?"
    params.append(limit)
    with _connect(db) as c:
        rows = c.execute(sql, params).fetchall()
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


def _register(db: str, handle: str, repo: str | None = None,
              status: str = "online", current_task: str | None = None) -> dict:
    """Upsert a session row (discovery/presence only — never consulted by routing).
    Bumps last_seen; preserves existing repo/current_task when the new value is None."""
    if not handle:
        return {"error": "handle is required"}
    with _connect(db) as c:
        c.execute(
            """INSERT INTO sessions(handle, repo, status, current_task)
               VALUES (?,?,?,?)
               ON CONFLICT(handle) DO UPDATE SET
                   status       = excluded.status,
                   repo         = COALESCE(excluded.repo, sessions.repo),
                   current_task = COALESCE(excluded.current_task, sessions.current_task),
                   last_seen    = datetime('now')""",
            (handle, repo, status, current_task),
        )
    return {"handle": handle, "status": status}


def _touch(db: str, handle: str) -> None:
    """Bump last_seen for a handle (liveness heartbeat from the long-poll). No-op if absent."""
    with _connect(db) as c:
        c.execute("UPDATE sessions SET last_seen=datetime('now') WHERE handle=?", (handle,))


def _list_sessions(db: str, ttl: int = PRESENCE_TTL) -> list[dict]:
    """Connected sessions with effective status (offline if last_seen older than ttl)."""
    with _connect(db) as c:
        rows = c.execute(
            """SELECT handle, repo, status, current_task, last_seen,
                      CAST((julianday('now') - julianday(last_seen)) * 86400 AS INTEGER) AS age
               FROM sessions ORDER BY handle"""
        ).fetchall()
    out = []
    for r in rows:
        status = "offline" if (r["age"] is not None and r["age"] > ttl) else r["status"]
        out.append({"handle": r["handle"], "repo": r["repo"], "status": status,
                    "current_task": r["current_task"], "last_seen": r["last_seen"]})
    return out


async def _wait_for_message(db: str, channel: str | None = None, since_id: int = 0,
                            timeout: float = 600.0, poll: float = 0.5,
                            as_handle: str | None = None) -> list[dict]:
    """Long-poll for messages newer than since_id (inbox filter via as_handle, §4.2).
    Bumps last_seen for as_handle each poll so a parked waiter stays 'online' in
    list_sessions. `await asyncio.sleep` yields the loop so other sessions aren't starved.
    Returns [] after `timeout` seconds."""
    elapsed = 0.0
    while elapsed < timeout:
        rows = _read_messages(db, channel, since_id, as_handle=as_handle)
        if rows:
            return rows
        if as_handle is not None:
            _touch(db, as_handle)
        await asyncio.sleep(poll)
        elapsed += poll
    return []


# --- dev auto-reload (in-process; works WITH KeepAlive) --------------------

def _source_mtime(path: str):
    """mtime of `path`, or None if it can't be stat'd (e.g. transiently missing)."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _source_changed(path: str, baseline) -> bool:
    """True only if `path` currently has an mtime that differs from `baseline`. A missing
    file returns False (don't treat a transient mid-save gap as a change)."""
    m = _source_mtime(path)
    return m is not None and m != baseline


def _watch_source_and_exit(path: str, interval: float = 2.0) -> None:
    """Auto-reload: when this source file changes on disk, exit so the KeepAlive LaunchAgent
    respawns the daemon with the new code. Done IN-PROCESS, not via launchd WatchPaths:
    WatchPaths only *starts* a stopped job, but KeepAlive keeps this daemon always running,
    so WatchPaths never fires (verified empirically). Exit + KeepAlive is reliable and also
    survives atomic-rename saves that would orphan a WatchPaths vnode."""
    baseline = _source_mtime(path)
    while True:
        time.sleep(interval)
        if _source_changed(path, baseline):
            os._exit(0)  # KeepAlive restarts us with fresh code


# --- MCP tool surface (thin wrappers; docstrings tell the model when to call) ----

@mcp.tool()
def usage() -> str:
    """Explain what contract-bus is and how to use it. Call first if unsure."""
    return _usage()


@mcp.tool()
def post_message(channel: str, author: str, body: str, to: str | None = None) -> dict:
    """Publish a message to a channel so other Claude sessions can read it.

    `to` addresses one session by its handle (see list_sessions); omit it (to=None) to
    broadcast to everyone on the channel. `author` is free-form. Returns id + created_at.
    """
    return _post_message(DB, channel, author, body, recipient=to)


@mcp.tool()
def read_messages(channel: str | None = None, since_id: int = 0,
                  limit: int = 50, as_handle: str | None = None) -> list[dict]:
    """Read messages newer than since_id (oldest first). Pass as_handle=<your handle> to get
    broadcasts plus mail addressed to you; omit channel (with as_handle) to read your mail
    across all channels. Track the highest id yourself — no server-side read state.
    """
    return _read_messages(DB, channel, since_id, limit, as_handle=as_handle)


@mcp.tool()
def list_channels() -> list[dict]:
    """List active channels with message counts and last id, to discover ongoing threads."""
    return _list_channels(DB)


@mcp.tool()
def list_sessions() -> list[dict]:
    """List connected sessions: handle, repo, status (online/offline), and current_task.
    Use to discover who is live and what they are working on before addressing them."""
    return _list_sessions(DB)


@mcp.custom_route("/wait", methods=["GET"])
async def wait_route(request: Request) -> JSONResponse:
    """Long-poll for mail. `as_handle` gives the inbox filter; `channel` optional when
    `as_handle` is set (channel-agnostic mail). Returns {"messages":[...]}; 400 if neither
    channel nor as_handle is given. Mounts at root /wait. Bumps last_seen for as_handle."""
    channel = request.query_params.get("channel") or None
    as_handle = request.query_params.get("as_handle") or None
    if not channel and not as_handle:
        return JSONResponse({"error": "channel or as_handle is required"}, status_code=400)
    try:
        since_id = int(request.query_params.get("since_id", 0))
        timeout = min(int(request.query_params.get("timeout", MAX_WAIT)), MAX_WAIT)
    except ValueError:
        return JSONResponse({"error": "since_id and timeout must be integers"}, status_code=400)
    rows = await _wait_for_message(DB, channel, since_id, timeout, as_handle=as_handle)
    return JSONResponse({"messages": rows})


@mcp.custom_route("/register", methods=["POST"])
async def register_route(request: Request) -> JSONResponse:
    """Hook-facing presence upsert/heartbeat (POST, since it mutates). Accepts handle, repo,
    status, current_task via query or form. Not an MCP tool — the model never registers."""
    params = dict(request.query_params)
    try:
        params.update(dict(await request.form()))
    except Exception:
        pass
    handle = params.get("handle")
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)
    res = _register(DB, handle, params.get("repo"),
                    params.get("status", "online"), params.get("current_task"))
    return JSONResponse(res)


if __name__ == "__main__":
    _init()
    # Dev auto-reload: exit when this file changes so the KeepAlive LaunchAgent respawns us
    # with fresh code. Daemon thread → dies with the process; started here (not at import)
    # so the test suite never spawns it.
    threading.Thread(
        target=_watch_source_and_exit, args=(os.path.abspath(__file__),), daemon=True
    ).start()
    mcp.run(transport="http", host=HOST, port=PORT)
