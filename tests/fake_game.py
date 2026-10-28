"""A stand-in for Warcraft III used by the smoke test.

A raw Win32 window (class and title "Warcraft III") with its own window
procedure, like the real game, so posted messages are handled the way a game
handles them (GUI toolkits such as Tk re-derive mouse state from the real
cursor and would ignore background clicks). Every key/char/mouse/wheel message
is appended as one JSON line to ``argv[1]``; clicks paint a dot so screenshots
change. F12 writes a WC3-style crash log into ``argv[2]`` and then crashes the
process with a real access violation (exit code 0xC0000005).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import traceback
from ctypes import wintypes as wt
from datetime import datetime, timezone
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT),
        ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL), ("rgbReserved", ctypes.c_byte * 32),
    ]


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.CreateWindowExW.restype = wt.HWND
user32.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.BeginPaint.restype = wt.HDC
user32.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
user32.InvalidateRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT), wt.BOOL]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
gdi32.CreateSolidBrush.restype = wt.HBRUSH
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.Ellipse.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
user32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

WM_DESTROY, WM_PAINT, WM_CLOSE = 0x0002, 0x000F, 0x0010
WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0102, 0x0104, 0x0105
WM_MOUSEMOVE, WM_MOUSEWHEEL = 0x0200, 0x020A
MOUSE_DOWN = {0x0201: 1, 0x0203: 1, 0x0204: 3, 0x0207: 2}  # L, L-dbl, R, M
MOUSE_UP = {0x0202: 1, 0x0205: 3, 0x0208: 2}
VK_F12 = 0x7B
WS_OVERLAPPEDWINDOW, WS_VISIBLE = 0x00CF0000, 0x10000000

CRASH_TEXT = """========================================================================
This application has encountered a critical error:
FATAL ERROR!

Program: C:\\Program Files (x86)\\Warcraft III\\war3.exe
Exception: 0xC0000005 (ACCESS_VIOLATION) at 001B:6F4A1B2C

The instruction at '0x6F4A1B2C' referenced memory at '0x00000000'.
The memory could not be 'read'.

Press OK to terminate the application.
============================================================================
Fault address: 6F4A1B2C 01:00000B2C C:\\Program Files (x86)\\Warcraft III\\war3.exe
Exception: 0xC0000005 (ACCESS_VIOLATION)
Register dump:
 EAX:00000000 EBX:00000001 ECX:00000000 EDX:00000000
 ESI:00000000 EDI:00000000 EBP:0019FF00 ESP:0019FEF0
 EIP:6F4A1B2C EFL:00010202
Call stack:
001B:6F4A1B2C war3.exe
001B:6F4A1C10 war3.exe
001B:6F4A1D44 war3.exe
Script:
war3map.j:1234 TriggerExecute
war3map.j:5678 main
JASS function crashed
========================================================================
"""

DOT_COLORS = [0x4040FF, 0x40FF40, 0xFF4040, 0x40FFFF, 0xFF40FF, 0xFFFF40]  # BGR


def _signed16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: fake_game.py <event-log.jsonl> <errors-dir>", file=sys.stderr)
        return 2
    log_path = os.path.abspath(sys.argv[1])
    errors_dir = Path(os.path.abspath(sys.argv[2]))
    errors_dir.mkdir(parents=True, exist_ok=True)
    dots: list[tuple[int, int]] = []
    crash_pending: list[bool] = []

    def log(payload: dict) -> None:
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload) + "\n")
        except Exception:
            traceback.print_exc(file=sys.stderr)

    def crash() -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        (errors_dir / f"{stamp}.txt").write_text(CRASH_TEXT, encoding="utf-8")
        sys.stderr.flush()
        # Reading address 8 through a pointer object faults in plain C (no
        # ctypes SEH guard), so Windows ends the process with 0xC0000005.
        try:
            ctypes.cast(8, ctypes.POINTER(ctypes.c_int)).contents.value
        finally:
            kernel32.TerminateProcess(ctypes.c_void_p(-1), ctypes.c_uint(0xC0000005))

    def paint(hwnd) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        rc = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        bg = gdi32.CreateSolidBrush(0x403020)
        user32.FillRect(hdc, ctypes.byref(rc), bg)
        gdi32.DeleteObject(bg)
        gdi32.SetBkMode(hdc, 1)
        gdi32.SetTextColor(hdc, 0xFFFFFF)
        title = wt.RECT(10, 10, 400, 40)
        user32.DrawTextW(hdc, "FAKE WARCRAFT III", -1, ctypes.byref(title), 0)
        # A fixed row of dots plus one per click, so frames are not blank.
        points = [(80 + i * 55, 520) for i in range(12)] + dots
        for i, (x, y) in enumerate(points):
            br = gdi32.CreateSolidBrush(DOT_COLORS[i % len(DOT_COLORS)])
            old = gdi32.SelectObject(hdc, br)
            gdi32.Ellipse(hdc, x - 10, y - 10, x + 10, y + 10)
            gdi32.SelectObject(hdc, old)
            gdi32.DeleteObject(br)
        user32.EndPaint(hwnd, ctypes.byref(ps))

    def wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
                down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
                log({"type": "keydown" if down else "keyup", "vk": int(wparam)})
                if down and wparam == VK_F12:
                    # Crash after leaving the callback: a fault inside a
                    # ctypes callback makes Python fast-fail with 0xC0000409.
                    crash_pending.append(True)
                    user32.PostQuitMessage(0)
                return 0
            if msg == WM_CHAR:
                log({"type": "key", "char": chr(int(wparam) & 0xFFFF)})
                return 0
            if msg in MOUSE_DOWN or msg in MOUSE_UP:
                x, y = _signed16(lparam), _signed16(lparam >> 16)
                down = msg in MOUSE_DOWN
                log({"type": "mouse", "kind": "down" if down else "up",
                     "button": (MOUSE_DOWN if down else MOUSE_UP)[msg],
                     "x": x, "y": y, "state": int(wparam)})
                if down:
                    dots.append((x, y))
                    user32.InvalidateRect(hwnd, None, True)
                return 0
            if msg == WM_MOUSEWHEEL:
                log({"type": "wheel", "delta": _signed16(wparam >> 16),
                     "x": _signed16(lparam), "y": _signed16(lparam >> 16),
                     "state": int(wparam) & 0xFFFF})
                return 0
            if msg == WM_PAINT:
                paint(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            traceback.print_exc(file=sys.stderr)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wndproc)  # keep a reference for the lifetime of the window
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.lpszClassName = "Warcraft III"
    wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
    if not user32.RegisterClassW(ctypes.byref(wc)):
        print(f"RegisterClassW failed: {ctypes.get_last_error()}", file=sys.stderr)
        return 4
    # 816x639 outer size gives roughly an 800x600 client area at 100% scaling.
    hwnd = user32.CreateWindowExW(0, "Warcraft III", "Warcraft III",
                                  WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                  80, 80, 816, 639, None, None, hinst, None)
    if not hwnd:
        print(f"CreateWindowExW failed: {ctypes.get_last_error()}", file=sys.stderr)
        return 5
    log({"type": "ready"})
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    if crash_pending:
        crash()
    return 0


if __name__ == "__main__":
    sys.exit(main())
