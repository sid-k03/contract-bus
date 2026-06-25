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
from urllib.parse import quote

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

DB = os.environ.get("CONTRACT_BUS_DB", "bus.sqlite3")
HOST = "127.0.0.1"  # localhost only — there is no auth (see spec §9)
PORT = 9100
MAX_LIMIT = 200
MAX_WAIT = 600  # cap (seconds) on a single long-poll; clients re-issue to keep waiting

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

Waiting for a reply (push, not poll-by-hand): after you post and need the other side's
answer, don't busy-loop read_messages.
- If you have nothing else to do, call wait_for_message(channel, since_id): it BLOCKS this
  session until a newer message arrives (or it times out), then returns it. On timeout it
  returns [] — if you still need the reply, call it AGAIN with the same since_id to keep
  waiting (re-queue). [] means "nothing yet," not "no reply is coming."
- If you want to keep working while you wait, call watch_channel(channel, since_id): it does
  NOT block — it returns a `curl` command. Run that command in the BACKGROUND (a backgrounded
  shell command). It stays parked server-side and exits the moment a newer message lands,
  which wakes you holding the reply. Then advance since_id to the newest id and call
  watch_channel again to keep listening.

Etiquette: read before you start a shared-feature task, post decisions as you make them,
and re-read (or wait/watch) with your last seen id before assuming the other side hasn't
replied."""

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


async def _wait_for_message(
    db: str, channel: str, since_id: int = 0, timeout: float = 600.0, poll: float = 0.5
) -> list[dict]:
    """Long-poll: block until a message with id > since_id exists on channel, then return
    those rows. Return [] if `timeout` seconds elapse first. `poll` is the re-check interval;
    `await asyncio.sleep` yields the event loop so other sessions' posts/reads aren't starved.
    Stays stateless about consumers — the cursor is still the caller's `since_id`."""
    elapsed = 0.0
    while elapsed < timeout:
        rows = _read_messages(db, channel, since_id)
        if rows:
            return rows
        await asyncio.sleep(poll)
        elapsed += poll
    return []


def _watch_command(channel: str, since_id: int = 0, timeout: int = 600) -> dict:
    """Build the backgroundable curl recipe for the /wait route. Pure string assembly — the
    agent runs the returned command as a backgrounded shell command, so the WAIT happens in
    that subprocess (0 model tokens while parked), not in a blocking tool call."""
    url = (
        f"http://{HOST}:{PORT}/wait"
        f"?channel={quote(channel)}&since_id={since_id}&timeout={timeout}"
    )
    cmd = f'curl -s --max-time {timeout + 10} "{url}"'
    return {
        "run_in_background": cmd,
        "channel": channel,
        "since_id": since_id,
        "note": (
            "Run the command in `run_in_background` as a BACKGROUNDED shell command, then keep "
            "working. It prints {\"messages\":[...]} and exits when a newer message arrives. "
            "Advance since_id to the newest id and call watch_channel again to keep listening."
        ),
    }


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


@mcp.tool()
async def wait_for_message(channel: str, since_id: int = 0, timeout: int = 60) -> list[dict]:
    """Block until a message newer than since_id arrives on a channel, then return it.

    Use when you've posted and are waiting on the other session's reply and have NOTHING
    else to do — this freezes the session until a message lands or `timeout` seconds pass
    (then returns []). To wait while still working, use watch_channel instead. Track the
    highest id you've seen and pass it as since_id; the server stores no read state.

    RE-QUEUE ON TIMEOUT: a [] result means "nothing arrived yet," not "no reply is coming."
    If you're still waiting, call wait_for_message again with the SAME since_id to keep
    blocking. Repeat until you get a message (or decide to stop waiting).
    """
    return await _wait_for_message(DB, channel, since_id, min(timeout, MAX_WAIT))


@mcp.tool()
def watch_channel(channel: str, since_id: int = 0) -> dict:
    """Listen for the next message on a channel WITHOUT blocking — returns a curl command
    to run in the background.

    Use when you want to be pinged with the other session's reply but keep working
    meanwhile. Returns a dict whose `run_in_background` value is a shell command: run it as
    a BACKGROUNDED command. It parks server-side and exits when a message newer than
    since_id arrives, waking you with the reply as JSON. Then advance since_id to the newest
    id and call watch_channel again to keep listening. Track the cursor yourself — the
    server stores no read state.
    """
    return _watch_command(channel, since_id, MAX_WAIT)


@mcp.custom_route("/wait", methods=["GET"])
async def wait_route(request: Request) -> JSONResponse:
    """Plain-HTTP long-poll endpoint (curl-able, so it can be backgrounded). Blocks until a
    message with id > since_id lands on channel, then returns {"messages": [...]}; returns
    {"messages": []} on timeout. Mounts at root /wait (not /mcp/wait)."""
    channel = request.query_params.get("channel", "")
    if not channel:
        return JSONResponse({"error": "channel is required"}, status_code=400)
    try:
        since_id = int(request.query_params.get("since_id", 0))
        timeout = min(int(request.query_params.get("timeout", MAX_WAIT)), MAX_WAIT)
    except ValueError:
        return JSONResponse({"error": "since_id and timeout must be integers"}, status_code=400)
    rows = await _wait_for_message(DB, channel, since_id, timeout)
    return JSONResponse({"messages": rows})


if __name__ == "__main__":
    _init()
    # Dev auto-reload: exit when this file changes so the KeepAlive LaunchAgent respawns us
    # with fresh code. Daemon thread → dies with the process; started here (not at import)
    # so the test suite never spawns it.
    threading.Thread(
        target=_watch_source_and_exit, args=(os.path.abspath(__file__),), daemon=True
    ).start()
    mcp.run(transport="http", host=HOST, port=PORT)
