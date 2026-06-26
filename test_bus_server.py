"""Tests for the contract-bus core logic (spec §10 acceptance criteria + edge cases).

These exercise the pure helper functions (`_init`, `_post_message`, `_read_messages`,
`_list_channels`) against a temp SQLite file, so no HTTP server needs to run.
"""
import asyncio

import pytest

import bus_server as bus


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "bus.sqlite3")
    bus._init(path)
    return path


# --- post_message ---------------------------------------------------------

def test_post_message_returns_id_one_for_first_row(db):
    res = bus._post_message(db, "t", "backend", "hello")
    assert res["id"] == 1
    assert res["channel"] == "t"
    assert res["created_at"]  # non-empty timestamp


def test_post_message_ids_are_monotonic(db):
    assert bus._post_message(db, "t", "backend", "a")["id"] == 1
    assert bus._post_message(db, "t", "frontend", "b")["id"] == 2


def test_post_message_rejects_empty_channel(db):
    res = bus._post_message(db, "", "backend", "hello")
    assert "error" in res


def test_post_message_rejects_empty_body(db):
    res = bus._post_message(db, "t", "backend", "")
    assert "error" in res


# --- read_messages --------------------------------------------------------

def test_read_messages_returns_message(db):
    bus._post_message(db, "t", "backend", "hello")
    rows = bus._read_messages(db, "t", since_id=0)
    assert len(rows) == 1
    assert rows[0]["author"] == "backend"
    assert rows[0]["body"] == "hello"


def test_read_messages_cursor_excludes_seen(db):
    bus._post_message(db, "t", "backend", "hello")
    assert bus._read_messages(db, "t", since_id=1) == []


def test_read_messages_returns_only_newer_than_cursor(db):
    bus._post_message(db, "t", "backend", "hello")
    bus._post_message(db, "t", "frontend", "hi back")
    rows = bus._read_messages(db, "t", since_id=1)
    assert len(rows) == 1
    assert rows[0]["id"] == 2
    assert rows[0]["body"] == "hi back"


def test_read_messages_oldest_first(db):
    bus._post_message(db, "t", "a", "first")
    bus._post_message(db, "t", "b", "second")
    rows = bus._read_messages(db, "t", since_id=0)
    assert [r["body"] for r in rows] == ["first", "second"]


def test_read_messages_isolated_by_channel(db):
    bus._post_message(db, "chan-a", "x", "a")
    bus._post_message(db, "chan-b", "y", "b")
    rows = bus._read_messages(db, "chan-a", since_id=0)
    assert len(rows) == 1
    assert rows[0]["body"] == "a"


def test_read_messages_clamps_limit_to_max(db):
    for i in range(250):
        bus._post_message(db, "t", "a", f"m{i}")
    rows = bus._read_messages(db, "t", since_id=0, limit=10_000)
    assert len(rows) == 200  # clamped to MAX_LIMIT


def test_read_messages_honors_limit(db):
    for i in range(5):
        bus._post_message(db, "t", "a", f"m{i}")
    rows = bus._read_messages(db, "t", since_id=0, limit=2)
    assert len(rows) == 2


# --- list_channels --------------------------------------------------------

def test_list_channels_reports_count_and_last_id(db):
    bus._post_message(db, "t", "backend", "a")
    bus._post_message(db, "t", "frontend", "b")
    chans = bus._list_channels(db)
    assert chans == [{"channel": "t", "message_count": 2, "last_id": 2}]


def test_list_channels_empty_when_no_messages(db):
    assert bus._list_channels(db) == []


# --- self-documentation ---------------------------------------------------

def test_usage_guide_explains_purpose_and_workflow():
    g = bus._usage()
    assert isinstance(g, str)
    low = g.lower()
    for term in ("contract-bus", "post_message", "read_messages",
                 "list_channels", "list_sessions", "since_id", "to=", "as_handle"):
        assert term in low, f"usage guide missing {term!r}"


def test_list_sessions_tool_helper_present():
    # the model-facing surface exposes list_sessions
    assert hasattr(bus, "list_sessions")


def test_wait_for_message_tool_helper_present():
    # the model-facing surface re-exposes wait_for_message (the documented idle-wake floor)
    assert hasattr(bus, "wait_for_message")


def test_usage_guide_mentions_wait_for_message():
    low = bus._usage().lower()
    assert "wait_for_message" in low


def test_server_instructions_set_for_handshake_discovery():
    # FastMCP sends `instructions` during the initialize handshake, so a connecting
    # session discovers the bus without calling any tool.
    assert bus.mcp.instructions
    assert "post_message" in bus.mcp.instructions


# --- wait_for_message (blocking long-poll) --------------------------------

def test_wait_for_message_returns_empty_on_timeout(db):
    # nothing posted → returns [] once the (short) timeout elapses
    rows = asyncio.run(
        bus._wait_for_message(db, "t", since_id=0, timeout=0.05, poll=0.01)
    )
    assert rows == []


def test_wait_for_message_returns_message_posted_during_wait(db):
    # a message posted WHILE the call is blocked must wake it and be returned
    async def scenario():
        async def insert_later():
            await asyncio.sleep(0.03)
            bus._post_message(db, "t", "backend", "late arrival")

        task = asyncio.create_task(insert_later())
        rows = await bus._wait_for_message(db, "t", since_id=0, timeout=2.0, poll=0.01)
        await task
        return rows

    rows = asyncio.run(scenario())
    assert len(rows) == 1
    assert rows[0]["body"] == "late arrival"


def test_wait_for_message_ignores_messages_at_or_below_since_id(db):
    # an existing message at id=1 must NOT satisfy a waiter watching since_id=1
    bus._post_message(db, "t", "backend", "old")
    rows = asyncio.run(
        bus._wait_for_message(db, "t", since_id=1, timeout=0.05, poll=0.01)
    )
    assert rows == []


# --- source auto-reload watcher -------------------------------------------

def test_source_changed_false_when_unmodified(tmp_path):
    f = tmp_path / "src.py"
    f.write_text("v1")
    base = bus._source_mtime(str(f))
    assert bus._source_changed(str(f), base) is False


def test_source_changed_true_after_modification(tmp_path):
    import os as _os
    f = tmp_path / "src.py"
    f.write_text("v1")
    base = bus._source_mtime(str(f))
    _os.utime(str(f), (base + 5, base + 5))   # deterministic mtime bump
    assert bus._source_changed(str(f), base) is True


def test_source_changed_false_when_path_missing(tmp_path):
    # a transient missing file (e.g. mid atomic-save) must NOT count as a change → no exit
    assert bus._source_changed(str(tmp_path / "gone.py"), 123.0) is False


# --- persistence ----------------------------------------------------------

def test_messages_persist_across_reconnect(db):
    bus._post_message(db, "t", "backend", "hello")
    bus._post_message(db, "t", "frontend", "hi back")
    # simulate daemon restart: nothing cached, reopen the same file
    rows = bus._read_messages(db, "t", since_id=0)
    assert len(rows) == 2


# --- v2 migration ---------------------------------------------------------

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


# --- recipient-aware long-poll --------------------------------------------

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


# --- route contracts (routes themselves verified e2e) ---------------------

def test_register_route_helper_rejects_missing_handle(db):
    assert "error" in bus._register(db, "")


def test_read_requires_channel_or_as_handle_contract(db):
    # the /wait route returns 400 when both are absent; the helper itself is permissive
    # (channel=None, as_handle=None returns all rows; the ROUTE enforces the 400, see e2e).
    bus._post_message(db, "feat", "lead", "x")
    assert bus._read_messages(db, channel=None, as_handle=None)  # helper itself is permissive
