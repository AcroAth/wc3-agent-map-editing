"""Finding and inspecting the Warcraft III window.

Windows are always identified by their top-level ``HWND`` (an ``int`` in
Python).  All coordinates exposed by this package are *client area* pixels of
that window (the same space a screenshot takes with ``scale == 1``), unless a
function explicitly says "screen".
"""

from __future__ import annotations

import logging
from . import winapi as w

log = logging.getLogger(__name__)

# Process names we treat as "the game".  The Reforged launcher is deliberately
# excluded: it is a small wrapper that exits after spawning the real game.
GAME_PROCESS_NAMES = {
    "war3.exe",
    "warcraft iii.exe",
    "frozen throne.exe",
}
EXCLUDED_PROCESS_NAMES = {
    "warcraft iii launcher.exe",
}
DEFAULT_CLASS = "Warcraft III"


def process_name_of(pid: int) -> str:
    """Best-effort process executable name (lower-case, e.g. ``war3.exe``).

    Not cached by PID: Windows reuses PIDs, and the shared server runs for
    hours.
    """
    if not pid:
        return ""
    try:
        import psutil

        return (psutil.Process(pid).name() or "").lower()
    except Exception:
        return ""


def window_title(hwnd: int) -> str:
    # For another process's window GetWindowText returns the stored caption
    # without sending it a message, so a busy or hung game cannot stall us.
    return w.get_window_text(hwnd)


def window_class(hwnd: int) -> str:
    return w.get_class_name(hwnd)


def window_pid(hwnd: int) -> int:
    return w.window_pid(hwnd)


def is_window(hwnd: int | None) -> bool:
    return w.is_window(hwnd)


def is_visible(hwnd: int) -> bool:
    return w.is_window_visible(hwnd)


def is_minimized(hwnd: int) -> bool:
    return w.is_iconic(hwnd)


def list_windows(visible_only: bool = True) -> list[dict]:
    """Snapshot of all top-level windows with pid/title/class.

    Useful for diagnostics and for the "a new WC3 window appeared" fallback
    when Reforged re-spawns the real game process.
    """
    result: list[dict] = []
    for hwnd in w.enum_windows():
        try:
            if visible_only and not w.is_window_visible(hwnd):
                continue
            pid = w.window_pid(hwnd)
            result.append(
                {
                    "hwnd": hwnd,
                    "pid": pid,
                    "title": window_title(hwnd),
                    "class_name": w.get_class_name(hwnd),
                    "process": process_name_of(pid),
                }
            )
        except Exception:
            continue
    return result


def _matches_default(hwnd: int, pid: int) -> bool:
    pname = process_name_of(pid)
    if pname in EXCLUDED_PROCESS_NAMES:
        return False
    cls = w.get_class_name(hwnd)
    if cls.lower() == DEFAULT_CLASS.lower():
        return True
    return pname in GAME_PROCESS_NAMES


def find_window(
    pid: int | None = None,
    title: str | None = None,
    class_name: str | None = None,
    process_name: str | None = None,
) -> int | None:
    """Find a visible top-level Warcraft III window.

    With no criteria, a window whose class is ``Warcraft III`` *or* whose
    process is one of the known game executables (launcher excluded) matches.
    ``title`` is matched case-insensitively, exact first then substring.
    """
    if title is not None:
        title_l = title.strip().lower()
    else:
        title_l = None
    cls_l = class_name.lower() if class_name else None
    pname_l = process_name.lower() if process_name else None

    exact_title: int | None = None
    partial_title: int | None = None
    fallback: int | None = None

    for hwnd in w.enum_windows():
        try:
            if not w.is_window_visible(hwnd):
                continue
            wpid = w.window_pid(hwnd)
            if pid is not None and wpid != int(pid):
                continue
            wtitle = window_title(hwnd)
            wcls = w.get_class_name(hwnd)
            if process_name is not None:
                if process_name_of(wpid) != pname_l:
                    continue
            if class_name is not None and wcls.lower() != cls_l:
                continue
            if title_l is not None:
                if wtitle.strip().lower() == title_l:
                    exact_title = exact_title or hwnd
                    continue
                if title_l in wtitle.lower():
                    partial_title = partial_title or hwnd
                    continue
                continue
            if class_name is not None or process_name is not None or pid is not None:
                fallback = fallback or hwnd
                continue
            # No criteria at all: use the package default heuristic.
            if _matches_default(hwnd, wpid):
                return hwnd
        except Exception:
            continue

    return exact_title or partial_title or fallback


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    return w.get_client_rect(hwnd)


def client_size(hwnd: int) -> tuple[int, int]:
    """Client area size in physical pixels."""
    left, top, right, bottom = w.get_client_rect(hwnd)
    return max(0, right - left), max(0, bottom - top)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    return w.get_window_rect(hwnd)


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    return w.client_to_screen(hwnd, x, y)


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    return w.screen_to_client(hwnd, x, y)


def is_hung(hwnd: int) -> bool:
    """True when Windows considers the window unresponsive."""
    try:
        if w.bool_at(w.user32.IsHungAppWindow(hwnd)):
            return True
    except Exception:
        pass
    # WM_NULL with SMTO_ABORTIFHUNG: a hung window does not answer in time.
    ok, _ = w.send_message_timeout(
        hwnd, w.WM_NULL, flags=w.SMTO_ABORTIFHUNG | w.SMTO_BLOCK, timeout_ms=2000
    )
    return not ok


def is_responsive(hwnd: int) -> bool:
    return not is_hung(hwnd)


def describe(hwnd: int) -> dict:
    """A JSON-friendly description of a window (for diagnostics)."""
    left, top, right, bottom = w.get_window_rect(hwnd)
    cw, ch = client_size(hwnd)
    return {
        "hwnd": hwnd,
        "pid": w.window_pid(hwnd),
        "title": window_title(hwnd),
        "class_name": w.get_class_name(hwnd),
        "process": process_name_of(w.window_pid(hwnd)),
        "visible": w.is_window_visible(hwnd),
        "minimized": w.is_iconic(hwnd),
        "client_width": cw,
        "client_height": ch,
        "window_rect": [left, top, right, bottom],
    }
