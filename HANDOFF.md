# wc3-tester handoff

## Goal
A Codex plugin that lets an agent drive Warcraft III for automated map testing
while the game runs in the background: mouse, keyboard, screenshots, crash
analysis.

## Current state (2026-10-27)
- Plugin is complete: `.codex-plugin/plugin.json` + `.mcp.json` (Python MCP stdio
  server `wc3`, 16 tools) + skill `skills/wc3-map-testing`.
- Verified on Windows 11 against the Win32 stand-in `tests/fake_game.py`:
  `tests/smoke_test.py` 23/23, `tests/mcp_e2e_test.py` 13/13 (the second runs
  over MCP stdio through `run_server.cmd`, with a window covering the game).
- Installed into an isolated CODEX_HOME. `codex exec` then started the server
  from the plugin cache and completed a `wc3_status` call.
- **Not verified:** a real Warcraft III install (none on the build machine),
  `wc3_launch`, foreground (`SendInput`) mode, WGC capture, `cdb` analysis,
  fatal-dialog and hung detection against a real dialog or hang.

## Key decisions
- Python + ctypes only (no pywin32); `mcp<2`, because mcp 2.x renamed FastMCP.
- The venv lives in `%LOCALAPPDATA%\wc3-tester\venv`: pywin32/pip failed on MAX_PATH
  inside deep plugin paths, and a marker file makes a failed install retry.
- Background input = PostMessage to the deepest child under the point. Keys send
  only KEYDOWN/UP, because the game's TranslateMessage makes the WM_CHAR and
  sending both doubled chat text. Text and chat use WM_CHAR.
- The exit code comes from a process handle held since launch/attach, so crashes of
  attached or re-spawned (Reforged) processes keep their NTSTATUS.
- `default_tools_approval_mode: "approve"` so unattended `codex exec` runs are
  not cancelled.

## Deviations found during verification
- The Tk stand-in ignored posted clicks (Tk reads the real mouse state). It was
  replaced with a raw Win32 window, which is closer to how WC3 handles input.
- The blank-frame heuristic (98% same colour on a 32x32 sample) flagged real
  frames. It now flags only uniform frames (full-resolution extrema ≤ 4).
- A fault inside a ctypes callback ends Python with 0xC0000409, so the stand-in
  crashes after leaving the message loop.
- On Windows 11, `notepad.exe` outlives `kill()` (Store app hand-off). The e2e cover
  window is now a self-owned Tk window.

## Update 2026-10-27: real WC3 1.29.2
- Background mouse missed (for example the lobby slot dropdown) because classic
  WC3 reads the position with `GetCursorPos`, not from message coordinates.
  The proof: posted moves gave no hover, while moving the real cursor did.
- Fix: `cursorhook.py` points the exe's IAT slot for USER32!GetCursorPos at an
  x86 stub that returns a virtual position (block tagged "WC3V", reused across
  server restarts). `inputs._bg_mouse_move/_bg_button` set it and wait one frame.
- Double-click: the WC3 window class lacks CS_DBLCLKS, so the second press is
  posted as WM_LBUTTONDOWN.
- Verified with the game unfocused and the real cursor unchanged: main menu,
  Single Player, Custom Game, double-click folder, select map, Player 3
  dropdown → 關閉 (Closed).
- Still open: there is no x64 stub (Reforged).

## Next steps
1. On a machine with WC3, run `wc3_launch` on a map, then check that
   background clicks and keys work. If not, try `mode="foreground"` and record
   which build needs which mode.
2. Check where that build writes Errors/Logs, and adjust `crash.error_dirs` if
   needed.
