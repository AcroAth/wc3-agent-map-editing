"""MCP stdio server exposing Warcraft III testing tools.

Run with ``python -m wc3_tester.server`` (the launcher script does this inside
a local virtualenv).  stdout belongs to the MCP protocol: log only to stderr.
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

from . import capture, crash, game, inputs, winapi, window

log = logging.getLogger("wc3_tester")

mcp = FastMCP("wc3")

INPUT_HINT = (
    "Check that the game is running and its window exists (wc3_status). "
    "If background input has no effect, retry with mode='foreground'."
)


def _error(exc: BaseException, hint: str = "") -> dict:
    log.exception("tool error: %s", exc)
    result: dict[str, Any] = {"ok": False, "error": str(exc)}
    if hint:
        result["hint"] = hint
    return result


def _target_hwnd() -> int:
    """The game window, attaching automatically when it is discoverable."""
    session = game.get_session()
    if session and session.hwnd and window.is_window(session.hwnd):
        return int(session.hwnd)
    found = window.find_window()
    if not found:
        raise RuntimeError(
            "No Warcraft III window found. Call wc3_launch (or wc3_attach) first."
        )
    pid = window.window_pid(found)
    if not session or session.hwnd != found:
        try:
            game.attach(pid=pid)
        except Exception:
            # Attaching is a convenience; input can still be posted to the hwnd.
            pass
    return int(found)


def _game_state() -> dict:
    """Compact lifecycle snapshot included with every input tool result."""
    try:
        state = game.status()
    except Exception as exc:
        return {"state": "unknown", "error": str(exc)}
    compact = {
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
    if state.get("fatal_dialog"):
        compact["fatal_dialog"] = state["fatal_dialog"]
    return compact


def _with_state(result: dict) -> dict:
    result = dict(result)
    result.setdefault("ok", True)
    result["game_state"] = _game_state()
    return result


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
) -> dict:
    """Launch Warcraft III, optionally loading a map, and wait for its window.

    ``exe`` defaults to the WC3_EXE environment variable, then common install
    paths.  ``flavor`` is auto|classic|reforged.  ``nowfpause`` keeps the game
    running while it is not focused (essential for background testing).
    """
    try:
        info = game.launch(
            exe=exe,
            map_path=map_path,
            windowed=windowed,
            nowfpause=nowfpause,
            extra_args=extra_args,
            flavor=flavor,
        )
        return _with_state(info)
    except Exception as exc:
        return _error(
            exc,
            "Set WC3_EXE to the game executable, or pass exe=. "
            "Classic: ...\\war3.exe, Reforged: ...\\_retail_\\x86_64\\Warcraft III.exe",
        )


@mcp.tool()
def wc3_attach(pid: int | None = None, title: str | None = None) -> dict:
    """Attach to an already running Warcraft III window (by process id or title)."""
    try:
        return _with_state(game.attach(pid=pid, title=title))
    except Exception as exc:
        return _error(exc, "Start the game first, or pass the correct pid/title.")


@mcp.tool()
def wc3_status() -> dict:
    """Process/window status: state, exit code, hung, uptime, CPU/RAM, fatal dialog."""
    try:
        return {"ok": True, **game.status()}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def wc3_kill() -> dict:
    """Terminate the game process tree and return the final status."""
    try:
        return {"ok": True, **game.kill()}
    except Exception as exc:
        return _error(exc)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


@mcp.tool()
def wc3_screenshot(
    method: str = "auto",
    max_width: int = 1280,
    restore_minimized: bool = False,
) -> list:
    """Screenshot the game client area; returns an image plus capture metadata.

    Coordinates in this metadata are client pixels: client = image_px / scale.
    ``method`` is auto|printwindow|wgc|screen.  ``screen`` only works when the
    window is visible and uncovered.
    """
    try:
        hwnd = _target_hwnd()
        png, info = capture.screenshot_png(
            hwnd,
            method=method,
            max_width=max_width,
            restore_minimized=restore_minimized,
        )
        info = dict(info)
        info["ok"] = True
        info["game_state"] = _game_state()
        return [Image(data=png, format="png"), json.dumps(info, default=str)]
    except Exception as exc:
        return [json.dumps(_error(exc, "Try method='printwindow' or 'screen'."))]


@mcp.tool()
def wc3_crash_report(since_iso: str | None = None) -> dict:
    """Crash report: exit code, fatal dialog, WC3 error files, WER, event log."""
    try:
        since: datetime | None = None
        if since_iso:
            try:
                since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            except Exception:
                since = None
        report = crash.crash_report(since=since)
        report["ok"] = True
        return report
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def wc3_analyze_dump(path: str) -> dict:
    """Analyse a .dmp with cdb.exe (if installed) or the minidump package."""
    try:
        result = crash.analyze_dump(path)
        result["ok"] = "error" not in result
        return result
    except Exception as exc:
        return _error(exc, "Pass a .dmp path from wc3_crash_report/new_files.")


@mcp.tool()
def wc3_read_logs(max_files: int = 5, max_lines: int = 200) -> dict:
    """Read the newest WC3 error/log files (tail of each file)."""
    try:
        max_files = max(1, min(int(max_files), 50))
        max_lines = max(1, min(int(max_lines), 5000))
        session = game.get_session()
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
        return {"ok": True, "dirs": dirs, "files": files}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def wc3_wait(seconds: float = 5.0, until: str | None = None) -> dict:
    """Wait, optionally until a condition: window|exit|responsive (max 600 s)."""
    try:
        total = max(0.0, min(float(seconds), 600.0))
        until_l = (until or "").strip().lower() or None
        if until_l not in (None, "window", "exit", "responsive"):
            raise ValueError("until must be null, 'window', 'exit' or 'responsive'")

        started = time.time()
        met = False
        state: dict = {}
        while True:
            state = game.status()
            if until_l is None:
                met = False
            elif until_l == "window":
                met = bool(window.find_window())
            elif until_l == "exit":
                met = state.get("state") in ("exited", "crashed", "not_started")
            elif until_l == "responsive":
                met = bool(state.get("running")) and not state.get("hung")
            if met or (time.time() - started) >= total:
                break
            time.sleep(0.5)
        return {
            "ok": True,
            "until": until_l,
            "until_met": met,
            "waited_sec": round(time.time() - started, 2),
            "status": state,
        }
    except Exception as exc:
        return _error(exc, "until is one of window, exit, responsive (or null).")


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _input_result(fn, *args, **kwargs) -> dict:
    try:
        hwnd = _target_hwnd()
        info = fn(hwnd, *args, **kwargs)
        return _with_state(info)
    except Exception as exc:
        return _error(exc, INPUT_HINT)


@mcp.tool()
def wc3_click(
    x: float,
    y: float,
    button: str = "left",
    clicks: int = 1,
    modifiers: list[str] | None = None,
    normalized: bool = False,
    mode: str | None = None,
) -> dict:
    """Click at client coordinates (or normalized 0..1 when normalized=True)."""
    return _input_result(
        inputs.click,
        x,
        y,
        button=button,
        clicks=clicks,
        modifiers=modifiers,
        normalized=normalized,
        mode=mode,
    )


@mcp.tool()
def wc3_mouse_move(
    x: float,
    y: float,
    normalized: bool = False,
    mode: str | None = None,
) -> dict:
    """Move the mouse to client coordinates."""
    return _input_result(
        inputs.mouse_move, x, y, normalized=normalized, mode=mode
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
) -> dict:
    """Drag from (x1,y1) to (x2,y2) in client coordinates (box-select units)."""
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
    )


@mcp.tool()
def wc3_scroll(
    x: float,
    y: float,
    delta: int,
    mode: str | None = None,
) -> dict:
    """Scroll the wheel at client coordinates. delta is in notches (1 = one
    wheel click, 120 units); positive scrolls up / zooms in."""
    return _input_result(inputs.scroll, x, y, delta=delta, mode=mode)


@mcp.tool()
def wc3_key(
    keys: str,
    hold_ms: int = 40,
    repeat: int = 1,
    mode: str | None = None,
    send_char: bool = False,
) -> dict:
    """Press a key or chord: "a", "f1", "ctrl+1", "shift+f2", "alt+q".
    Use wc3_type/wc3_chat for text. send_char=True also posts WM_CHAR in
    background mode, only for a game build that ignores typed keys otherwise."""
    return _input_result(
        inputs.key, keys, hold_ms=hold_ms, repeat=repeat, mode=mode,
        send_char=send_char,
    )


@mcp.tool()
def wc3_type(text: str, mode: str | None = None) -> dict:
    """Type text into the game (WM_CHAR in background mode)."""
    return _input_result(inputs.type_text, text, mode=mode)


@mcp.tool()
def wc3_chat(text: str, to_all: bool = False, mode: str | None = None) -> dict:
    """Open chat, type text and press Enter; to_all uses Shift+Enter."""
    return _input_result(inputs.send_chat, text, to_all=to_all, mode=mode)


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
