# wc3-tester handoff

## Goal
A Codex plugin that lets an agent drive Warcraft III for automated map testing
while the game runs in the background: mouse, keyboard, screenshots, crash
analysis.

## Current state (2026-10-27)
- Plugin is complete: `.codex-plugin/plugin.json` + `.mcp.json` (Python MCP stdio
  server `wc3`, 16 tools at the time; 20 since 2026-10-28) + skill `skills/wc3-map-testing`.
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
- Still open: the virtual cursor has no x64 stub (Reforged).

## Update 2026-10-28: several instances + LAN (branch feat/multi-instance-lan)
- Goal: background/maximized start without focus; several 1.29c copies that
  join each other's LAN lobby, to test player leaves.
- Done: instance registry (p1..pN, every tool takes `instance`), `launcher.py`
  (WMI start = no focus, ShowWindowAsync maximize, single-instance event
  release, LAN name via `String\userlocal`), launch wrapper setting,
  `lanrelay.py`, `wc3_launch_lan`, `wc3_set_launch_wrapper`, `wc3_instances`,
  `wc3_select`, `wc3_kill(all)`. 20 tools.
- Verified on 1.29c: two copies start in the background, maximized, focus
  unchanged; LAN screen shows the per-copy name ("Bot1").
- Verified with fakes: relay test (fake host/client UDP), registry test,
  multi-instance MCP test.
- Deviations: 1.29c ignores `netgameport` for the LAN socket (always UDP 6112,
  exclusive), so the 1.27/YDWE per-port trick does not carry over. Broadcasts
  from loopback-bound sockets do not reach other loopback addresses, hence
  the relay. Smoke/e2e click+scale checks fail on main too since the display
  moved to 200% scaling (fake game is DPI-unaware); not caused by this branch.

## Update 2026-10-28: built-in bind hook (no ForceBindIP install)
- The missing ForceBindIP function is now implemented in `bindhook.py`; LAN
  launches no longer need a user-installed tool. It finds each module's
  WS2_32/WSOCK32 import slots for `bind`, `connect`, `sendto`, `WSAConnect`,
  `WSASendTo` and redirects them to generated x86/x64 stubs in the game. The
  `bind` stub rewrites a wildcard `0.0.0.0` IPv4 address to `127.0.0.N`; the
  others pre-bind a not-yet-bound socket before forwarding. A watcher patches
  modules loaded later. Only the target process's IAT and an owned RWX block
  change; no files, and the block is reused if the MCP server restarts.
- Launches try the hook immediately after process creation (classic) and again
  after the window appears; failures during the early attempt are non-fatal,
  the post-window install is authoritative. `lan_binding`/`bind_hook` are in
  launch/status output, and `lan_binding` is listed by `wc3_instances`. An external wrapper still overrides the
  built-in hook when configured.
- Verified with `tests/test_bindhook.py`: x64 hook in a Python socket probe
  (`bind`, unbound `sendto` and unbound `connect` -> 127.0.0.9, uninstall
  restores 0.0.0.0, block reuse after `forget`), and x86 hook in a compiled
  32-bit Winsock probe (`bind`, `connect`, `sendto`, `WSAConnect`, `WSASendTo`
  -> 127.0.0.10; peers see that source). Existing smoke 25/25, e2e 20 tools,
  multi-instance and relay tests still pass.
- Open: real two-copy join through the built-in hook + relay is untested (no
  WC3 install on the build machine). Unknown: whether 1.29c creates its LAN
  socket before the hook goes in (the early attempt is meant to close that
  window), whether a host errors when its own broadcast fails, and whether the
  client accepts the relayed GAMEINFO's source IP (expected per the protocol).
  The hook cannot catch a build that resolves Winsock via `GetProcAddress`
  instead of the import table; a wrapper remains the fallback for that.

## Next steps
1. On a machine with WC3, run `wc3_launch_lan(count=2)`, host on p1, join from
   p2, and check both sides. `lan_binding` must be `"builtin"`, `bind_hook`
   should list patched modules, and `wc3_instances()["lan_relay"]["games"]`
   should show the host. If the list stays empty, verify the game's imports
   (`bind_hook` module names) before considering the external wrapper fallback.
2. Run `wc3_launch` on a map, then check that background clicks and keys work.
   If not, try `mode="foreground"` and record which build needs which mode.
3. Check where that build writes Errors/Logs, and adjust `crash.error_dirs` if
   needed.

## Verified 2026-10-28: real 1.29c two-copy LAN join (built-in hook + relay)
- `launch_lan(count=2, names=["Host1","Guest2"])`: both copies started in the
  background, maximized, focus unchanged; hooks patched 3 slots each in
  `Warcraft III.exe` (x86). On the LAN screen p1 held UDP 127.0.0.1:6112 and
  p2 held UDP 127.0.0.2:6112 (previously the second copy failed to open LAN).
- p1 created a LAN game; the relay learned LAN version 29 and forwarded the
  GAMEINFO; p2's list showed "本地遊戲(Host1) (1/12)"; p2 joined and the host
  lobby showed Host1 + Guest2 ("Guest2已經加入遊戲"), with an established TCP
  connection 127.0.0.2 -> 127.0.0.1:6112.
- Killing p2's process: the host printed "Guest2已經離開遊戲" and reopened the
  slot. The match itself was not started. The LAN name was restored afterwards.
- Seen during the test: the first background click after a screen change was
  sometimes not taken (map list, LAN game list); a second click worked.
  A process killed outside `wc3_kill` stays in the relay list until the
  server restarts (harmless; it just gets no answer).
