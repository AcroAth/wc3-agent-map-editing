"""MCP stdio server exposing Warcraft III testing tools.

Run with ``python -m wc3_tester.server`` (the launcher script does this inside
a local virtualenv).  stdout belongs to the MCP protocol: log only to stderr.

Every tool accepts an optional ``instance`` id (``"p1"``, ``"p2"``, ...).
Without it the current instance is used: the last launched/attached/selected
one.  Existing single-instance use therefore keeps working unchanged.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from . import capture, crash, game, inputs, lanrelay, winapi, window

log = logging.getLogger("wc3_tester")

mcp = FastMCP("wc3")

INPUT_HINT = (
    "Check that the game is running and its window exists (wc3_status). "
    "If background input has no effect, retry with mode='foreground'."
)

INSTANCE_DOC = (
    'Which game instance (e.g. "p2"); default = the current one, '
    "i.e. the last launched/attached/selected"
)


def _instance_name(instance: str | None = None) -> str | None:
    """Resolve an explicit id, or the id of the current session."""
    if instance is not None:
        return instance
    try:
        session = game.get_session(None)
    except Exception:
        return None
    return session.instance if session else None


def _error(
    exc: BaseException, hint: str = "", instance: str | None = None
) -> dict:
    log.exception("tool error: %s", exc)
    result: dict[str, Any] = {
        "ok": False,
        "error": str(exc),
        "instance": _instance_name(instance),
    }
    if hint:
        result["hint"] = hint
    return result


def _target_hwnd(instance: str | None = None) -> int:
    """The game window of *instance*, attaching automatically if discoverable."""
    session = game.get_session(instance)
    if session and session.hwnd and window.is_window(session.hwnd):
        return int(session.hwnd)
    found = None
    if session and session.pid:
        found = window.find_window(pid=session.pid)
    if not found and not session:
        # First use without launch/attach: adopt the first game window.
        found = window.find_window()
    if not found and session and len(getattr(game, "_instances", {})) <= 1:
        # Preserve the old single-instance recovery after a window re-create.
        found = window.find_window()
    if not found:
        if session:
            raise RuntimeError(
                f"No window found for instance {session.instance!r} "
                "(game exited? call wc3_status or wc3_launch)."
            )
        raise RuntimeError(
            "No Warcraft III window found. Call wc3_launch (or wc3_attach) first."
        )
    pid = window.window_pid(found)
    if not session or session.hwnd != found:
        target = session.instance if session else instance
        try:
            game.attach(pid=pid, instance=target)
        except Exception:
            # Attaching is a convenience; input can still be posted to the hwnd.
            pass
    return int(found)


def _game_state(instance: str | None = None) -> dict:
    """Compact lifecycle snapshot included with every input tool result."""
    try:
        state = game.status(instance)
    except Exception as exc:
        return {"instance": _instance_name(instance), "state": "unknown", "error": str(exc)}
    compact = {
        "instance": state.get("instance", _instance_name(instance)),
    }
    compact.update(
        {
            key: state.get(key)
            for key in (
                "state",
                "running",
                "hung",
                "pid",
                "exit_code",
                "exit_code_hex",
                "exit_code_name",
            )
        }
    )
    if state.get("fatal_dialog"):
        compact["fatal_dialog"] = state["fatal_dialog"]
    return compact


def _with_state(result: dict, instance: str | None = None) -> dict:
    result = dict(result)
    result.setdefault("ok", True)
    name = result.get("instance") or _instance_name(instance)
    result["instance"] = name
    result["game_state"] = _game_state(name)
    return result


def _window_present(session) -> bool:
    if session and session.hwnd and window.is_window(session.hwnd):
        return True
    if session and session.pid:
        return bool(window.find_window(pid=session.pid))
    return bool(window.find_window())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@mcp.tool()
def wc3_launch(
    map_path: str | None = None,
    exe: str | None = None,
    windowed: bool = True,
    nowfpause: bool = True,
    extra_args: list[str] | None = None,
    flavor: str = "auto",
    instance: str | None = None,
    background: bool = True,
    maximized: bool = True,
    lan_ip: str | None = None,
    player_name: str | None = None,
    wrapper: str | None = None,
) -> dict:
    """Launch Warcraft III, optionally loading a map, and wait for its window.

    ``exe`` defaults to the WC3_EXE environment variable, then common install
    paths.  ``flavor`` is auto|classic|reforged.  ``nowfpause`` keeps the game
    running while it is not focused (essential for background testing).
    ``instance`` is the registry id to use (default: the first free p1, p2, ...).
    ``background=True`` starts the game without taking keyboard focus and
    keeps it behind other windows; ``maximized=True`` maximizes the window.
    ``lan_ip`` (e.g. "127.0.0.2") forces the game's sockets onto that address
    with the built-in bind hook (no external tool needed) and adds it to the
    LAN discovery relay; ``player_name`` sets the LAN player name.  A launch
    wrapper (``wrapper`` or the saved one, see wc3_set_launch_wrapper) is used
    instead of the built-in hook when configured. See wc3_launch_lan.
    """
    try:
        info = game.launch(
            exe=exe,
            map_path=map_path,
            windowed=windowed,
            nowfpause=nowfpause,
            extra_args=extra_args,
            flavor=flavor,
            instance=instance,
            background=background,
            maximized=maximized,
            lan_ip=lan_ip,
            player_name=player_name,
            wrapper=wrapper,
        )
        return _with_state(info)
    except Exception as exc:
        return _error(
            exc,
            "Set WC3_EXE to the game executable, or pass exe=. "
            "Classic: ...\\war3.exe, Reforged: ...\\_retail_\\x86_64\\Warcraft III.exe",
            instance=instance,
        )


@mcp.tool()
def wc3_launch_lan(
    count: int = 2,
    exe: str | None = None,
    map_path: str | None = None,
    names: list[str] | None = None,
    background: bool = True,
    maximized: bool = True,
    wrapper: str | None = None,
) -> dict:
    """Start several instances that can see each other's LAN games (each gets
    its own 127.0.0.N address and player name).

    The plugin forces each game's Winsock sockets onto its own loopback address
    (the useful part of ForceBindIP, built in and x86/x64), so no external tool
    is required.  A configured launch wrapper still overrides this.  The LAN
    relay then shows each instance's hosted game to the others.

    Then: host = p1 opens LAN → Create Game, the others open LAN, select the
    game, Join.  ``count`` is 2..12; ``map_path`` is passed to p1 (the host)
    only and is usually None because LAN games are created from the LAN screen.
    """
    try:
        result = game.launch_lan(
            count=count,
            exe=exe,
            map_path=map_path,
            names=names,
            background=background,
            maximized=maximized,
            wrapper=wrapper,
        )
        return _with_state(result)
    except Exception as exc:
        return _error(
            exc,
            "count must be 2..12 and WC3_EXE/the exe= path must point at the game. "
            "The built-in bind hook needs a 32-bit classic or 64-bit Reforged build.",
        )


@mcp.tool()
def wc3_set_launch_wrapper(template: str | None = None) -> dict:
    """Optionally override the built-in LAN binding with an external wrapper.

    ``template`` is a command placed before the game's command line, with
    ``{ip}`` replaced by the instance's LAN address, e.g.
    ``"C:/Tools/ForceBindIP/ForceBindIP.exe {ip}"`` (backslashes work too).
    The tool must be installed by the user.  LAN launches use the plugin's
    built-in bind hook when no wrapper is configured; clear it with None/""
    to go back to the built-in hook.  Stored in
    %LOCALAPPDATA%/wc3-tester/config.json; the WC3_LAUNCH_WRAPPER environment
    variable overrides it.
    """
    try:
        from . import launcher

        return {"ok": True, **launcher.set_launch_wrapper(template),
                "effective": launcher.launch_wrapper()}
    except Exception as exc:
        return _error(
            exc,
            "Pass a command containing {ip}, or None to clear it and use the "
            "built-in bind hook.",
        )


@mcp.tool()
def wc3_attach(
    pid: int | None = None,
    title: str | None = None,
    instance: str | None = None,
) -> dict:
    """Attach to an already running Warcraft III window (by process id or title).

    ``instance`` is the registry id to use (default: the first free p1, p2, ...).
    """
    try:
        return _with_state(game.attach(pid=pid, title=title, instance=instance), instance)
    except Exception as exc:
        return _error(
            exc,
            "Start the game first, or pass the correct pid/title.",
            instance=instance,
        )


@mcp.tool()
def wc3_status(instance: str | None = None) -> dict:
    """Process/window status: state, exit code, hung, uptime, CPU/RAM, fatal dialog.

    ``instance``: Which game instance (e.g. "p2"); default = the current one,
    i.e. the last launched/attached/selected.
    """
    try:
        result = {"ok": True, **game.status(instance)}
        result["instance"] = result.get("instance") or _instance_name(instance)
        return result
    except Exception as exc:
        return _error(exc, instance=instance)


@mcp.tool()
def wc3_instances() -> dict:
    """List all registered game instances and their state.

    The entry with ``"current": true`` is the one tools act on when no
    ``instance`` argument is passed.
    """
    try:
        instances = game.list_instances()
        current = next(
            (item["instance"] for item in instances if item.get("current")), None
        )
        return {
            "ok": True,
            "instance": current,
            "current": current,
            "count": len(instances),
            "instances": instances,
            "lan_relay": lanrelay.status(),
        }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def wc3_select(instance: str) -> dict:
    """Make an instance current (the default target for later tool calls)."""
    try:
        return _with_state(game.select(instance), instance)
    except Exception as exc:
        return _error(
            exc,
            "Call wc3_instances to list valid instance ids.",
            instance=instance,
        )


@mcp.tool()
def wc3_kill(instance: str | None = None, all: bool = False) -> dict:
    """Terminate the game process tree and return the final status.

    ``instance``: Which game instance (e.g. "p2"); default = the current one,
    i.e. the last launched/attached/selected.  ``all=True`` kills every
    registered instance.

    Killing one instance during a multiplayer game simulates that player
    dropping/leaving; for a graceful leave drive the in-game menu instead
    (F10 → Quit/Leave).
    """
    try:
        if all:
            results = game.kill_all()
            return {
                "ok": True,
                "all": True,
                "instance": _instance_name(None),
                "count": len(results),
                "results": results,
            }
        return {"ok": True, **game.kill(instance)}
    except Exception as exc:
        return _error(exc, instance=instance)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


@mcp.tool()
def wc3_screenshot(
    method: str = "auto",
    max_width: int = 1280,
    restore_minimized: bool = False,
    instance: str | None = None,
) -> list:
    """Screenshot the game client area; returns an image plus capture metadata.

    Coordinates in this metadata are client pixels: client = image_px / scale.
    ``method`` is auto|printwindow|wgc|screen.  ``screen`` only works when the
    window is visible and uncovered.  ``instance``: Which game instance (e.g.
    "p2"); default = the current one.
    """
    try:
        hwnd = _target_hwnd(instance)
        png, info = capture.screenshot_png(
            hwnd,
            method=method,
            max_width=max_width,
            restore_minimized=restore_minimized,
            instance=instance,
        )
        info = dict(info)
        info["ok"] = True
        info["instance"] = _instance_name(instance)
        info["game_state"] = _game_state(instance)
        return [Image(data=png, format="png"), json.dumps(info, default=str)]
    except Exception as exc:
        return [
            json.dumps(
                _error(
                    exc,
                    "Try method='printwindow' or 'screen'.",
                    instance=instance,
                )
            )
        ]


@mcp.tool()
def wc3_crash_report(
    since_iso: str | None = None, instance: str | None = None
) -> dict:
    """Crash report: exit code, fatal dialog, WC3 error files, WER, event log.

    ``instance``: Which game instance (e.g. "p2"); default = the current one,
    i.e. the last launched/attached/selected.
    """
    try:
        since: datetime | None = None
        if since_iso:
            try:
                since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            except Exception:
                since = None
        report = crash.crash_report(since=since, instance=instance)
        report["ok"] = True
        report["instance"] = report.get("instance") or _instance_name(instance)
        return report
    except Exception as exc:
        return _error(exc, instance=instance)


@mcp.tool()
def wc3_analyze_dump(path: str, instance: str | None = None) -> dict:
    """Analyse a .dmp with cdb.exe (if installed) or the minidump package.

    ``instance``: Which game instance (e.g. "p2"); default = the current one.
    """
    try:
        result = crash.analyze_dump(path)
        result["ok"] = "error" not in result
        result["instance"] = _instance_name(instance)
        return result
    except Exception as exc:
        return _error(
            exc,
            "Pass a .dmp path from wc3_crash_report/new_files.",
            instance=instance,
        )


@mcp.tool()
def wc3_read_logs(
    max_files: int = 5,
    max_lines: int = 200,
    instance: str | None = None,
) -> dict:
    """Read the newest WC3 error/log files (tail of each file).

    ``instance``: Which game instance (e.g. "p2"); default = the current one.
    """
    try:
        max_files = max(1, min(int(max_files), 50))
        max_lines = max(1, min(int(max_lines), 5000))
        session = game.get_session(instance)
        name = instance or (session.instance if session else None)
        exe = session.exe if session else None
        since_ts = (
            session.start_time.timestamp()
            if session and session.start_time
            else time.time() - 24 * 3600
        )
        dirs = crash.error_dirs(exe)
        candidates: list[Path] = []

        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except Exception:
                return 0.0

        for directory in dirs:
            base = Path(directory)
            if not base.is_dir():
                continue
            try:
                for path in base.rglob("*"):
                    if (
                        path.is_file()
                        and path.suffix.lower() in (".txt", ".log", ".mdmp")
                        and _mtime(path) >= since_ts - 2
                    ):
                        candidates.append(path)
            except Exception:
                continue
        candidates.sort(key=_mtime, reverse=True)
        files: list[dict] = []
        for path in candidates[:max_files]:
            try:
                text = crash._read_text_file(path)
                lines = text.splitlines()
                truncated = len(lines) > max_lines
                files.append(
                    {
                        "path": str(path),
                        "mtime": datetime.fromtimestamp(
                            _mtime(path), tz=timezone.utc
                        ).isoformat(),
                        "size": path.stat().st_size,
                        "line_count": len(lines),
                        "truncated": truncated,
                        "lines": lines[-max_lines:],
                    }
                )
            except Exception as exc:
                files.append({"path": str(path), "error": str(exc)})
        return {"ok": True, "instance": name, "dirs": dirs, "files": files}
    except Exception as exc:
        return _error(exc, instance=instance)


@mcp.tool()
def wc3_wait(
    seconds: float = 5.0,
    until: str | None = None,
    instance: str | None = None,
) -> dict:
    """Wait, optionally until a condition: window|exit|responsive (max 600 s).

    ``instance``: Which game instance (e.g. "p2"); default = the current one.
    """
    try:
        total = max(0.0, min(float(seconds), 600.0))
        until_l = (until or "").strip().lower() or None
        if until_l not in (None, "window", "exit", "responsive"):
            raise ValueError("until must be null, 'window', 'exit' or 'responsive'")
        session = game.get_session(instance)

        started = time.time()
        met = False
        state: dict = {}
        while True:
            state = game.status(instance)
            if until_l is None:
                met = False
            elif until_l == "window":
                met = _window_present(session)
            elif until_l == "exit":
                met = state.get("state") in ("exited", "crashed", "not_started")
            elif until_l == "responsive":
                met = bool(state.get("running")) and not state.get("hung")
            if met or (time.time() - started) >= total:
                break
            time.sleep(0.5)
        return {
            "ok": True,
            "instance": instance or state.get("instance"),
            "until": until_l,
            "until_met": met,
            "waited_sec": round(time.time() - started, 2),
            "status": state,
        }
    except Exception as exc:
        return _error(
            exc,
            "until is one of window, exit, responsive (or null).",
            instance=instance,
        )


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _input_result(fn, *args, instance: str | None = None, **kwargs) -> dict:
    try:
        hwnd = _target_hwnd(instance)
        info = fn(hwnd, *args, **kwargs)
        return _with_state(info, instance)
    except Exception as exc:
        return _error(exc, INPUT_HINT, instance=instance)


@mcp.tool()
def wc3_click(
    x: float,
    y: float,
    button: str = "left",
    clicks: int = 1,
    modifiers: list[str] | None = None,
    normalized: bool = False,
    mode: str | None = None,
    instance: str | None = None,
) -> dict:
    """Click at client coordinates (or normalized 0..1 when normalized=True).

    ``instance``: Which game instance (e.g. "p2"); default = the current one.
    """
    return _input_result(
        inputs.click,
        x,
        y,
        button=button,
        clicks=clicks,
        modifiers=modifiers,
        normalized=normalized,
        mode=mode,
        instance=instance,
    )


@mcp.tool()
def wc3_mouse_move(
    x: float,
    y: float,
    normalized: bool = False,
    mode: str | None = None,
    instance: str | None = None,
) -> dict:
    """Move the mouse to client coordinates.

    ``instance``: Which game instance (e.g. "p2"); default = the current one.
    """
    return _input_result(
        inputs.mouse_move,
        x,
        y,
        normalized=normalized,
        mode=mode,
        instance=instance,
    )


@mcp.tool()
def wc3_drag(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    button: str = "left",
    steps: int = 10,
    normalized: bool = False,
    mode: str | None = None,
    instance: str | None = None,
) -> dict:
    """Drag from (x1,y1) to (x2,y2) in client coordinates (box-select units).

    ``instance``: Which game instance (e.g. "p2"); default = the current one.
    """
    return _input_result(
        inputs.drag,
        x1,
        y1,
        x2,
        y2,
        button=button,
        steps=steps,
        normalized=normalized,
        mode=mode,
        instance=instance,
    )


@mcp.tool()
def wc3_scroll(
    x: float,
    y: float,
    delta: int,
    mode: str | None = None,
    instance: str | None = None,
) -> dict:
    """Scroll the wheel at client coordinates. delta is in notches (1 = one
    wheel click, 120 units); positive scrolls up / zooms in.

    ``instance``: Which game instance (e.g. "p2"); default = the current one.
    """
    return _input_result(inputs.scroll, x, y, delta=delta, mode=mode, instance=instance)


@mcp.tool()
def wc3_key(
    keys: str,
    hold_ms: int = 40,
    repeat: int = 1,
    mode: str | None = None,
    send_char: bool = False,
    instance: str | None = None,
) -> dict:
    """Press a key or chord: "a", "f1", "ctrl+1", "shift+f2", "alt+q".
    Use wc3_type/wc3_chat for text. send_char=True also posts WM_CHAR in
    background mode, only for a game build that ignores typed keys otherwise.

    ``instance``: Which game instance (e.g. "p2"); default = the current one.
    """
    return _input_result(
        inputs.key,
        keys,
        hold_ms=hold_ms,
        repeat=repeat,
        mode=mode,
        send_char=send_char,
        instance=instance,
    )


@mcp.tool()
def wc3_type(
    text: str, mode: str | None = None, instance: str | None = None
) -> dict:
    """Type text into the game (WM_CHAR in background mode).

    ``instance``: Which game instance (e.g. "p2"); default = the current one.
    """
    return _input_result(inputs.type_text, text, mode=mode, instance=instance)


@mcp.tool()
def wc3_chat(
    text: str,
    to_all: bool = False,
    mode: str | None = None,
    instance: str | None = None,
) -> dict:
    """Open chat, type text and press Enter; to_all uses Shift+Enter.

    ``instance``: Which game instance (e.g. "p2"); default = the current one.
    """
    return _input_result(
        inputs.send_chat,
        text,
        to_all=to_all,
        mode=mode,
        instance=instance,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        winapi.enable_dpi_awareness()
    except Exception:
        pass
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="[wc3-tester] %(levelname)s %(name)s: %(message)s",
    )
    log.info("starting wc3-tester MCP server on stdio")
    mcp.run()


if __name__ == "__main__":
    main()
