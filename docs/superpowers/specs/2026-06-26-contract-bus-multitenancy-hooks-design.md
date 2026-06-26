# Design: contract-bus v2 — addressing, opt-in activation, ambient watcher

**Date:** 2026-06-26
**Status:** Approved design (post adversarial review), pre-implementation
**Extends** the existing `contract-bus` server and `mcp-contract-bus-spec.md`. The v1 model
(stateless pairwise log, cursor reads, push via long-poll) stays intact; v2 adds addressing,
a discovery-only presence registry, and a Claude Code integration layer that is **dormant
until a human opts a session in**.

> This document was rewritten after an adversarial review. The review cut several earlier
> additions (a separate "announce" board channel, a synchronous UserPromptSubmit mail-fetch,
> hook-injected message bodies with auto-continue, a 4-skill layer) and tightened the rest.
> The cut features and why are recorded in §13 so they are not re-added as "missing."

---

## 1. Goal

Turn contract-bus from a **2-session shared log** into an **N-session addressed mailbox**
for independent, long-lived, cross-repo Claude Code sessions, with three properties:

- **Addressed:** messages broadcast to a channel OR direct to one session's handle.
- **Opt-in, ambient:** hooks are installed once globally but **dormant by default** — a
  session joins the bus only when a human says "this task needs the bus." Unrelated sessions
  pay ~nothing.
- **Idle-wake:** an opted-in session is woken by mail addressed to it even while idle, via a
  single background watcher (no manual polling).

### Non-goals (deliberately not built)
- **Multi-machine / auth / TLS.** `127.0.0.1` only. If ever needed: configurable HOST + a
  VPN (Tailscale/WireGuard), never hand-rolled auth.
- **Delivery receipts / ack table.** The durable cursor already gives at-least-once
  delivery; server-side acks would only add visibility, at the cost of stateless routing.
- **A separate activity-board channel.** Folded into a `current_task` column on the
  presence row (§4.5) — see §13.
- **Edit / delete / content validation.** Unchanged v1 non-goals.

---

## 2. Architecture

```
  session A (backend)  ─┐   opt-in only          ┌─ hooks (global, dormant until active)
  session B (frontend) ─┼─ HTTP (MCP + /wait) ─▶ │  contract-bus daemon ─▶ bus.sqlite3
  session C (mobile)   ─┘                        └─ tools: usage/post/read/list_channels/
                                                     list_sessions ; routes: /wait /register
```

Two layers, each testable alone:
1. **Server** (`bus_server.py`, stays ~one file): a nullable `recipient` column, a
   discovery-only `sessions` table (with `current_task`), addressing on the existing tools,
   and two hook-facing HTTP routes. Routing stays a WHERE clause — **stateless about
   consumers** holds; presence is discovery-only and never consulted by routing.
2. **Hook pack** (`hook-pack/`, new): scripts + skills + a global `settings.json` snippet.
   Installed once; inert until a session activates.

### Why not the rejected alternatives
- **Server-side mailboxes with delivery state** — breaks stateless routing, most code. The
  optional ack layer (a non-goal) is the only piece worth ever revisiting.
- **A second "announce" channel for activity** — reinvents `list_sessions`; replaced by one
  column (§13).

---

## 3. Identity / handle model

A session's **handle** is its routing address, presence key, and state-dir namespace.

- **Derivation:** `slug(repo-dir) + "-" + session_id[:8]`.
  Example: repo `Data Bus MCP`, session `a3f29c1b…` → **`data-bus-mcp-a3f29c1b`**.
- **Why `session_id`:** stable for a session's life, survives `--resume`/compaction, unique
  per launch. `[:8]` = 32 bits — collision-safe for realistic fleets (not the reckless 4
  hex chars of an earlier draft; no "guaranteed distinct" claim is made).
- **Uniqueness for parallel sessions:** N sessions in the *same* repo share the slug but
  differ in `session_id`, so they get distinct handles → distinct state dirs → no collision.
- **Discovery over memorization:** peers find the current live handle via `list_sessions()`;
  the registry is the source of truth for "who is `backend` right now."

`slug()` = lowercase, non-alphanumeric → `-`, collapse repeats, trim.

---

## 4. Server changes

### 4.1 Schema + migration
```sql
ALTER TABLE messages ADD COLUMN recipient TEXT;     -- NULL = broadcast; else a handle
```
Idempotent migration in `_init()`: read `PRAGMA table_info(messages)`, add the column only
if absent; existing rows become broadcast (`NULL`). Backward compatible. Indexes:
```sql
CREATE INDEX IF NOT EXISTS idx_messages_channel_recipient_id ON messages(channel, recipient, id);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_id        ON messages(recipient, id);  -- channel-agnostic mail
```
```sql
CREATE TABLE IF NOT EXISTS sessions (
    handle        TEXT PRIMARY KEY,
    repo          TEXT,
    status        TEXT NOT NULL DEFAULT 'online',   -- 'online' | 'offline'
    current_task  TEXT,                             -- free-text "what I'm working on" (replaces the board)
    last_seen     TEXT NOT NULL DEFAULT (datetime('now')),
    registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### 4.2 Read filter
A reader supplies its own handle as `as_handle` and sees **broadcast + its own directed
mail**:
```sql
WHERE channel = ? AND id > ?
  AND (recipient IS NULL OR recipient = ?)      -- ? = as_handle
ORDER BY id LIMIT ?
```
Omitting `as_handle` → recipient filter dropped → v1 behavior (see all).

**Channel optional (channel-agnostic mail).** When `channel` is omitted and `as_handle` is
given, drop the `channel = ?` clause and match directed mail to that handle on any channel:
```sql
WHERE id > ? AND recipient = ?   ORDER BY id LIMIT ?
```
This backs the ambient watcher (§6): a freshly-joined session listens for anything addressed
to it before it knows which feature channel matters. (Channel-agnostic reads exclude
broadcasts by design — an ambient listener must not wake on every channel's broadcast
traffic.)

### 4.3 Tool surface (5 tools — only what the MODEL calls)
Rule: a capability is an MCP tool only if the *model* invokes it; hook-only capabilities are
HTTP routes (§4.4), off the model's tool schema.
- `usage()` — slim self-doc (GUIDE, §12).
- `post_message(channel, author, body, to=None)` — `to` = recipient handle; `None` =
  broadcast. Return shape gains `recipient`.
- `read_messages(channel=None, since_id=0, limit=50, as_handle=None)` — `as_handle` is the
  reader's own handle for the inbox filter; `channel` optional with `as_handle` set.
- `list_channels()` — unchanged.
- `list_sessions()` → `[{handle, repo, status, current_task, last_seen}]`. Presence +
  activity discovery in one read (this is what replaced the announce board).

**Naming:** the sender sets `to=` (where it's going); the reader sets `as_handle=` (who I am).
Distinct words for the distinct concepts the review flagged as confusing.

**Dropped as tools:** `wait_for_message`, `watch_channel` — idle-wake is the watcher's job
(§6, "watcher only" decision). Keeps the surface at 5.

### 4.4 HTTP routes (hook/curl-facing — NOT tools)
- **`/wait`** — long-poll. `GET /wait?as_handle=…[&channel=…]&since_id=…&timeout=600`.
  `channel` optional when `as_handle` set (§4.2); returns `400` only if both missing.
  Returns `{"messages":[…]}`. **Side effect: bumps `last_seen` for `as_handle`** — a parked
  watcher is itself the liveness heartbeat (fixes the TTL-vs-watcher staleness, §4.6). Mounts
  at root `/wait`.
- **`/register`** — `POST /register` with `handle`, `repo`, optional `status`
  (default `online`), optional `current_task`. Upserts the `sessions` row, bumps
  `last_seen`. Idempotent; also the explicit heartbeat. **POST, not GET** (it mutates).
  Called by hooks/skills via curl; not a tool — the model never registers.

### 4.5 Presence + activity (TTL)
- `/register` and `/wait` both bump `last_seen`, so any online or parked-listening session
  stays fresh.
- `list_sessions()` derives effective status: a row whose `last_seen` is older than
  `PRESENCE_TTL` (default **900s**, deliberately > the 600s watcher timeout so a parked
  watcher never ages out mid-wait) reports `offline`. Crashed sessions age out.
- `current_task` is set/updated on `/register` (e.g. the join flow and the
  `conclude` teardown set it); it is the "I'm working on XYZ, reach me at <handle>" signal,
  visible to every peer via `list_sessions()` — including idle peers, which the old
  read-on-event board could not reach.

### 4.6 What stays stateless
Message **routing** never consults `sessions` — purely the `recipient` WHERE clause. The
registry is **discovery + presence + activity display only**. Append-only and
stateless-routing both hold; presence is the one conscious exception (like push relaxed spec
§9), and it is kept out of the routing path.

---

## 5. Hook pack — global install, dormant until activated

**Install scope: global / user**, via `install-hooks.sh` → hook entries in
`~/.claude/settings.json`, scripts + skills under `~/.claude/`. But **opt-out by default**:

### 5.1 The activation gate
Every hook's first action is an **activation check**: compute the handle, test for
`~/.contract-bus/<handle>/active`. **Absent → `exit 0` immediately** (one file-stat; an
unrelated session pays essentially nothing — no register, no watcher, no injection). Present
→ run the hook body. This is what makes a machine-wide install cheap.

**Activation (human-triggered, mid-session):** the human says "this task needs the bus" (or
similar). The model invokes the `join-contract-bus` skill (§7.1), which runs
`join_session.sh`: derive handle, `POST /register` (online, with `current_task`), write the
`active` marker + `identity` file, start the ambient watcher (§6), and inject one identity
line. From then on this session's hooks are live.

**Re-arm on resume:** a `--resume`d session that was active still has its `active` marker
(state dir persists, handle is stable), so SessionStart finds it and re-arms (re-register +
restart watcher) with no human action.

### 5.2 State files — `~/.contract-bus/<handle>/`
Unique per session via the handle (§3). Tiny (bytes each).
- `active` — the activation marker (presence of file = opted in).
- `identity` — the handle.
- `cursor` — highest directed-mail `id` seen.
- `watcher.pid` — the single ambient watcher's pid (for liveness/reap).

### 5.3 Hook events (all no-op unless active)
| Event | Action (only if `active` marker present) |
|---|---|
| **SessionStart** | Compute handle; if `active` marker exists (resumed active session) → `POST /register` online, restart the ambient watcher (single-instance, §6), inject the one-line identity/announce directive. Else exit 0. Also run the **TTL reaper** (§5.4) — cheap, unconditional. |
| **Stop / SubagentStop** | Ensure the watcher is alive (relaunch if dead, single-instance). If there is unseen mail, inject a **bodyless pointer** only — `"You have unread contract-bus mail; call read_messages(as_handle=…, since_id=…) to read it."` **Never inject message bodies, never `decision:block`-auto-continue** (security, §5.5). |
| **PostToolUse** (bus tools) | Persist the highest `id` seen to `cursor`. Ensure the watcher is alive (single-instance). |
| **SessionEnd** | `POST /register` status=offline; reap the watcher. **Does NOT delete the state dir** (a session may `--resume`). |

**Dropped vs the earlier draft:** **UserPromptSubmit** (the synchronous mail-fetch — it sat
on every prompt's critical path, couldn't be backgrounded, and duplicated the watcher) and
**PreCompact** (cursors live in files, so compaction can't threaten them — the note was dead
weight). See §13.

### 5.4 Cleanup — prune on finish + TTL backstop
- **Prune on finish (human-deemed):** `conclude-bus-session` skill (§7.3) → `conclude_session.sh <handle>`: `POST /register` status=offline (clear `current_task`), reap the watcher, **`rm -rf ~/.contract-bus/<handle>/`**. The deliberate "this work is done" teardown — distinct from SessionEnd's close.
- **TTL backstop:** the SessionStart reaper deletes any `~/.contract-bus/<handle>/` whose files are older than `STATE_TTL_DAYS` (default 7) and whose handle is offline/absent in `list_sessions`. A long-running session is never reaped (its files keep getting touched). Safety net for "forgot to conclude."

### 5.5 Security — mail is data, never an injected directive
The watcher delivers mail as its curl's JSON stdout (framed as command output = data) and
`read_messages` delivers it as a tool result the model *chose* to fetch. **Hooks never
prepend peer-authored bodies into context, and never force continuation on them.** This
closes the confused-deputy / prompt-injection path the review flagged: a peer that ingested
untrusted content cannot steer your session, because nothing auto-injects its text as
instructions. The Stop hook surfaces only a bodyless "you have mail" pointer; the model pulls
the content deliberately.

### 5.6 Graceful degradation
Every hook uses a short connect timeout; on any failure (daemon down/refused/non-200/stall)
it **exits 0 silently** — no error, no block, no injected text. The bus being down never
breaks a session. Active-session bus calls run in the background where they don't gate a
turn.

---

## 6. Idle-wake — the ambient watcher (sole mechanism)

An active session runs **one** background watcher: a backgrounded
`curl /wait?as_handle=<handle>&since_id=<cursor>&timeout=600` (no channel — wakes on mail to
this handle on any channel, §4.2). It parks server-side (**0 model tokens**), and **exits
when mail lands or the timeout elapses**. Its exit surfaces as a background-task completion,
which wakes the session; the model then reads the delivered JSON / calls `read_messages`. A
hook (Stop/PostToolUse) relaunches it with the advanced cursor.

**Single-instance invariant (fixes the orphan/duplicate bug farm):** before launching,
`kill -0 $(cat watcher.pid)` — if alive, do nothing; if dead/absent, launch and write the
new pid atomically. Only Stop and PostToolUse launch, and both go through this guard, so no
two live watchers and no lost-pid orphans.

**Empirical basis + fragility (honest):** that a completing background task wakes a *fully
idle* session was observed directly (2026-06-26, this session: a backgrounded `sleep 60` exit
re-invoked the idle session via a `task-notification`). This is **undocumented Claude Code
behavior** and could change in a point release. The "watcher only" choice accepts that risk
for leanness. Mitigation: a **build-time canary test** (§9) re-verifies background-exit
idle-wake, so a harness change is *caught loudly*, not degraded silently. If the canary ever
fails, the fallback is to reintroduce an explicit `wait_for_message` tool (model chooses to
wait — documented, robust).

**Cost:** 0 tokens parked; one turn per delivery (hit or timeout); long timeout is cheaper
(fewer re-entries), capped at `MAX_WAIT`=600s.

---

## 7. Skills (three, lazy-loaded → 0 baseline tokens)

All ship in the hook pack (user scope). Each single-sources tool signatures from
`usage()`/instructions (skills = WHEN + PROTOCOL; instructions/usage = WHAT).

### 7.1 `join-contract-bus` — activation + participant protocol
- **Trigger (`description`):** the human signals a task needs cross-session coordination /
  "use the contract bus" / "this needs the bus."
- **Body:** run `join_session.sh` (activate: register, marker, watcher, identity). Then the
  participant runbook: set `current_task` (your "WORKING on X, reach me at <handle>"),
  `list_sessions()` to find the peer, `post_message(..., to=peer)` or broadcast, the watcher
  delivers replies, advance the cursor, reply; on finish use `conclude-bus-session`.

### 7.2 `orchestrating-contract-bus-sessions` — soft orchestrator (slimmed)
- **Trigger:** "coordinate/delegate across multiple existing sessions in different repos."
- **Honest framing (kept short):** the bus has **no spawning, no shared task list, no
  shutdown control** — peers are autonomous; you *ask*, not command. For ephemeral
  *same-repo* parallel work, use Claude Code **agent teams** instead (§11); reach for the bus
  only for durable *cross-repo* coordination of pre-existing sessions.
- **Pattern:** `list_sessions()` (who's live + `current_task`) → directed `post_message`
  requests → the watcher delivers responses → synthesize. The free-text `ASSIGN/ACK/DONE`
  convention is offered as *optional* lightweight tagging, not a framework, and explicitly
  not a reimplementation of agent-teams' task list.

### 7.3 `conclude-bus-session` — human-triggered teardown
- **Trigger:** the human deems the session's bus work finished ("wind down the bus session").
- **Body:** run `conclude_session.sh <handle>` (§5.4): offline + clear `current_task`, reap
  watcher, `rm -rf` the state dir. Confirm what was cleaned up. A skill (not the SessionEnd
  hook) because concluding is a one-time human judgement, and SessionEnd must preserve state
  for `--resume`.

> Cut from the earlier 4-skill draft: a standalone `announce-on-task` skill (its "announce
> your work" is now one line of the join/participant runbook setting `current_task`), and a
> separate `coordinating` skill (merged into `join`). See §13.

---

## 8. Components & boundaries

| Unit | Does | Tested by |
|---|---|---|
| Server storage helpers (`_post_message`, `_read_messages` w/ recipient+channel-optional, `_register`, `_list_sessions`, migration) | pure SQLite logic | unit tests vs temp DB |
| Long-poll helper (`_wait_for_message`, recipient-aware, last_seen bump) | recipient/channel-optional long-poll | `asyncio.run` unit tests |
| MCP tools + `/wait` + `/register` routes | thin wrappers | build-time e2e |
| Hook scripts (`join_session.sh`, `conclude_session.sh`, the event hooks) | activation gate, watcher single-instance, cursor, reaper, graceful degradation | shell/e2e in a live session |
| Skills (3) | trigger + protocol | review |

Hook logic stays thin and delegates to small, testable pieces (the activation check,
single-instance launch, and reaper are the only non-trivial shell — keep them as functions
in one sourced `bus_hooks.sh` so they're not copy-pasted across events).

---

## 9. Testing

- **Unit (extend `test_bus_server.py`):** recipient filter (broadcast vs directed vs
  cross-talk isolation); channel-agnostic `as_handle` read (mail to me any channel,
  broadcasts excluded); migration adds column idempotently on a pre-v2 DB; `sessions` upsert
  incl. `current_task`; TTL aging with `PRESENCE_TTL` > watcher timeout; `list_sessions`
  shape includes `current_task`; recipient-aware long-poll (with/without channel);
  `/wait` bumps `last_seen`.
- **Build-time e2e:** `/wait?as_handle=` (no channel) wakes on mail to that handle on any
  channel; `/wait` with neither channel nor `as_handle` → 400; `/register` (POST) upserts +
  sets online/`current_task`; a directed message wakes only its recipient's watcher; TTL
  ages a silent session offline but NOT one whose watcher is parked (last_seen bumped).
- **Idle-wake canary (build-time, load-bearing):** background a `sleep` that exits, let the
  session go idle, assert the completion re-invokes it. This re-verifies the undocumented
  behavior §6 depends on — a harness change fails loudly here.
- **Hook e2e (live session):** dormant session (no `active` marker) → every hook exits 0,
  no register, no watcher (the leanness/opt-out guarantee); `join-contract-bus` activates
  (marker + register + single watcher + identity line); 5 sessions in one repo → 5 distinct
  state dirs; directed mail on an arbitrary channel wakes an idle active session; Stop
  injects only a bodyless pointer, never a body (security); single-instance watcher (no
  duplicate/orphan after repeated Stop/PostToolUse); cursor advances; SessionEnd marks
  offline but keeps the dir; `conclude-bus-session` prunes it; TTL reaper removes a stale
  offline dir while sparing a live one; daemon-down → all hooks exit 0 silently.

---

## 10. Non-negotiables carried forward
- Bind `127.0.0.1` only; no auth.
- Always bound SQL parameters; never string-format SQL.
- Validate minimally (non-empty channel/body; clamp limit ≤ 200).
- Append-only; no edit/delete; no content validation.
- Presence/activity is discovery-only and the one conscious exception to "stateless about
  consumers"; routing never consults it.

---

## 11. Relationship to Claude Code agent teams

Claude Code ships an official experimental feature, **agent teams**
(`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`; docs code.claude.com/docs/en/agent-teams, v2.1.178;
this Claude is 2.1.191). It is adjacent but solves a different problem; the orchestrator skill
must position the bus as complementary, not competing.

| Dimension | Agent teams | contract-bus |
|---|---|---|
| Sessions | Lead **spawns** ephemeral teammates | Connects **pre-existing, independent** sessions |
| Lifespan | Die with lead; one team/session; no `/resume` of teammates | **Durable** — survive restart/crash/resume |
| Workspace | Effectively single workspace | **Cross-repo**, each its own CLAUDE.md |
| Topology | Hub-and-spoke, fixed lead | **Peer-to-peer**, leaderless |
| Transport | In-memory mailbox + shared task list (`~/.claude/teams`,`tasks`) | HTTP daemon + **append-only SQLite** |
| Coordination | Native task list — claims, deps, file-locking | Messaging + a discovery registry |
| Humans | One human, one terminal | Possibly **multiple humans / terminals** |
| Control | Lead assigns, shuts down, approves plans | **No spawn/shutdown/force** — peers autonomous |
| Best for | Ephemeral parallel work in one repo | **Ongoing cross-repo coordination** |

**Takeaway:** the bus is what agent teams structurally cannot be — cross-session, cross-repo,
persistent, leaderless, multi-human. Use both where apt: agent teams for intra-repo fan-out,
contract-bus to coordinate with a separate long-lived session elsewhere. The orchestrator
skill defers to agent teams for same-repo parallelism.

---

## 12. Context budget (leanness)

Rule (tool vs route): a capability is an MCP tool only if the model calls it; hook-only
capabilities are routes. That keeps `/register` and `/wait` off the schema.

| Item | When | Budget |
|---|---|---|
| MCP tool schemas (5: `usage`, `post_message`, `read_messages`, `list_channels`, `list_sessions`) | always (globally registered) | the floor |
| `GUIDE`/`instructions` | always | **hard cap ≤ 150 tokens** (single source for `instructions=` and `usage()`) |
| Dormant session hook cost | every non-bus session | **~1 file-stat** (activation check) → exit 0 |
| Active session SessionStart inject | active only | **1 line** |
| Skills (3) | on demand | **0 baseline** |
| Ambient watcher | active only | **0 tokens** (background process) |
| Mail / pointers | per delivery | only when mail exists; bodies pulled, not pushed |

**Honest floor:** because the MCP server is registered globally so any session *can* join,
the **5 tool schemas + GUIDE (~600–700 tokens) load in every session, even dormant ones.**
The activation gate removes the *active* costs (watcher, register, injections, per-event
work), not the tool schemas. Zero-for-dormant would require per-repo MCP registration, which
sacrifices "any session can opt in." This is the accepted tradeoff for ambient availability;
the number is small (~0.3% of a 200k window) and flat regardless of fleet size or activity.

**Implementation checklist:** (1) only the 5 tools are `@mcp.tool`; (2) `/register`,`/wait`
are `@mcp.custom_route`; (3) `GUIDE` ≤ 150 tokens, single-sourced; (4) docstrings
trigger-clear but trim; (5) every hook starts with the activation check; (6) all 3 skills are
separate lazy files, never inlined into `GUIDE`.

---

## 13. Adversarial-review change log (do not re-add as "missing")

- **Activation gate added** — global install is now opt-out/dormant (the human opts a session
  in), fixing machine-wide blast radius while keeping auto-join.
- **Announce board removed** → `current_task` column on `sessions`. One read
  (`list_sessions`) gives who's online + what they're doing, reaches idle peers, no second
  channel/cursor/staleness cross-reference.
- **UserPromptSubmit mail-fetch removed** — synchronous, on every prompt's critical path,
  un-backgroundable, duplicated the watcher.
- **PreCompact removed** — cursors live in files; compaction can't threaten them.
- **Hook-injected message bodies + `decision:block` auto-continue removed** — closes the
  confused-deputy/injection path. Mail is data (curl JSON / tool result), never an injected
  directive; Stop surfaces only a bodyless pointer.
- **Watcher hardened** — single-instance invariant (kill -0 + atomic pid) kills the
  orphan/duplicate-watcher bugs.
- **Presence staleness fixed** — `/wait` bumps `last_seen`; `PRESENCE_TTL` (900s) > watcher
  timeout (600s), so a parked-but-alive session never ages out.
- **Handle suffix widened** to `session_id[:8]`; the "never collide" guarantee dropped.
- **Skills cut 4 → 3** — `announce-on-task` folded into the join/participant runbook
  (`current_task`); `coordinating` merged into `join`.
- **`/register` is POST** (it mutates); read/sender params renamed to `as_handle`/`to` to
  end the recipient-overload confusion.
- **Idle-wake = watcher only**, with a build-time **canary test** so the undocumented
  background-exit-wakes-idle behavior fails loudly if the harness changes, rather than
  degrading silently. Documented fallback: reintroduce an explicit `wait_for_message` tool.

**Kept because sound:** the `recipient` nullable column as a WHERE-clause filter; presence as
discovery-only, never in routing; idempotent additive migration; dropping
multi-machine/auth/TLS; the tool-vs-route discipline; §11's honest agent-teams tabulation.

---

## 14. Packaging — a Claude Code plugin

contract-bus ships as a **Claude Code plugin**, so install is `/plugin install` instead of a
manual `claude mcp add` + `install-hooks.sh` + `install-service.sh`. The plugin format was
confirmed empirically against installed plugins (serena, hookify, superpowers).

### 14.1 Layout
```
contract-bus/                      # the plugin (also the repo root, or a subdir)
├── .claude-plugin/plugin.json     # {name, description, author} — metadata only
├── .mcp.json                      # MCP server declaration (see 14.2)
├── bus_server.py                  # the daemon (v1 + v2)
├── bus_cli.py                     # hook brain (Python) + daemon ensure/auto-spawn
├── hooks/hooks.json               # event → command map (see 14.3)
├── skills/
│   ├── join-contract-bus/SKILL.md
│   ├── orchestrating-contract-bus-sessions/SKILL.md
│   └── conclude-bus-session/SKILL.md
├── commands/                      # optional slash commands: bus-status, bus-join, bus-conclude
├── test_bus_server.py · test_bus_cli.py
└── README.md · requirements.txt
```

### 14.2 MCP declaration — connect, not spawn
Unlike serena (which spawns a per-session stdio server), contract-bus needs **one shared
daemon**, so `.mcp.json` connects by URL:
```json
{ "contract-bus": { "url": "http://127.0.0.1:9100/mcp" } }
```
The model-facing tools (§4.3) load from this connection. The daemon process is ensured-up by
14.4 — the URL only *connects*.

### 14.3 Hooks via the plugin
`hooks/hooks.json` maps the 5 events (§5.3) to `python3 "${CLAUDE_PLUGIN_ROOT}/bus_cli.py
<event>"`, e.g.:
```json
{ "hooks": {
  "SessionStart": [{ "matcher": "startup|clear|compact",
    "hooks": [{ "type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/bus_cli.py\" session-start", "async": false, "timeout": 10 }] }],
  "Stop":        [{ "hooks": [{ "type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/bus_cli.py\" stop", "timeout": 10 }] }],
  "SubagentStop":[{ "hooks": [{ "type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/bus_cli.py\" stop", "timeout": 10 }] }],
  "PostToolUse": [{ "hooks": [{ "type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/bus_cli.py\" post-tool-use", "timeout": 10 }] }],
  "SessionEnd":  [{ "hooks": [{ "type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/bus_cli.py\" session-end", "timeout": 10 }] }]
}}
```
The plugin install is global by nature (a user-installed plugin applies to all sessions),
which is exactly the §5 "global install, dormant until activated" model — the activation gate
(§5.1) keeps non-bus sessions free. Hooks being Python (`bus_cli.py`) keeps logic testable
(§9), not stranded in bash.

### 14.4 Daemon lifecycle — auto-spawn singleton
Because `.mcp.json` only connects, the daemon must be running. Two layers:
- **Auto-spawn (default):** the `join` flow (`bus_cli.py` activation, §5.1) ensures the
  singleton daemon is up before registering — a flock-guarded `nohup python3 bus_server.py`
  (borrowed from bobnet's auto-spawn; the flock makes concurrent first-joiners race-safe).
  First session to opt in starts the daemon; the rest connect to it. After spawn, the
  session's MCP client auto-reconnects (HTTP transport) so the tools appear (or
  `/mcp reconnect contract-bus`).
- **Always-on (optional):** the existing LaunchAgent (`install-service.sh`) can still pin the
  daemon at login so the MCP connects with zero reconnect on the very first session. Now
  optional, not required.

The daemon stays a single shared process on `127.0.0.1:9100` (the architectural
non-negotiable, §2) — the plugin changes how it's *started and distributed*, not what it is.

### 14.5 What the plugin replaces
`install-hooks.sh` (→ `hooks/hooks.json` in the plugin) and the manual `claude mcp add`
(→ `.mcp.json`). `install-service.sh` survives as the optional always-on path (14.4). This is
the deliverable shape the implementation plan targets.
