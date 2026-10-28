"""Keyboard and mouse input for Warcraft III.

Two delivery modes are supported and selected per call:

``background``
    Post window messages (``PostMessageW``) directly to the game window.  The
    game never needs to be focused and may be covered by other windows.  This
    is the default because it is the least intrusive and keeps working while
    the agent does something else.  Some Reforged builds use raw input and
    ignore posted messages; switch to ``foreground`` in that case.

``foreground``
    Bring the window to the foreground (restoring it if minimised), inject
    events with ``SendInput``, then optionally restore the previously focused
    window.

All coordinates are *client area pixels* of the game window unless
``normalized=True`` is passed (0..1 of client width/height).
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import time
from contextlib import contextmanager

from . import winapi as w
from . import window

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Key tables
# ---------------------------------------------------------------------------

NAMED_KEYS: dict[str, int] = {
    "backspace": 0x08,
    "bksp": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "menu": 0x12,
    "pause": 0x13,
    "break": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "spacebar": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "apps": 0x5D,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "multiply": 0x6A,
    "add": 0x6B,
    "separator": 0x6C,
    "subtract": 0x6D,
    "decimal": 0x6E,
    "divide": 0x6F,
    "numpadenter": 0x0D,
    "numpad_enter": 0x0D,
    "lshift": 0xA0,
    "rshift": 0xA1,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "lalt": 0xA4,
    "ralt": 0xA5,
    "semicolon": 0xBA,
    "plus": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "slash": 0xBF,
    "tilde": 0xC0,
    "lbracket": 0xDB,
    "backslash": 0xDC,
    "rbracket": 0xDD,
    "quote": 0xDE,
}

MODIFIER_VK = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12, "win": 0x5B}

# Virtual keys that require the "extended key" bit.
EXTENDED_VKS = {
    0x21,  # page up
    0x22,  # page down
    0x23,  # end
    0x24,  # home
    0x25,  # left
    0x26,  # up
    0x27,  # right
    0x28,  # down
    0x2D,  # insert
    0x2E,  # delete
    0x5B,  # left windows
    0x5C,  # right windows
    0x5D,  # applications
    0x6F,  # numpad divide
    0xA3,  # right ctrl
    0xA5,  # right alt
}

MODIFIER_MK = {"shift": w.MK_SHIFT, "ctrl": w.MK_CONTROL}

CHAR_FOR_KEY = {
    "enter": 13,
    "numpadenter": 13,
    "numpad_enter": 13,
    "esc": 27,
    "escape": 27,
    "backspace": 8,
    "tab": 9,
}

BUTTONS = {
    "left": (w.WM_LBUTTONDOWN, w.WM_LBUTTONUP, w.WM_LBUTTONDBLCLK, w.MK_LBUTTON, w.MOUSEEVENTF_LEFTDOWN, w.MOUSEEVENTF_LEFTUP),
    "right": (w.WM_RBUTTONDOWN, w.WM_RBUTTONUP, w.WM_RBUTTONDBLCLK, w.MK_RBUTTON, w.MOUSEEVENTF_RIGHTDOWN, w.MOUSEEVENTF_RIGHTUP),
    "middle": (w.WM_MBUTTONDOWN, w.WM_MBUTTONUP, w.WM_MBUTTONDBLCLK, w.MK_MBUTTON, w.MOUSEEVENTF_MIDDLEDOWN, w.MOUSEEVENTF_MIDDLEUP),
}


# ---------------------------------------------------------------------------
# Small configuration helpers
# ---------------------------------------------------------------------------


def default_mode() -> str:
    mode = (os.environ.get("WC3_INPUT_MODE") or "background").strip().lower()
    return mode if mode in ("background", "foreground") else "background"


def _resolve_mode(mode: str | None) -> str:
    chosen = (mode or default_mode()).strip().lower()
    if chosen not in ("background", "foreground"):
        raise ValueError("mode must be 'background' or 'foreground'")
    return chosen


def _delay_seconds() -> float:
    try:
        ms = int(os.environ.get("WC3_INPUT_DELAY_MS", "30"))
    except Exception:
        ms = 30
    return max(0.0, min(ms, 2000)) / 1000.0


def _sleep() -> None:
    delay = _delay_seconds()
    if delay:
        time.sleep(delay)


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------


def resolve_vk(name: str) -> tuple[int, bool]:
    """Map a key name to (virtual-key code, extended?).

    Accepts named keys, ``f1``..``f12``, ``numpad0``..``numpad9`` and single
    printable characters.
    """
    key = (name or "").strip()
    if not key:
        raise ValueError("empty key name")
    low = key.lower()
    if low in NAMED_KEYS:
        return NAMED_KEYS[low], low in ("numpadenter", "numpad_enter")
    m = re.fullmatch(r"f([1-9]|1[0-2])", low)
    if m:
        return 0x70 + int(m.group(1)) - 1, False
    m = re.fullmatch(r"numpad([0-9])", low)
    if m:
        return 0x60 + int(m.group(1)), False
    if len(key) == 1:
        ch = key
        if ch.isalpha():
            return ord(ch.upper()), False
        if ch.isdigit():
            return ord(ch), False
        try:
            scan = int(w.user32.VkKeyScanW(ch))
        except Exception:
            scan = -1
        if scan != -1:
            vk = scan & 0xFF
            return vk, vk in EXTENDED_VKS
        return ord(ch) & 0xFF, False
    raise ValueError(f"Unknown key name: {name!r}")


def parse_combo(keys: str) -> tuple[list[str], str]:
    """Split ``"ctrl+shift+f2"`` into (["ctrl", "shift"], "f2")."""
    if not keys or not keys.strip():
        raise ValueError("keys must not be empty")
    parts = [p.strip() for p in keys.split("+") if p.strip()]
    if not parts:
        raise ValueError("keys must not be empty")
    modifiers: list[str] = []
    key_name: str | None = None
    for part in parts:
        low = part.lower()
        if low in ("control",):
            low = "ctrl"
        if low in MODIFIER_VK and key_name is None:
            modifiers.append(low)
        elif key_name is None:
            key_name = part
        else:
            raise ValueError(f"More than one non-modifier key in {keys!r}")
    if key_name is None:
        # "shift" alone means tap shift.
        key_name = modifiers.pop() if modifiers else ""
        if not key_name:
            raise ValueError(f"No key in {keys!r}")
    return modifiers, key_name


def _char_for_key(key_name: str, shift: bool = False) -> int | None:
    low = key_name.lower()
    if low in CHAR_FOR_KEY:
        return CHAR_FOR_KEY[low]
    if len(key_name) == 1:
        ch = key_name
        if shift:
            ch = ch.upper()
        return ord(ch) & 0xFFFF
    if low == "space" or low == "spacebar":
        return 32
    return None


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------


def to_client_xy(hwnd: int, x: float, y: float, normalized: bool = False) -> tuple[int, int]:
    """Convert user coordinates to integer client-area pixels."""
    cw, ch = window.client_size(hwnd)
    if cw <= 0 or ch <= 0:
        raise RuntimeError("window has no client area (is it minimised?)")
    if normalized:
        fx, fy = float(x), float(y)
        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            raise ValueError("normalized coordinates must be between 0 and 1")
        px, py = int(round(fx * (cw - 1))), int(round(fy * (ch - 1)))
    else:
        px, py = int(round(float(x))), int(round(float(y)))
    # WM_* messages transport coordinates as signed 16-bit values.
    px = max(0, min(px, 32767))
    py = max(0, min(py, 32767))
    return px, py


# ---------------------------------------------------------------------------
# Background (PostMessage) implementation
# ---------------------------------------------------------------------------


def _post(hwnd: int, msg: int, wparam: int, lparam: int, warnings: list[str]) -> bool:
    try:
        ok = w.bool_at(w.user32.PostMessageW(hwnd, msg, wparam, lparam))
    except Exception as exc:  # pragma: no cover - Windows only
        warnings.append(f"PostMessageW(0x{msg:X}) failed: {exc}")
        return False
    if not ok:
        warnings.append(
            f"PostMessageW(0x{msg:X}) failed (error {w.last_error()}); "
            "the game may be elevated or the window destroyed"
        )
    return ok


def _bg_fake_focus(hwnd: int, warnings: list[str]) -> None:
    _post(hwnd, w.WM_ACTIVATE, w.WA_ACTIVE, 0, warnings)
    _post(hwnd, w.WM_SETFOCUS, 0, 0, warnings)


def _bg_key_event(
    hwnd: int,
    vk: int,
    down: bool,
    extended: bool | None = None,
    sys_key: bool = False,
    warnings: list[str] | None = None,
) -> None:
    warnings = warnings if warnings is not None else []
    try:
        scan = int(w.user32.MapVirtualKeyW(vk, w.MAPVK_VK_TO_VSC)) & 0xFF
    except Exception:
        scan = 0
    ext = (vk in EXTENDED_VKS) if extended is None else extended
    lparam = 1 | (scan << 16)
    if ext:
        lparam |= 1 << 24
    if down:
        msg = w.WM_SYSKEYDOWN if sys_key else w.WM_KEYDOWN
    else:
        # previous-state (bit 30) and transition (bit 31) set on key up
        lparam |= 0x40000000 | 0xC0000000
        msg = w.WM_SYSKEYUP if sys_key else w.WM_KEYUP
    _post(hwnd, msg, vk, lparam, warnings)


def _bg_char(hwnd: int, char: int, warnings: list[str]) -> None:
    _post(hwnd, w.WM_CHAR, char & 0xFFFF, 1, warnings)
    _sleep()


def _bg_key(
    hwnd: int,
    keys: str,
    hold_ms: int = 40,
    repeat: int = 1,
    fake_focus: bool = True,
    warnings: list[str] | None = None,
    send_char: bool = False,
) -> dict:
    """Post a key press. A window whose message loop calls TranslateMessage
    (most games, WC3 included) makes its own WM_CHAR from WM_KEYDOWN, so an
    explicit WM_CHAR is only posted with ``send_char=True``; otherwise chat
    text would arrive doubled."""
    warnings = warnings if warnings is not None else []
    modifiers, key_name = parse_combo(keys)
    vk, ext = resolve_vk(key_name)
    if fake_focus:
        _bg_fake_focus(hwnd, warnings)

    alt_held = "alt" in modifiers or vk == 0x12
    sys_key = alt_held or vk == 0x79  # F10 opens the menu even without Alt
    pressed: list[int] = []
    for mod in modifiers:
        mod_vk = MODIFIER_VK[mod]
        _bg_key_event(hwnd, mod_vk, True, sys_key=(mod == "alt"), warnings=warnings)
        pressed.append(mod_vk)
        _sleep()

    shift = "shift" in modifiers
    char = _char_for_key(key_name, shift=shift) if send_char else None
    if char is not None and ("ctrl" in modifiers or "alt" in modifiers):
        char = None  # ctrl/alt chords are commands, not text

    for _ in range(max(1, int(repeat))):
        _bg_key_event(hwnd, vk, True, extended=ext, sys_key=sys_key, warnings=warnings)
        _sleep()
        if char is not None:
            _bg_char(hwnd, char, warnings)
        if hold_ms > 0:
            time.sleep(min(hold_ms, 5000) / 1000.0)
        _bg_key_event(hwnd, vk, False, extended=ext, sys_key=sys_key, warnings=warnings)
        _sleep()

    for mod_vk in reversed(pressed):
        _bg_key_event(hwnd, mod_vk, False, sys_key=(mod_vk == 0x12), warnings=warnings)
        _sleep()

    return {
        "mode": "background",
        "keys": keys,
        "key": key_name,
        "vk": vk,
        "modifiers": modifiers,
        "repeat": max(1, int(repeat)),
        "warnings": warnings,
    }


_CWP_SKIP = 0x0001 | 0x0002 | 0x0004  # CWP_SKIPINVISIBLE|SKIPDISABLED|SKIPTRANSPARENT


def _mouse_target(hwnd: int, x: int, y: int) -> tuple[int, int, int]:
    """Deepest child window under client point (x, y) and the point in its
    client space. Posted mouse messages are not re-routed by Windows, so a
    game that renders into a child window would otherwise never see them."""
    target, cx, cy = hwnd, x, y
    try:
        fn = w.user32.ChildWindowFromPointEx
        fn.argtypes = [w.HWND, w.POINT, w.UINT]
        fn.restype = w.HWND
        for _ in range(16):
            child = fn(target, w.POINT(cx, cy), _CWP_SKIP)
            child = int(child or 0)
            if not child or child == target:
                break
            sx, sy = w.client_to_screen(target, cx, cy)
            cx, cy = w.screen_to_client(child, sx, sy)
            target = child
    except Exception:
        return hwnd, x, y
    return target, cx, cy


def _has_dblclks(hwnd: int) -> bool:
    try:
        fn = w.user32.GetClassLongPtrW
        fn.argtypes = [w.HWND, ctypes.c_int]
        fn.restype = ctypes.c_size_t
        return bool(fn(hwnd, -26) & 0x0008)  # GCL_STYLE & CS_DBLCLKS
    except Exception:
        return True


def _frame_wait() -> None:
    # Give the game one or two frames to read the new cursor position.
    try:
        ms = int(os.environ.get("WC3_FRAME_MS", "50"))
    except ValueError:
        ms = 50
    time.sleep(max(0, ms) / 1000.0)


def _set_virtual_cursor(hwnd: int, x: int, y: int) -> bool:
    """Point the game's GetCursorPos at client (x, y) when the game reads the
    real cursor instead of message coordinates (classic WC3). Returns True when
    a virtual cursor is in use for this window."""
    from . import cursorhook

    vc = cursorhook.for_pid(w.window_pid(hwnd))
    if vc is None:
        return False
    sx, sy = window.client_to_screen(hwnd, x, y)
    vc.set(sx, sy)
    return True


def _bg_mouse_move(
    hwnd: int, x: int, y: int, warnings: list[str], keystate: int = 0
) -> None:
    if _set_virtual_cursor(hwnd, x, y):
        _frame_wait()
    target, tx, ty = _mouse_target(hwnd, x, y)
    _post(target, w.WM_MOUSEMOVE, keystate, w.make_lparam(tx, ty), warnings)
    _sleep()


def _bg_button(
    hwnd: int,
    button: str,
    x: int,
    y: int,
    down: bool,
    keystate: int,
    dblclk: bool,
    warnings: list[str],
) -> None:
    down_msg, up_msg, dbl_msg, mk_button, _, _ = BUTTONS[button]
    if _set_virtual_cursor(hwnd, x, y) and not down:
        _frame_wait()  # let the game see the press before the release
    target, tx, ty = _mouse_target(hwnd, x, y)
    lparam = w.make_lparam(tx, ty)
    if down:
        msg = dbl_msg if dblclk else down_msg
        _post(target, msg, keystate | mk_button, lparam, warnings)
    else:
        _post(target, up_msg, keystate, lparam, warnings)
    _sleep()


def _bg_click(
    hwnd: int,
    x: int,
    y: int,
    button: str,
    clicks: int,
    modifiers: list[str],
    fake_focus: bool,
    delay_ms: int | None,
) -> dict:
    warnings: list[str] = []
    modifiers = [m.lower() for m in modifiers]
    if fake_focus:
        _bg_fake_focus(hwnd, warnings)

    keystate = 0
    for mod in modifiers:
        keystate |= MODIFIER_MK.get(mod, 0)
        if mod in MODIFIER_VK:
            _bg_key_event(hwnd, MODIFIER_VK[mod], True, sys_key=(mod == "alt"), warnings=warnings)
            _sleep()

    _bg_mouse_move(hwnd, x, y, warnings)
    clicks = max(1, int(clicks))
    if button == "left" and clicks >= 2:
        # Windows convention for a double click: down, up, dblclk(down), up.
        # WM_LBUTTONDBLCLK only exists for CS_DBLCLKS classes; others (WC3)
        # detect double clicks from two plain presses, as Windows sends them.
        dbl = _has_dblclks(hwnd)
        _bg_button(hwnd, button, x, y, True, keystate, False, warnings)
        _bg_button(hwnd, button, x, y, False, keystate, False, warnings)
        for _ in range(clicks - 1):
            _bg_button(hwnd, button, x, y, True, keystate, dbl, warnings)
            _bg_button(hwnd, button, x, y, False, keystate, False, warnings)
    else:
        for _ in range(clicks):
            _bg_button(hwnd, button, x, y, True, keystate, False, warnings)
            _bg_button(hwnd, button, x, y, False, keystate, False, warnings)
        if delay_ms:
            time.sleep(max(0, int(delay_ms)) / 1000.0)

    for mod in reversed(modifiers):
        if mod in MODIFIER_VK:
            _bg_key_event(hwnd, MODIFIER_VK[mod], False, sys_key=(mod == "alt"), warnings=warnings)
            _sleep()

    return {
        "mode": "background",
        "virtual_cursor": _set_virtual_cursor(hwnd, x, y),
        "x": x,
        "y": y,
        "button": button,
        "clicks": clicks,
        "modifiers": modifiers,
        "warnings": warnings,
    }


def _bg_scroll(hwnd: int, x: int, y: int, delta: int) -> dict:
    warnings: list[str] = []
    _bg_fake_focus(hwnd, warnings)
    sx, sy = window.client_to_screen(hwnd, x, y)
    # WM_MOUSEWHEEL carries screen coordinates and a packed keystate/delta.
    wheel_delta = int(delta) * 120
    wheel_delta = max(-0x7FFF, min(0x7FFF, wheel_delta))
    wparam = w.make_word(0, wheel_delta & 0xFFFF)
    lparam = w.make_lparam(sx, sy)
    _bg_mouse_move(hwnd, x, y, warnings)
    _post(hwnd, w.WM_MOUSEWHEEL, wparam, lparam, warnings)
    return {"mode": "background", "x": x, "y": y, "delta": delta, "warnings": warnings}


def _bg_type_text(hwnd: int, text: str, fake_focus: bool) -> dict:
    warnings: list[str] = []
    if fake_focus:
        _bg_fake_focus(hwnd, warnings)
    encoded = text.replace("\n", "\r").encode("utf-16-le", "surrogatepass")
    count = 0
    for i in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[i : i + 2], "little")
        _bg_char(hwnd, unit, warnings)
        count += 1
    return {"mode": "background", "chars": count, "warnings": warnings}


# ---------------------------------------------------------------------------
# Foreground (SendInput) implementation
# ---------------------------------------------------------------------------


def _activate_window(hwnd: int, retries: int = 3) -> bool:
    """Bring *hwnd* to the foreground; returns True when verified."""
    if not w.is_window(hwnd):
        return False
    try:
        if w.is_iconic(hwnd):
            w.user32.ShowWindow(hwnd, w.SW_RESTORE)
            time.sleep(0.15)
    except Exception:
        pass

    for attempt in range(max(1, retries)):
        if w.get_foreground_window() == hwnd:
            return True
        cur_tid = 0
        target_tid = 0
        attached = False
        try:
            cur_tid = int(w.kernel32.GetCurrentThreadId())
            target_tid = w.window_thread_id(hwnd)
            if cur_tid and target_tid and cur_tid != target_tid:
                attached = w.bool_at(
                    w.user32.AttachThreadInput(cur_tid, target_tid, True)
                )
            w.user32.BringWindowToTop(hwnd)
            w.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        finally:
            if attached:
                try:
                    w.user32.AttachThreadInput(cur_tid, target_tid, False)
                except Exception:
                    pass
        time.sleep(0.1 + 0.05 * attempt)
        if w.get_foreground_window() == hwnd:
            return True
    return w.get_foreground_window() == hwnd


@contextmanager
def _foreground_guard(hwnd: int, restore_focus: bool = True):
    from . import cursorhook

    cursorhook.release_all()  # real SendInput moves the real cursor; follow it
    previous = w.get_foreground_window()
    activated = _activate_window(hwnd)
    try:
        yield activated
    finally:
        if restore_focus and previous and previous != hwnd and w.is_window(previous):
            try:
                w.user32.SetForegroundWindow(previous)
            except Exception:
                pass


def _fg_send(events: list[w.INPUT]) -> int:
    sent = w.send_inputs(events)
    if sent != len(events):
        raise RuntimeError(
            f"SendInput accepted {sent}/{len(events)} events (error {w.last_error()})"
        )
    return sent


def _fg_key_vk(vk: int, down: bool, expanded: bool = False) -> None:
    ext = vk in EXTENDED_VKS
    scan = 0
    try:
        scan = int(w.user32.MapVirtualKeyW(vk, w.MAPVK_VK_TO_VSC)) & 0xFF
    except Exception:
        scan = 0
    flags = 0
    if scan:
        flags |= w.KEYEVENTF_SCANCODE
        if ext:
            flags |= w.KEYEVENTF_EXTENDEDKEY
        event = w.make_key_input(0, scan, flags if down else flags | w.KEYEVENTF_KEYUP)
    else:
        event = w.make_key_input(vk, 0, 0 if down else w.KEYEVENTF_KEYUP)
    _fg_send([event])


def _fg_key(keys: str, hold_ms: int = 40, repeat: int = 1) -> dict:
    modifiers, key_name = parse_combo(keys)
    vk, _ext = resolve_vk(key_name)
    pressed: list[int] = []
    for mod in modifiers:
        mod_vk = MODIFIER_VK[mod]
        _fg_key_vk(mod_vk, True)
        pressed.append(mod_vk)
        time.sleep(0.01)
    for _ in range(max(1, int(repeat))):
        _fg_key_vk(vk, True)
        if hold_ms > 0:
            time.sleep(min(hold_ms, 5000) / 1000.0)
        _fg_key_vk(vk, False)
        time.sleep(0.01)
    for mod_vk in reversed(pressed):
        _fg_key_vk(mod_vk, False)
    return {"mode": "foreground", "keys": keys, "key": key_name, "vk": vk}


def _fg_absolute_move(hwnd: int, x: int, y: int) -> None:
    sx, sy = window.client_to_screen(hwnd, x, y)
    vx, vy, vw, vh = w.virtual_screen()
    # Normalise to 0..65535 over the virtual desktop, as SendInput requires.
    nx = int(round((sx - vx) * 65535 / max(1, vw - 1)))
    ny = int(round((sy - vy) * 65535 / max(1, vh - 1)))
    nx = max(0, min(65535, nx))
    ny = max(0, min(65535, ny))
    _fg_send(
        [
            w.make_mouse_input(
                w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK,
                nx,
                ny,
            )
        ]
    )


def _fg_click(
    hwnd: int,
    x: int,
    y: int,
    button: str,
    clicks: int,
    modifiers: list[str],
    restore_focus: bool,
) -> dict:
    _down, _up, _dbl, _mk, down_flag, up_flag = BUTTONS[button]
    with _foreground_guard(hwnd, restore_focus) as activated:
        pressed: list[int] = []
        for mod in modifiers:
            mod_vk = MODIFIER_VK.get(mod.lower())
            if mod_vk:
                _fg_key_vk(mod_vk, True)
                pressed.append(mod_vk)
                time.sleep(0.01)
        _fg_absolute_move(hwnd, x, y)
        time.sleep(0.03)
        for _ in range(max(1, int(clicks))):
            _fg_send([w.make_mouse_input(down_flag)])
            time.sleep(0.02)
            _fg_send([w.make_mouse_input(up_flag)])
            time.sleep(0.05)
        for mod_vk in reversed(pressed):
            _fg_key_vk(mod_vk, False)
    return {
        "mode": "foreground",
        "x": x,
        "y": y,
        "button": button,
        "clicks": max(1, int(clicks)),
        "modifiers": modifiers,
        "activated": activated,
    }


def _fg_move(hwnd: int, x: int, y: int, restore_focus: bool) -> dict:
    with _foreground_guard(hwnd, restore_focus) as activated:
        _fg_absolute_move(hwnd, x, y)
    return {"mode": "foreground", "x": x, "y": y, "activated": activated}


def _fg_drag(
    hwnd: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    button: str,
    steps: int,
    restore_focus: bool,
) -> dict:
    _down, _up, _dbl, _mk, down_flag, up_flag = BUTTONS[button]
    steps = max(1, int(steps))
    with _foreground_guard(hwnd, restore_focus) as activated:
        _fg_absolute_move(hwnd, x1, y1)
        time.sleep(0.05)
        _fg_send([w.make_mouse_input(down_flag)])
        time.sleep(0.05)
        for i in range(1, steps + 1):
            ix = int(round(x1 + (x2 - x1) * i / steps))
            iy = int(round(y1 + (y2 - y1) * i / steps))
            _fg_absolute_move(hwnd, ix, iy)
            time.sleep(0.02)
        _fg_send([w.make_mouse_input(up_flag)])
    return {
        "mode": "foreground",
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "button": button,
        "steps": steps,
        "activated": activated,
    }


def _fg_scroll(hwnd: int, x: int, y: int, delta: int, restore_focus: bool) -> dict:
    with _foreground_guard(hwnd, restore_focus) as activated:
        _fg_absolute_move(hwnd, x, y)
        time.sleep(0.03)
        _fg_send(
            [w.make_mouse_input(w.MOUSEEVENTF_WHEEL, data=int(delta) * 120)]
        )
    return {
        "mode": "foreground",
        "x": x,
        "y": y,
        "delta": delta,
        "activated": activated,
    }


def _fg_type_text(text: str, restore_focus: bool) -> dict:
    events: list[w.INPUT] = []
    normalized = text.replace("\n", "\r")
    encoded = normalized.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[i : i + 2], "little")
        events.append(w.make_key_input(char=unit, flags=0))
        events.append(w.make_key_input(char=unit, flags=w.KEYEVENTF_KEYUP))
    _fg_send(events)
    return {"mode": "foreground", "chars": len(events) // 2}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def click(
    hwnd: int,
    x: float,
    y: float,
    button: str = "left",
    clicks: int = 1,
    modifiers: list[str] | None = None,
    normalized: bool = False,
    mode: str | None = None,
    restore_focus: bool = True,
) -> dict:
    chosen = _resolve_mode(mode)
    button = (button or "left").lower()
    if button not in BUTTONS:
        raise ValueError("button must be 'left', 'right' or 'middle'")
    mods = [str(m).lower() for m in (modifiers or [])]
    for mod in mods:
        if mod not in MODIFIER_VK:
            raise ValueError(f"unsupported modifier: {mod!r}")
    px, py = to_client_xy(hwnd, x, y, normalized)
    if chosen == "background":
        return _bg_click(hwnd, px, py, button, clicks, mods, True, None)
    return _fg_click(hwnd, px, py, button, clicks, mods, restore_focus)


def mouse_move(
    hwnd: int,
    x: float,
    y: float,
    normalized: bool = False,
    mode: str | None = None,
    restore_focus: bool = True,
) -> dict:
    chosen = _resolve_mode(mode)
    px, py = to_client_xy(hwnd, x, y, normalized)
    if chosen == "background":
        warnings: list[str] = []
        _bg_mouse_move(hwnd, px, py, warnings)
        return {"mode": "background", "x": px, "y": py, "warnings": warnings}
    return _fg_move(hwnd, px, py, restore_focus)


def drag(
    hwnd: int,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    button: str = "left",
    steps: int = 10,
    normalized: bool = False,
    mode: str | None = None,
    restore_focus: bool = True,
) -> dict:
    chosen = _resolve_mode(mode)
    button = (button or "left").lower()
    if button not in BUTTONS:
        raise ValueError("button must be 'left', 'right' or 'middle'")
    ax, ay = to_client_xy(hwnd, x1, y1, normalized)
    bx, by = to_client_xy(hwnd, x2, y2, normalized)
    steps = max(1, int(steps))
    if chosen == "foreground":
        return _fg_drag(hwnd, ax, ay, bx, by, button, steps, restore_focus)

    warnings: list[str] = []
    _bg_fake_focus(hwnd, warnings)
    button_mk = BUTTONS[button][3]
    _bg_mouse_move(hwnd, ax, ay, warnings)
    _bg_button(hwnd, button, ax, ay, True, 0, False, warnings)
    for i in range(1, steps + 1):
        ix = int(round(ax + (bx - ax) * i / steps))
        iy = int(round(ay + (by - ay) * i / steps))
        _bg_mouse_move(hwnd, ix, iy, warnings, keystate=button_mk)
    _bg_button(hwnd, button, bx, by, False, 0, False, warnings)
    return {
        "mode": "background",
        "x1": ax,
        "y1": ay,
        "x2": bx,
        "y2": by,
        "button": button,
        "steps": steps,
        "warnings": warnings,
    }


def scroll(
    hwnd: int,
    x: float,
    y: float,
    delta: int,
    normalized: bool = False,
    mode: str | None = None,
    restore_focus: bool = True,
) -> dict:
    chosen = _resolve_mode(mode)
    px, py = to_client_xy(hwnd, x, y, normalized)
    if chosen == "background":
        return _bg_scroll(hwnd, px, py, int(delta))
    return _fg_scroll(hwnd, px, py, int(delta), restore_focus)


def key(
    hwnd: int,
    keys: str,
    hold_ms: int = 40,
    repeat: int = 1,
    mode: str | None = None,
    restore_focus: bool = True,
    send_char: bool = False,
) -> dict:
    chosen = _resolve_mode(mode)
    if chosen == "background":
        return _bg_key(hwnd, keys, hold_ms=hold_ms, repeat=repeat, send_char=send_char)
    with _foreground_guard(hwnd, restore_focus) as activated:
        info = _fg_key(keys, hold_ms=hold_ms, repeat=repeat)
    info["activated"] = activated
    return info


def type_text(
    hwnd: int,
    text: str,
    mode: str | None = None,
    restore_focus: bool = True,
) -> dict:
    chosen = _resolve_mode(mode)
    if chosen == "background":
        return _bg_type_text(hwnd, text, True)
    with _foreground_guard(hwnd, restore_focus) as activated:
        info = _fg_type_text(text, restore_focus)
    info["activated"] = activated
    return info


def send_chat(
    hwnd: int,
    text: str,
    to_all: bool = False,
    mode: str | None = None,
    restore_focus: bool = True,
) -> dict:
    """Open the chat line, type *text* and press Enter.

    ``to_all`` uses Shift+Enter (all-chat) where the game supports it.
    """
    chosen = _resolve_mode(mode)
    enter = "shift+enter" if to_all else "enter"
    if chosen == "background":
        first = _bg_key(hwnd, enter)
        time.sleep(0.15)
        typed = _bg_type_text(hwnd, text, False)
        time.sleep(0.1)
        last = _bg_key(hwnd, "enter", fake_focus=False)
        warnings = list(first.get("warnings", [])) + list(typed.get("warnings", [])) + list(
            last.get("warnings", [])
        )
        return {
            "mode": "background",
            "text": text,
            "to_all": bool(to_all),
            "warnings": warnings,
        }

    with _foreground_guard(hwnd, restore_focus) as activated:
        _fg_key(enter)
        time.sleep(0.15)
        _fg_type_text(text, restore_focus=False)
        time.sleep(0.1)
        _fg_key("enter")
    return {
        "mode": "foreground",
        "text": text,
        "to_all": bool(to_all),
        "activated": activated,
    }
