---
name: wc3-map-testing
description: Operate the WC3 Tester MCP tools for Warcraft III launches, screenshots, input and crash diagnostics. Use war3-test-ops for test design and evidence review, and war3-map-ops for archive edits and rebuilds.
---

# Testing Warcraft III maps with the wc3 MCP server

Use this tool guide when operating Warcraft III (classic 1.26–1.31 or Reforged
1.32+) on Windows with the `wc3` MCP tools. For a gameplay verification task,
first use [war3-test-ops](../war3-test-ops/SKILL.md) to prepare the fixture,
actions, observations and stopping criteria. Use
[war3-map-ops](../war3-map-ops/SKILL.md) for map modifications. Follow explicit
local requirements about delegation and model choice.

## Test loop

1. Verify the selected Windows machine, client and exact candidate path/hash.
   Inspect `wc3_instances()` and preserve existing sessions. Launch with an
   explicit task-owned instance, for example
   `wc3_launch(map_path="C:\\maps\\MyMap.w3x", instance="map-check")`, or attach
   to an explicitly authorized running game. Pass that instance on every later
   call. Launch uses `-nowfpause` by default so the game keeps simulating unfocused.
2. `wc3_wait(until="responsive", seconds=30)` — never act on a window that is
   still loading or hung.
3. `wc3_screenshot()` — establish the starting screen. Read `scale` and
   `client_width/height` from the returned metadata.
4. Act: `wc3_click`, `wc3_key`, `wc3_chat`, `wc3_drag`, `wc3_scroll`.
5. `wc3_screenshot()` again after each meaningful action; compare visually.
6. On any anomaly (frozen screen, unexpected exit, error dialog):
   `wc3_status()` then `wc3_crash_report()`, and `wc3_analyze_dump(path)` for
   any `.dmp` in `new_files`.
7. Review observations against the prepared criteria. Close only verified
   task-owned temporary instances with `wc3_kill(instance="map-check")` when
   cleanup is appropriate. Preserve user sessions and report any left running.

## Multiplayer (several instances)

- Every tool takes `instance` ("p1", "p2", ...); `wc3_instances()` lists them,
  `wc3_select(instance)` changes the default.
- All Codex chats share one wc3 server, so games started in another chat show
  up in `wc3_instances()` too. Pass `instance` explicitly and do not kill or
  drive instances another chat is using.
- `wc3_launch_lan(count=2, names=["Host", "Guest"])` starts p1..pN in the
  background, each on its own 127.0.0.N. The plugin's built-in bind hook does
  the ForceBindIP-style address binding; no external tool is needed. If the
  user explicitly configured a wrapper with `wc3_set_launch_wrapper`, that is
  used instead. Check `lan_binding`/`bind_hook` in the result and
  `wc3_instances()` if a copy does not appear on LAN.
- Host: p1 presses `l` (LAN) → Create Game → pick the map → Create. Others: `l`,
  wait ~2 s for the game to appear (the relay forwards it once per second;
  `wc3_instances()["lan_relay"]["games"]` shows what it sees), select it, Join.
- Player leaves: `wc3_kill(instance="p2")` (a drop), or drive p2's menu
  (F10 → quit) for a graceful leave. Screenshot the host afterwards.

## Coordinates

- Every mouse tool takes **client-area pixels** of the game window, matching a
  screenshot taken with `scale=1`.
- From a returned screenshot: `client_x = image_x / scale`,
  `client_y = image_y / scale`. Use `client_width/height` to sanity-check.
- `normalized=True` accepts 0..1 fractions of client width/height instead.
- Warcraft III's HUD occupies roughly the bottom 15% of the screen; minimap
  bottom-left, command card bottom-right, unit selection centre-bottom.

## Input modes

- Default is `background` (PostMessage). It never steals focus and works while
  the window is covered. Use it for most testing.
- If background input has **no visible effect** (check with a screenshot before
  and after), retry with `mode="foreground"`. Reforged builds using raw input
  often need this. Foreground input restores the previous focus afterwards.
- Set `WC3_INPUT_MODE=foreground` to change the default for a session.
- On classic WC3 `"virtual_cursor": true` reports use of the virtual cursor
  path; verify the intended UI effect with a fresh observation. For dropdowns,
  click the arrow, inspect the opened items, then select the intended item.
- Chat/cheats: `wc3_chat("iseedeadpeople")`. Single-player only cheats:
  `greedisgood 10000`, `whosyourdaddy`, `warpten`, `iseedeadpeople`,
  `thereisnospoon`, `strengthandhonor`. Map debug commands are usually chat
  commands such as `-test`, `-debug`, `-lvl 10`.
- To open the game menu use `wc3_key("f10")`; `Esc` cancels menus/dialogs.
- Use `wc3_key` for hotkeys and `wc3_type`/`wc3_chat` for text. If a typed key
  never shows up in a chat box in background mode, retry with
  `wc3_key(..., send_char=True)`.
- `wc3_scroll` `delta` is in wheel notches (1 = one click); positive zooms in.

## Common WC3 hotkeys

| Key | Action |
| --- | --- |
| F1–F3 | select hero 1–3 |
| Ctrl+1..9 | assign control group |
| 1..9 | select control group |
| A | attack (then click target/ground) |
| M | move |
| S | stop |
| H | hold position |
| P | patrol |
| B | build / back (in menus) |
| F10 | game menu |
| Esc | cancel / close menu |
| Enter | chat |

Hero abilities are usually bound to Q/W/E/R or the command card position;
click the command card buttons if the map uses custom abilities.

## Reading crash reports

- `wc3_status()` gives `state` (`running|hung|crashed|exited`), `exit_code_hex`
  and `exit_code_name`. `0xC0000005` = ACCESS_VIOLATION (bad pointer/read),
  `0xC00000FD` = STACK_OVERFLOW (runaway recursion, often JASS/Lua),
  `0xC0000409` = STACK_BUFFER_OVERRUN, `0xC0000374` = HEAP_CORRUPTION,
  `0xE06D7363` = C++ exception.
- `wc3_crash_report()` collects a visible FATAL ERROR dialog, new
  `Errors\*.txt` (classic) or `Documents\Warcraft III\Errors` and WER files
  (Reforged), dumps, and Application event-log entries. `parsed` contains the
  exception code/address and lines mentioning `war3map.j`, `JASS`, `Lua` or
  `trigger` — start debugging there.
- Classic error text (`Errors\*.txt`) is plain text with `Exception:` and a
  call stack; Reforged may only leave a WER report or `CrashDumps\*.dmp`.
- `wc3_analyze_dump(path)` uses `cdb.exe` when the Windows SDK Debugging Tools
  are installed, otherwise the Python `minidump` package. Look at the faulting
  module and offset: in classic, `game.dll` holds the engine and JASS VM, so a
  fault there right after a trigger fires usually points at map script; in
  Reforged the engine is inside `Warcraft III.exe`. Faults in graphics or
  audio driver DLLs are usually environment problems, not the map.
- A "hung" window usually indicates an infinite loop, a modal dialog or a
  debug break; take a screenshot and call `wc3_crash_report()` before killing.

## Practical tips

- Launch once per fixture and use an explicit, unused instance ID. Before
  restarting, verify the instance/PID and ownership; do not replace another
  chat's session or use an unqualified kill for cleanup.
- `wc3_wait(until="exit")` is the clean way to detect that the map quit or
  crashed after an action.
- Screenshots use `printwindow` first (works covered). If they are black, try
  `method="screen"` with the window visible, or install `windows-capture`.
- Use fresh screenshots for ordinary navigation. For timing-sensitive tests,
  prepare the supported action sequence and event/capture evidence in advance;
  occasional screenshots cannot establish a short transition. Stop on unknown
  state, and do not improvise blind retries or infer gameplay from an input
  acknowledgement. If frames look stale, verify freshness before further input.
