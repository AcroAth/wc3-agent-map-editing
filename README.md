# wc3-tester

A Codex plugin with an MCP (stdio) server that lets an agent control
**Warcraft III** for automated map testing: classic 1.26–1.31 (`war3.exe`) and
Reforged 1.32+ (`Warcraft III.exe` under `_retail_\x86_64\`). The game can run
in the background, unfocused and covered by other windows.

## Install

Requires Windows 10/11 and Python 3.11+ (`py` launcher or `python` on PATH).

```bat
codex plugin marketplace add AcroAth/wc3-tester
codex plugin add wc3-tester@wc3-tester
```

To work on a local clone instead, pass its folder:
`codex plugin marketplace add C:\path\to\wc3-tester`.

Start a new Codex chat after installing or updating. A running chat keeps the
server it started with.

The first launch runs `scripts\run_server.cmd`. It creates a venv in
`%LOCALAPPDATA%\wc3-tester\venv` (override with `WC3_TESTER_VENV`), which stays
outside the plugin folder because deep plugin-cache paths break pip on systems
without long-path support. It then installs `requirements.txt` (`mcp<2`, Pillow,
psutil, minidump). Delete the `.wc3-tester-deps-v2` marker in the venv to force
a reinstall. Nothing is written to stdout before the MCP server starts, so the
stdio channel stays clean.

Point the server at your game if it is not in a default location:

```bat
setx WC3_EXE "C:\Program Files (x86)\Warcraft III\_retail_\x86_64\Warcraft III.exe"
```

Optional, for better diagnostics:

- `%LOCALAPPDATA%\wc3-tester\venv\Scripts\pip install windows-capture`: a
  Windows Graphics Capture fallback for windows that return black frames from
  PrintWindow.
- Windows SDK **Debugging Tools for Windows**: provides `cdb.exe`, which is used
  automatically for deeper dump analysis (`!analyze -v`). Without it, dumps are
  parsed with the bundled pure-Python `minidump` package.

Environment variables (all optional):

| Variable | Meaning |
| --- | --- |
| `WC3_EXE` | Path to the game executable |
| `WC3_INPUT_MODE` | Default `background` or `foreground` |
| `WC3_INPUT_DELAY_MS` | Delay between posted messages (default 30) |
| `WC3_ERRORS_DIRS` | Extra `;`-separated error/log directories |
| `WC3_TESTER_OUT` | Root for session folders (screenshots, runs) |
| `WC3_TESTER_VENV` | Where the launcher keeps its venv |
| `WC3_LAUNCH_WRAPPER` | Optional external LAN wrapper, e.g. `ForceBindIP.exe {ip}` (overrides the built-in hook) |
| `WC3_SYMPATH` | Symbol path for `cdb` (enables `srv*<path>*`) |
| `WC3_VIRTUAL_CURSOR` | `0` disables the virtual cursor for classic WC3 |
| `WC3_FRAME_MS` | Wait after moving the virtual cursor (default 50) |

Session output defaults to `%LOCALAPPDATA%\wc3-tester\runs\<timestamp>\`.

## Tools

| Tool | Purpose |
| --- | --- |
| `wc3_launch(map_path, exe, windowed, nowfpause, extra_args, flavor, instance, background, maximized, lan_ip, player_name, wrapper)` | Start the game (and optionally load a map), wait for its window |
| `wc3_launch_lan(count, exe, map_path, names, background, maximized, wrapper)` | Start p1..pN that can find each other's LAN games (see below) |
| `wc3_set_launch_wrapper(template)` | Optionally override the built-in LAN socket binding with an external wrapper |
| `wc3_attach(pid, title, instance)` | Attach to a running game |
| `wc3_instances()` | List instances, their state, and the LAN relay status |
| `wc3_select(instance)` | Make an instance the default target |
| `wc3_status()` | State, exit code, hung, uptime, CPU/RAM, fatal dialog |
| `wc3_kill(instance, all)` | Terminate one instance (simulates a player dropping) or all |
| `wc3_screenshot(method, max_width, restore_minimized)` | Client-area screenshot + metadata (`scale`, `client_width/height`, saved path) |
| `wc3_click(x, y, button, clicks, modifiers, normalized, mode)` | Click |
| `wc3_mouse_move(x, y, normalized, mode)` | Move the cursor |
| `wc3_drag(x1, y1, x2, y2, button, steps, normalized, mode)` | Drag / box-select |
| `wc3_scroll(x, y, delta, mode)` | Mouse wheel; `delta` in notches (1 = 120 units) |
| `wc3_key(keys, hold_ms, repeat, mode, send_char)` | Key or chord, e.g. `ctrl+1`, `shift+f2`, `alt+q` |
| `wc3_type(text, mode)` | Type text |
| `wc3_chat(text, to_all, mode)` | Chat line / cheat / `-command` |
| `wc3_wait(seconds, until)` | Wait; `until` = `window`, `exit`, `responsive` |
| `wc3_crash_report(since_iso)` | Exit code, fatal dialog, error logs, WER, event log, verdict |
| `wc3_analyze_dump(path)` | Analyse a minidump with `cdb` or `minidump` |
| `wc3_read_logs(max_files, max_lines)` | Tail the newest error/log files |

Every tool takes an optional `instance` (`"p1"`, `"p2"`, ...); the default is
the last launched, attached or selected one.

All mouse coordinates are **client-area pixels** of the game window, matching a
screenshot with `scale=1` (`client = image_px / scale`). `normalized=True`
accepts 0..1 fractions.

Input modes:

- `background` (default) posts `WM_*` messages and never changes focus. Mouse
  messages go to the deepest child window under the point. Classic WC3 (1.2x,
  32-bit) ignores the coordinates in mouse messages and reads the real cursor
  with `GetCursorPos`. For that build the plugin redirects the game's own
  `GetCursorPos` import to a virtual cursor, so background clicks land on
  target without moving your mouse. The redirect lasts until the game exits.
  Set `WC3_VIRTUAL_CURSOR=0` to turn it off. While it is active, your real mouse
  does not steer the game; any foreground-mode action hands control back.
  Double-clicks are sent as two plain presses, which is how WC3 detects them.
  Keys post only
  `WM_KEYDOWN/UP`, because the game's own `TranslateMessage` produces the
  `WM_CHAR`. Text and chat post `WM_CHAR`.
- `foreground` uses `SendInput`. It brings the window forward and by default
  restores the previous focus afterwards.

`wc3_launch` passes `-nowfpause`, so the game keeps simulating while it is not
focused. Background testing depends on this. With `background=True` (default)
the game is started through WMI, which keeps it from taking keyboard focus; it
is then maximized without being activated and moved behind other windows.

### Several instances and LAN games

Each launch closes the "Warcraft III Game Application" single-instance event in
running games, so another copy can start. `player_name` sets the LAN name of
that copy (registry `String\userlocal`, restored after the start).

Copies on one PC cannot see each other's LAN games on their own: 1.29 always
uses UDP 6112 for LAN and only one copy can hold it (the second shows "unable
to connect to network"). The fix has two parts:

1. **Built-in socket binding** (`bindhook.py`), the useful part of ForceBindIP
   without the external tool. On each LAN launch the plugin redirects the
   game's Winsock imports so instance N binds `127.0.0.N` instead of
   `0.0.0.0`; unbound `connect`/`sendto` sockets are pre-bound too, and modules
   loaded later are covered by a watcher. This works for 32-bit classic and
   64-bit Reforged builds. A user-installed wrapper such as ForceBindIP can
   still be selected with
   `wc3_set_launch_wrapper("C:/Tools/ForceBindIP/ForceBindIP.exe {ip}")`
   (or `WC3_LAUNCH_WRAPPER`); clear both to use the built-in hook.
2. **The plugin's LAN relay** (`lanrelay.py`), a thread in the MCP server.
   Broadcasts from a game bound to 127.0.0.N reach no other copy, so every
   second the relay asks each copy for its hosted game (`W3GS_SEARCHGAME`) and
   forwards the answer (`W3GS_GAMEINFO`) to the others from the host's
   address. It only sends ordinary LAN packets and does not touch the games.

Then: `wc3_launch_lan(count=2)`; host p1 opens LAN → Create Game; p2 opens LAN
and joins. `wc3_kill(instance="p2")` mid-game simulates p2 dropping. The
launch result's `lan_binding` is `"builtin"` or `"wrapper"`, and `bind_hook`
reports the injected block and patched import slots.

### One server shared by all chats

Codex starts one MCP server per chat. Each of those is only a small stdio
proxy: it starts (or finds) one background server and forwards every tool call
to it, so all chats see the same instances, the same `p1`/`p2` numbering and
the same LAN relay.

- The shared server listens on 127.0.0.1 only and refuses requests without the
  random token in `%LOCALAPPDATA%\wc3-tester\daemon.json` (log: `daemon.log`).
- It is started through WMI so that closing the chat that started it does not
  end it. It takes its settings (`WC3_EXE`, ...) from the chat that started it.
- Tools run in worker threads: a long `wc3_wait` in one chat does not hold up
  another. Launch, attach, kill, select and wrapper changes run one at a time.
- "Current instance" is shared too, so with several chats pass `instance`
  explicitly.
- After a plugin update, the old server is replaced on the next call if no
  game is running; otherwise it keeps serving its games until they end.
- It exits after `WC3_DAEMON_IDLE_MIN` minutes (default 30) without calls and
  without running games; the next call starts a new one.
- `WC3_SHARED_SERVER=0` gives each chat its own private server again. The
  proxy also falls back to that if the shared server cannot start.
- `wc3_instances()["server"]` shows the answering server's pid.

The exit code is read through a process handle opened at launch/attach time,
so a crash is reported with its NTSTATUS code (for example
`0xC0000005 ACCESS_VIOLATION`) even for an attached game or a Reforged
re-spawned process.

## Tests

`tests/fake_game.py` is a raw Win32 stand-in (class and title "Warcraft III",
with its own WndProc like the game). It logs input and paints clicks. On F12 it
writes a realistic `Errors\*.txt` and dies with a real access violation
(`0xC0000005`).

On Windows:

```bat
set PYTHONPATH=src
%LOCALAPPDATA%\wc3-tester\venv\Scripts\python tests\smoke_test.py
%LOCALAPPDATA%\wc3-tester\venv\Scripts\python tests\mcp_e2e_test.py
%LOCALAPPDATA%\wc3-tester\venv\Scripts\python tests\multi_instance_test.py
%LOCALAPPDATA%\wc3-tester\venv\Scripts\python tests\test_lanrelay.py
%LOCALAPPDATA%\wc3-tester\venv\Scripts\python tests\test_registry.py
%LOCALAPPDATA%\wc3-tester\venv\Scripts\python tests\test_bindhook.py
%LOCALAPPDATA%\wc3-tester\venv\Scripts\python tests\shared_server_test.py
```

`shared_server_test.py` runs two chats (stdio proxies) against one shared
server in a private `LOCALAPPDATA`. The other MCP tests set
`WC3_SHARED_SERVER=0` because they pass their own folders to the server.

`test_bindhook.py` checks the 64-bit hook against a Python socket probe and,
when a 32-bit MSVC toolchain is installed, compiles and checks a small classic
(x86) Winsock probe as well.

`smoke_test.py` calls the library directly. `mcp_e2e_test.py` drives the same
flow over MCP stdio through `run_server.cmd`, the way Codex launches it. Notepad
covers the game window during the run, and the test checks crash detection for
an *attached* (not launched) game.

## Limitations

- Verified on classic 1.29.2 (32-bit): background menu clicks, double-clicks
  and the lobby player-slot dropdown. Reforged (x64) has no virtual cursor yet,
  so if background clicks miss there, use `mode="foreground"`.
- The virtual cursor and the LAN bind hook work by writing into the game's
  memory. Use them only for local and offline testing, not on Battle.net.
- The LAN bind hook rewrites the game's Winsock import slots. If a build
  resolves Winsock with `GetProcAddress` instead, fall back to an external
  wrapper via `wc3_set_launch_wrapper`.
- `PrintWindow` may return black frames for some DirectX configurations. The
  `auto` method then tries WGC (if installed) and finally a desktop grab.
- Elevated games reject messages from a non-elevated server, so run the agent
  with matching privileges.
- The `screen` capture method only shows the game when its window is visible
  and uncovered.
