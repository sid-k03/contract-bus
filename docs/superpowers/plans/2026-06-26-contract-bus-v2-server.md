# contract-bus v2 — Server Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the contract-bus server with per-session message addressing (`recipient`), a discovery-only presence registry (`sessions` + `current_task`), recipient-aware long-poll, and the lean 5-tool surface + `/register` route — all backward compatible with the v1 SQLite file.

**Architecture:** Keep the single-file `bus_server.py` (helpers + thin `@mcp.tool`/route wrappers; tests import the pure `_helpers`). Addressing is a WHERE-clause filter so routing stays stateless about consumers; presence is discovery-only and never consulted by routing. This is Plan 1 of 2 — the hook pack + skills + plugin packaging (Plan 2) build on the `/wait` and `/register` routes produced here.

**Tech Stack:** Python 3.11+, FastMCP 3.4.2 (HTTP transport), stdlib `sqlite3` (no ORM), Starlette `Request`/`JSONResponse` for routes, `pytest` (no pytest-asyncio — async helpers tested via `asyncio.run`).

**Spec:** `docs/superpowers/specs/2026-06-26-contract-bus-multitenancy-hooks-design.md` (esp. §3, §4, §12).

## Global Constraints

- Bind `127.0.0.1` only; never `0.0.0.0`. No auth.
- Always use bound SQL parameters; never string-format values into SQL.
- Validate minimally: non-empty `channel` and `body`; clamp `limit` to `MAX_LIMIT` (200). Return an error dict, never raise raw.
- Append-only: no edit/delete, no content/schema validation.
- Constants: `HOST="127.0.0.1"`, `PORT=9100`, `MAX_LIMIT=200`, `MAX_WAIT=600`, `PRESENCE_TTL=900` (deliberately > `MAX_WAIT` so a parked watcher never ages out).
- Keep it one file (`bus_server.py`). Logic lives in `_helpers`; `@mcp.tool`-decorated objects are not callable in fastmcp 3.x, so tests import helpers, not tools.
- Run tests: `.venv/bin/pytest`.
- The model-facing tool surface is exactly 5: `usage`, `post_message`, `read_messages`, `list_channels`, `list_sessions`. Anything only hooks call is an HTTP route, not a tool.

---

### Task 1: Schema migration — `recipient` column, `sessions` table, indexes

**Files:**
- Modify: `bus_server.py` (the `PRESENCE_TTL` constant near the other constants; the `_init` function)
- Test: `test_bus_server.py`

**Interfaces:**
- Produces: `_init(db: str)` now (a) adds a nullable `recipient` column to `messages` if absent, (b) creates the `sessions` table, (c) creates the two new indexes — all idempotent and safe on a pre-v2 DB.

- [ ] **Step 1: Write the failing tests**

Add to `test_bus_server.py`:

```python
def _columns(db, table):
    import sqlite3
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    c.close()
    return cols


def test_init_adds_recipient_column_to_pre_v2_db(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite3")
    # simulate a v1 DB: messages table WITHOUT a recipient column
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
        author TEXT NOT NULL, body TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    c.commit(); c.close()
    assert "recipient" not in _columns(path, "messages")
    bus._init(path)
    assert "recipient" in _columns(path, "messages")


def test_init_is_idempotent(db):
    # running _init again on an already-migrated DB must not raise
    bus._init(db)
    bus._init(db)
    assert "recipient" in _columns(db, "messages")


def test_init_creates_sessions_table(db):
    cols = _columns(db, "sessions")
    assert {"handle", "repo", "status", "current_task", "last_seen", "registered_at"} <= cols
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest test_bus_server.py::test_init_adds_recipient_column_to_pre_v2_db test_bus_server.py::test_init_creates_sessions_table -v`
Expected: FAIL — `recipient` column missing / no `sessions` table.

- [ ] **Step 3: Add the `PRESENCE_TTL` constant**

In `bus_server.py`, next to the other constants (after `MAX_WAIT = 600`):

```python
PRESENCE_TTL = 900  # seconds; a session whose last_seen is older reports offline.
                    # > MAX_WAIT so a session parked on a long-poll never ages out mid-wait.
```

- [ ] **Step 4: Rewrite `_init` to migrate**

Replace the body of `_init` in `bus_server.py` with:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest test_bus_server.py -v`
Expected: PASS (the three new tests + all existing v1 tests still green).

- [ ] **Step 6: Commit**

```bash
git add bus_server.py test_bus_server.py
git commit -m "feat(server): v2 migration — recipient column, sessions table, indexes"
```

---

### Task 2: Addressing — `recipient` on post, inbox filter on read

**Files:**
- Modify: `bus_server.py` (`_post_message`, `_read_messages`)
- Test: `test_bus_server.py`

**Interfaces:**
- Consumes: `_init` (Task 1).
- Produces:
  - `_post_message(db, channel, author, body, recipient=None) -> dict` — return shape gains `"recipient"`.
  - `_read_messages(db, channel=None, since_id=0, limit=50, as_handle=None) -> list[dict]` — when `as_handle` set: with `channel`, returns broadcast+own-directed on that channel; without `channel`, returns own-directed on ANY channel (broadcasts excluded).

- [ ] **Step 1: Write the failing tests**

```python
# --- addressing -----------------------------------------------------------

def test_directed_message_only_visible_to_recipient(db):
    bus._post_message(db, "feat", "lead", "for backend", recipient="backend-1")
    assert bus._read_messages(db, "feat", as_handle="backend-1")[0]["body"] == "for backend"
    assert bus._read_messages(db, "feat", as_handle="frontend-1") == []


def test_broadcast_visible_to_everyone(db):
    bus._post_message(db, "feat", "lead", "all hands")           # recipient=None
    assert bus._read_messages(db, "feat", as_handle="backend-1")[0]["body"] == "all hands"
    assert bus._read_messages(db, "feat", as_handle="frontend-1")[0]["body"] == "all hands"


def test_inbox_returns_broadcast_plus_own_directed(db):
    bus._post_message(db, "feat", "lead", "all hands")            # broadcast
    bus._post_message(db, "feat", "lead", "for backend", recipient="backend-1")
    bus._post_message(db, "feat", "lead", "for frontend", recipient="frontend-1")
    bodies = [m["body"] for m in bus._read_messages(db, "feat", as_handle="backend-1")]
    assert bodies == ["all hands", "for backend"]


def test_channel_agnostic_returns_my_directed_any_channel_excluding_broadcast(db):
    bus._post_message(db, "chan-a", "lead", "a-direct", recipient="backend-1")
    bus._post_message(db, "chan-b", "lead", "b-direct", recipient="backend-1")
    bus._post_message(db, "chan-a", "lead", "a-broadcast")        # broadcast, must be excluded
    rows = bus._read_messages(db, channel=None, as_handle="backend-1")
    assert [m["body"] for m in rows] == ["a-direct", "b-direct"]


def test_post_message_returns_recipient(db):
    res = bus._post_message(db, "feat", "lead", "hi", recipient="backend-1")
    assert res["recipient"] == "backend-1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest test_bus_server.py -k "directed or broadcast or inbox or channel_agnostic or returns_recipient" -v`
Expected: FAIL — `_post_message` rejects the `recipient` kwarg / `_read_messages` rejects `as_handle`.

- [ ] **Step 3: Update `_post_message`**

Replace `_post_message` in `bus_server.py`:

```python
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
```

- [ ] **Step 4: Update `_read_messages`**

Replace `_read_messages` in `bus_server.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest test_bus_server.py -v`
Expected: PASS (new addressing tests + all existing v1 read/post tests — they call `_read_messages(db, "t", since_id=0)` and `_post_message(db, "t", "a", "b")`, still valid).

- [ ] **Step 6: Commit**

```bash
git add bus_server.py test_bus_server.py
git commit -m "feat(server): recipient addressing on post + inbox filter on read"
```

---

### Task 3: Presence registry — `_register`, `_touch`, `_list_sessions`

**Files:**
- Modify: `bus_server.py` (add three helpers near `_list_channels`)
- Test: `test_bus_server.py`

**Interfaces:**
- Consumes: `_init` (sessions table).
- Produces:
  - `_register(db, handle, repo=None, status="online", current_task=None) -> dict` — upsert; bumps `last_seen`; preserves existing `repo`/`current_task` when the new value is `None`.
  - `_touch(db, handle) -> None` — bump `last_seen` only (used by the long-poll, Task 4).
  - `_list_sessions(db, ttl=PRESENCE_TTL) -> list[dict]` — `[{handle, repo, status, current_task, last_seen}]`; effective `status` is `offline` when `last_seen` age > `ttl`.

- [ ] **Step 1: Write the failing tests**

```python
# --- presence -------------------------------------------------------------

def test_register_inserts_online_session(db):
    bus._register(db, "backend-1", repo="backend", current_task="checkout API")
    s = bus._list_sessions(db)
    assert s == [{"handle": "backend-1", "repo": "backend", "status": "online",
                  "current_task": "checkout API", "last_seen": s[0]["last_seen"]}]


def test_register_upsert_updates_task_preserves_repo(db):
    bus._register(db, "backend-1", repo="backend", current_task="checkout")
    bus._register(db, "backend-1", current_task="refunds")     # repo omitted → preserved
    s = bus._list_sessions(db)[0]
    assert s["repo"] == "backend"
    assert s["current_task"] == "refunds"


def test_register_offline_status(db):
    bus._register(db, "backend-1", repo="backend")
    bus._register(db, "backend-1", status="offline")
    assert bus._list_sessions(db)[0]["status"] == "offline"


def test_list_sessions_ages_out_stale_session(db):
    bus._register(db, "ghost-1", repo="x")
    # force last_seen far into the past
    import sqlite3
    c = sqlite3.connect(db)
    c.execute("UPDATE sessions SET last_seen = datetime('now','-1 hour') WHERE handle=?", ("ghost-1",))
    c.commit(); c.close()
    assert bus._list_sessions(db, ttl=900)[0]["status"] == "offline"


def test_touch_keeps_session_fresh(db):
    bus._register(db, "backend-1", repo="backend")
    import sqlite3
    c = sqlite3.connect(db)
    c.execute("UPDATE sessions SET last_seen = datetime('now','-1 hour') WHERE handle=?", ("backend-1",))
    c.commit(); c.close()
    assert bus._list_sessions(db, ttl=900)[0]["status"] == "offline"   # stale before touch
    bus._touch(db, "backend-1")
    assert bus._list_sessions(db, ttl=900)[0]["status"] == "online"    # fresh after touch
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest test_bus_server.py -k "register or list_sessions or touch" -v`
Expected: FAIL — `_register`/`_touch`/`_list_sessions` not defined.

- [ ] **Step 3: Implement the three helpers**

Add to `bus_server.py` (after `_list_channels`):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest test_bus_server.py -k "register or list_sessions or touch" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bus_server.py test_bus_server.py
git commit -m "feat(server): presence registry — register, touch, list_sessions with TTL"
```

---

### Task 4: Recipient-aware long-poll + liveness bump

**Files:**
- Modify: `bus_server.py` (`_wait_for_message`)
- Test: `test_bus_server.py`

**Interfaces:**
- Consumes: `_read_messages`, `_touch`.
- Produces: `_wait_for_message(db, channel=None, since_id=0, timeout=600.0, poll=0.5, as_handle=None) -> list[dict]` — long-poll using the inbox filter; bumps `last_seen` for `as_handle` each poll so a parked waiter stays online; returns `[]` on timeout.

- [ ] **Step 1: Write the failing tests**

```python
def test_wait_wakes_on_directed_message_any_channel(db):
    bus._register(db, "backend-1", repo="backend")

    async def scenario():
        async def insert_later():
            await asyncio.sleep(0.03)
            bus._post_message(db, "random-chan", "lead", "ping", recipient="backend-1")
        task = asyncio.create_task(insert_later())
        rows = await bus._wait_for_message(db, channel=None, since_id=0,
                                           timeout=2.0, poll=0.01, as_handle="backend-1")
        await task
        return rows

    rows = asyncio.run(scenario())
    assert len(rows) == 1 and rows[0]["body"] == "ping"


def test_wait_ignores_broadcast_when_channel_agnostic(db):
    bus._register(db, "backend-1", repo="backend")
    bus._post_message(db, "feat", "lead", "broadcast only")   # no recipient
    rows = asyncio.run(
        bus._wait_for_message(db, channel=None, since_id=0,
                              timeout=0.05, poll=0.01, as_handle="backend-1")
    )
    assert rows == []   # channel-agnostic excludes broadcasts


def test_wait_bumps_last_seen(db):
    bus._register(db, "backend-1", repo="backend")
    import sqlite3
    c = sqlite3.connect(db)
    c.execute("UPDATE sessions SET last_seen = datetime('now','-1 hour') WHERE handle=?", ("backend-1",))
    c.commit(); c.close()
    asyncio.run(bus._wait_for_message(db, channel=None, since_id=0,
                                      timeout=0.05, poll=0.01, as_handle="backend-1"))
    assert bus._list_sessions(db, ttl=900)[0]["status"] == "online"   # bumped during the wait
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest test_bus_server.py -k "wait_wakes or wait_ignores_broadcast or wait_bumps" -v`
Expected: FAIL — `_wait_for_message` doesn't accept `as_handle`.

- [ ] **Step 3: Update `_wait_for_message`**

Replace `_wait_for_message` in `bus_server.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest test_bus_server.py -v`
Expected: PASS (new long-poll tests + the existing `test_wait_for_message_*` tests, which call `_wait_for_message(db, "t", since_id=…)` — `channel="t"` positional still valid).

- [ ] **Step 5: Commit**

```bash
git add bus_server.py test_bus_server.py
git commit -m "feat(server): recipient-aware long-poll with liveness bump"
```

---

### Task 5: Lean tool surface + slim GUIDE (drop wait/watch tools)

**Files:**
- Modify: `bus_server.py` (`GUIDE`; tool wrappers; remove `_watch_command`, `wait_for_message` tool, `watch_channel` tool)
- Test: `test_bus_server.py` (update usage-terms test; remove `_watch_command` tests)

**Interfaces:**
- Produces the final 5-tool surface: `usage()`, `post_message(channel, author, body, to=None)`, `read_messages(channel=None, since_id=0, limit=50, as_handle=None)`, `list_channels()`, `list_sessions()`. `GUIDE` ≤ 150 tokens, single-sourced for `instructions=` and `usage()`.

- [ ] **Step 1: Update the tests (red)**

In `test_bus_server.py`: (a) replace the term list in `test_usage_guide_explains_purpose_and_workflow`, and (b) DELETE the two now-obsolete tests `test_watch_command_builds_background_curl` and `test_watch_command_urlencodes_channel`.

```python
def test_usage_guide_explains_purpose_and_workflow():
    g = bus._usage()
    low = g.lower()
    for term in ("contract-bus", "post_message", "read_messages",
                 "list_channels", "list_sessions", "since_id", "to=", "as_handle"):
        assert term in low, f"usage guide missing {term!r}"


def test_list_sessions_tool_helper_present():
    # the model-facing surface exposes list_sessions
    assert hasattr(bus, "list_sessions")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest test_bus_server.py -k "usage_guide or list_sessions_tool" -v`
Expected: FAIL — GUIDE missing `list_sessions`/`as_handle`; (and the file still defines `_watch_command` tests if not yet deleted — delete them in Step 1).

- [ ] **Step 3: Replace `GUIDE`**

Replace the `GUIDE = """..."""` block in `bus_server.py` with this slim version (≤150 tokens, single source):

```python
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
```

- [ ] **Step 4: Replace the tool wrappers and delete `_watch_command`**

In `bus_server.py`: delete the `_watch_command` helper, the `wait_for_message` tool, and the `watch_channel` tool. Ensure the tool section reads exactly:

```python
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
```

(The `import` of `quote` becomes unused once `_watch_command` is gone — remove `from urllib.parse import quote`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest test_bus_server.py -v`
Expected: PASS. (`test_server_instructions_set_for_handshake_discovery` still passes — `GUIDE` still contains `post_message`.)

- [ ] **Step 6: Commit**

```bash
git add bus_server.py test_bus_server.py
git commit -m "feat(server): lean 5-tool surface + slim GUIDE; drop wait/watch tools"
```

---

### Task 6: HTTP routes — `/wait` (updated) + `/register`

**Files:**
- Modify: `bus_server.py` (`wait_route`; add `register_route`)
- Test: `test_bus_server.py` (helper-level), plus a build-time e2e verification step

**Interfaces:**
- Consumes: `_wait_for_message`, `_register`.
- Produces:
  - `GET /wait?as_handle=…[&channel=…]&since_id=…&timeout=…` → `{"messages":[…]}`; `400` if neither `channel` nor `as_handle`; non-int `since_id`/`timeout` → `400`.
  - `POST /register` (query or form: `handle`, `repo?`, `status?`, `current_task?`) → `{"handle":…, "status":…}`; `400` if `handle` missing.

- [ ] **Step 1: Write the failing tests (route argument-handling at helper level)**

These assert the parsing/branching the routes depend on; the routes themselves are verified e2e in Step 5.

```python
def test_register_route_helper_rejects_missing_handle(db):
    assert "error" in bus._register(db, "")


def test_read_requires_channel_or_as_handle_contract(db):
    # the /wait route returns 400 when both are absent; the helper convention it relies on:
    # channel=None and as_handle=None means "no inbox scope" — the route must reject it.
    # (helper still returns all rows; the ROUTE enforces the 400 — asserted in e2e Step 5.)
    bus._post_message(db, "feat", "lead", "x")
    assert bus._read_messages(db, channel=None, as_handle=None)  # helper itself is permissive
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest test_bus_server.py -k "register_route_helper or requires_channel_or" -v`
Expected: the first FAILS until Task 3's `_register` is present (it is) — so this mainly locks the contract; if green already, proceed. (No route unit test — fastmcp routes need a live server, covered in Step 5.)

- [ ] **Step 3: Update `wait_route`**

Replace `wait_route` in `bus_server.py`:

```python
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
```

- [ ] **Step 4: Add `register_route`**

Add to `bus_server.py` (after `wait_route`):

```python
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
```

- [ ] **Step 5: Build-time e2e verification (routes need a live server)**

Boot the daemon on an alt port + temp DB (so the live LaunchAgent on 9100 is undisturbed), then exercise the routes:

```bash
CONTRACT_BUS_DB=/tmp/bus_e2e.sqlite3 .venv/bin/python -c "
import bus_server as b, threading, time, urllib.request, urllib.parse, json
b.PORT=9112; b._init('/tmp/bus_e2e.sqlite3'); b.DB='/tmp/bus_e2e.sqlite3'
threading.Thread(target=lambda: b.mcp.run(transport='http', host='127.0.0.1', port=9112), daemon=True).start()
time.sleep(2)
# register (POST)
data=urllib.parse.urlencode({'handle':'backend-1','repo':'backend','current_task':'checkout'}).encode()
print('register:', urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:9112/register', data=data)).read())
# /wait with neither → 400
try:
    urllib.request.urlopen('http://127.0.0.1:9112/wait')
except urllib.error.HTTPError as e:
    print('wait-no-args status:', e.code)   # expect 400
# post a directed message via the tool helper, then /wait?as_handle wakes
b._post_message('/tmp/bus_e2e.sqlite3','feat','lead','hi backend',recipient='backend-1')
print('wait:', urllib.request.urlopen('http://127.0.0.1:9112/wait?as_handle=backend-1&timeout=2').read())
"
rm -f /tmp/bus_e2e.sqlite3*
```
Expected: `register:` prints `{"handle": "backend-1", "status": "online"}`; `wait-no-args status: 400`; `wait:` prints `{"messages": [...]}` containing `hi backend`.

- [ ] **Step 6: Run the full unit suite once more**

Run: `.venv/bin/pytest -v`
Expected: PASS (all tasks green).

- [ ] **Step 7: Commit**

```bash
git add bus_server.py test_bus_server.py
git commit -m "feat(server): /wait recipient filter + POST /register route"
```

---

## Self-Review

- **Spec coverage:** §4.1 migration → Task 1. §4.2 read filter (incl. channel-agnostic) → Task 2. §4.5 presence + TTL + current_task → Task 3. §4.4 `/wait` last_seen bump + §6 recipient watcher backing → Task 4. §4.3 lean 5-tool surface + §12 GUIDE cap → Task 5. §4.4 routes (`/wait`, POST `/register`) → Task 6. (Hook pack, skills, plugin packaging, idle-wake canary = Plan 2.)
- **Type consistency:** `_read_messages(channel=None, since_id, limit, as_handle=None)`, `_wait_for_message(channel=None, since_id, timeout, poll, as_handle=None)`, `_register(handle, repo, status, current_task)`, `_list_sessions(ttl)` — names/order identical across tasks and tool wrappers (`to=`→`recipient`, `as_handle` passthrough).
- **No placeholders:** every step has full code or a runnable command with expected output.
- **Backward compatibility:** existing v1 tests call helpers with positional `channel`/`since_id`, which the new optional-kwarg signatures preserve; the migration adds the column without touching existing rows.

## Next

Plan 2 (`2026-06-26-contract-bus-v2-plugin.md`) covers `bus_cli.py` (hook brain: activation gate, handle derivation, single-instance watcher, cursor, reaper, join/conclude, daemon auto-spawn), the 5 hook entries, the 3 skills, the `.claude-plugin`/`.mcp.json`/`hooks.json` manifests, the idle-wake canary test, and docs — all building on the `/wait` and `/register` routes delivered here.
