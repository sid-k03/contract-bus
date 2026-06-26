# Task 6 Report: CLAUDE_PLUGIN_ROOT resolution + quote watch path; skills defer to commands

## Status
COMPLETE — all tests pass, committed.

## Changes Made

### `bus_cli.py`
- **`watch_command()`**: Changed f-string to quote the script path:
  `f'bash "{plugin_root}/bus_watch.sh" {session_id} {handle} {int(since_id)}'`
  Handles paths with spaces (e.g. "Data Bus MCP").
- **`main()` plugin_root resolution**: Changed from `CONTRACT_BUS_PLUGIN_ROOT`-or-`__file__` to
  three-way cascade: `CLAUDE_PLUGIN_ROOT` → `CONTRACT_BUS_PLUGIN_ROOT` → `__file__` dir.

### `test_bus_cli.py`
- Added `test_main_prefers_claude_plugin_root` — monkeypatches `CLAUDE_PLUGIN_ROOT=/plug/root`,
  calls `main(["bus_cli.py", "ensure-daemon"])`, asserts `ensure_daemon` received `/plug/root`.
- Added `test_watch_command_quotes_path_with_spaces` — asserts exact quoted output for a
  path containing spaces.
- Updated three pre-existing tests (`test_watch_command_shape`,
  `test_launch_directive_embeds_handle_cursor_and_floor`,
  `test_join_writes_state_and_returns_directive`) to match the new quoted path format.

### `skills/join-contract-bus/SKILL.md`
- Replaced `## Activate` bash block with `/contract-bus:join` slash command instruction.
  Manual install fallback preserved as a blockquote.

### `skills/conclude-bus-session/SKILL.md`
- Replaced `python3 "<plugin root>/bus_cli.py" conclude-cli ...` run block with
  `/contract-bus:conclude` slash command instruction. Manual install fallback preserved inline.

## Test Summary
87 passed, 0 failed (87 total across all test files).

## Concerns
None. The three pre-existing test updates were mechanical — they tested the old unquoted
format and needed to be updated to the new quoted format. The behaviour contract (the path
appears in the directive) is preserved; only the quoting changed.
