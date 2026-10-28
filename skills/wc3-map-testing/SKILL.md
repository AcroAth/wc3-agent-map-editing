---
name: wc3-map-testing
description: Test Warcraft III maps with the wc3 MCP tools — launch/attach to the game, inspect it with screenshots, drive it with background or foreground input, and diagnose crashes from exit codes, WC3 error logs, WER reports and dumps.
---

# Testing Warcraft III maps with the wc3 MCP server

Use this skill whenever a task involves running or debugging a Warcraft III map
(classic 1.26–1.31 or Reforged 1.32+) on Windows with the `wc3` MCP tools.

## Test loop

1. `wc3_launch(map_path="C:\\maps\\MyMap.w3x")` — or `wc3_attach()` if the game
   is already running. Launch always uses `-nowfpause` by default so the game
   keeps simulating while unfocused.
2. `wc3_wait(until="responsive", seconds=30)` — never act on a window that is
   still loading or hung.
3. `wc3_screenshot()` — establish the starting screen. Read `scale` and
   `client_width/height` from the returned metadata.
4. Act: `wc3_click`, `wc3_key`, `wc3_chat`, `wc3_drag`, `wc3_scroll`.
5. `wc3_screenshot()` again after each meaningful action; compare visually.
6. On any anomaly (frozen screen, unexpected exit, error dialog):
   `wc3_status()` then `wc3_crash_report()`, and `wc3_analyze_dump(path)` for
   any `.dmp` in `new_files`.
7. Always finish with `wc3_kill()` so no game process is left behind.

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
- On classic WC3 the click result says `"virtual_cursor": true`: the game's
  cursor was moved virtually and the click landed at your coordinates. Menus,
  dropdowns (click the arrow, then the item) and double-clicks all work in
  background mode. Take a screenshot after opening a dropdown to read its items.
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

- Launch only once per test; `wc3_launch` replaces the current session. Use
  `wc3_kill()` between runs to avoid stale windows.
- `wc3_wait(until="exit")` is the clean way to detect that the map quit or
  crashed after an action.
- Screenshots use `printwindow` first (works covered). If they are black, try
  `method="screen"` with the window visible, or install `windows-capture`.
- Prefer several small actions with screenshots over one long scripted sequence:
  you cannot see intermediate state otherwise.
