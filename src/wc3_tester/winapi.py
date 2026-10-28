"""Thin, fully-typed ctypes layer over the Win32 APIs used by wc3-tester.

Everything here is designed so that the module can still be *imported* on
Linux (useful for linting / unit-testing pure helpers).  Actually calling a
Win32 function on a non-Windows platform raises ``RuntimeError``.

Rule of thumb: every function we call has explicit ``argtypes`` and
``restype``.  Handles are ``c_void_p`` (pointer sized), integers are the exact
Win32 widths (DWORD/LONG/WORD are always 32/32/16 bits, also on Linux) so that
structure layouts such as ``INPUT`` are correct (40 bytes on x64).
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import (
    POINTER,
    Structure,
    Union,
    byref,
    c_char,
    c_int,
    c_int32,
    c_size_t,
    c_ssize_t,
    c_ubyte,
    c_uint16,
    c_uint32,
    c_void_p,
    c_wchar,
    c_wchar_p,
)

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Basic types (explicit widths -> identical layout on every platform)
# ---------------------------------------------------------------------------
BOOL = c_int
BYTE = c_ubyte
WORD = c_uint16
DWORD = c_uint32
UINT = c_uint32
LONG = c_int32
ULONG = c_uint32
ULONG_PTR = c_size_t
WPARAM = c_size_t
LPARAM = c_ssize_t
LRESULT = c_ssize_t
HRESULT = c_int32
HANDLE = c_void_p
HWND = c_void_p
HDC = c_void_p
HBITMAP = c_void_p
HGDIOBJ = c_void_p
HMODULE = c_void_p
LPCWSTR = c_wchar_p
LPWSTR = c_wchar_p
LPVOID = c_void_p

# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------


class RECT(Structure):
    _fields_ = [
        ("left", LONG),
        ("top", LONG),
        ("right", LONG),
        ("bottom", LONG),
    ]


class POINT(Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RGBQUAD(Structure):
    _fields_ = [
        ("rgbBlue", BYTE),
        ("rgbGreen", BYTE),
        ("rgbRed", BYTE),
        ("rgbReserved", BYTE),
    ]


class BITMAPINFOHEADER(Structure):
    _fields_ = [
        ("biSize", DWORD),
        ("biWidth", LONG),
        ("biHeight", LONG),
        ("biPlanes", WORD),
        ("biBitCount", WORD),
        ("biCompression", DWORD),
        ("biSizeImage", DWORD),
        ("biXPelsPerMeter", LONG),
        ("biYPelsPerMeter", LONG),
        ("biClrUsed", DWORD),
        ("biClrImportant", DWORD),
    ]


class BITMAPINFO(Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", RGBQUAD * 1),
    ]


class MOUSEINPUT(Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(Structure):
    _fields_ = [
        ("uMsg", DWORD),
        ("wParamL", WORD),
        ("wParamH", WORD),
    ]


class _INPUTUNION(Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", DWORD),
        ("u", _INPUTUNION),
    ]


class GUID(Structure):
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", c_uint16),
        ("Data3", c_uint16),
        ("Data4", c_ubyte * 8),
    ]


# EnumWindows callback: BOOL CALLBACK (HWND, LPARAM)
if IS_WINDOWS:  # pragma: no cover - Windows only
    WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)
else:  # pragma: no cover - allows import on Linux
    WNDENUMPROC = ctypes.CFUNCTYPE(BOOL, HWND, LPARAM)


# ---------------------------------------------------------------------------
# DLL loading (with harmless stubs on non-Windows)
# ---------------------------------------------------------------------------


class _StubFunc:
    def __init__(self, name: str) -> None:
        self.name = name
        self.argtypes = None
        self.restype = None

    def __call__(self, *args, **kwargs):  # pragma: no cover
        raise RuntimeError(
            f"Win32 function {self.name}() is only available on Windows"
        )


class _StubDLL:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, item: str):  # pragma: no cover
        func = _StubFunc(f"{self._name}.{item}")
        setattr(self, item, func)
        return func


if IS_WINDOWS:  # pragma: no cover - Windows only
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
    except Exception:  # pragma: no cover - very old Windows
        shcore = _StubDLL("shcore")
else:  # pragma: no cover
    user32 = _StubDLL("user32")
    gdi32 = _StubDLL("gdi32")
    kernel32 = _StubDLL("kernel32")
    shell32 = _StubDLL("shell32")
    ole32 = _StubDLL("ole32")
    shcore = _StubDLL("shcore")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WM_NULL = 0x0000
WM_ACTIVATE = 0x0006
WM_SETFOCUS = 0x0007
WM_KILLFOCUS = 0x0008
WM_CLOSE = 0x0010
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MBUTTONDBLCLK = 0x0209
WM_MOUSEWHEEL = 0x020A

WA_ACTIVE = 1
WA_CLICKACTIVE = 2

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_SHIFT = 0x0004
MK_CONTROL = 0x0008
MK_MBUTTON = 0x0010
MK_XBUTTON1 = 0x0020
MK_XBUTTON2 = 0x0040

SMTO_NORMAL = 0x0000
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
SMTO_NOTIMEOUTIFNOTHUNG = 0x0008

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

BI_RGB = 0
DIB_RGB_COLORS = 0

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

MAPVK_VK_TO_VSC = 0
MAPVK_VSC_TO_VK = 1
MAPVK_VK_TO_CHAR = 2
MAPVK_VK_TO_VSC_EX = 4

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1

# Known folder: FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
FOLDERID_Documents = GUID(
    0xFDD39AD0,
    0x238F,
    0x46AF,
    (c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
)

# ---------------------------------------------------------------------------
# user32 signatures
# ---------------------------------------------------------------------------
user32.EnumWindows.argtypes = [WNDENUMPROC, LPARAM]
user32.EnumWindows.restype = BOOL
user32.EnumChildWindows.argtypes = [HWND, WNDENUMPROC, LPARAM]
user32.EnumChildWindows.restype = BOOL
user32.GetWindowThreadProcessId.argtypes = [HWND, POINTER(DWORD)]
user32.GetWindowThreadProcessId.restype = DWORD
user32.IsWindow.argtypes = [HWND]
user32.IsWindow.restype = BOOL
user32.IsWindowVisible.argtypes = [HWND]
user32.IsWindowVisible.restype = BOOL
user32.IsWindowEnabled.argtypes = [HWND]
user32.IsWindowEnabled.restype = BOOL
user32.GetWindowTextW.argtypes = [HWND, LPWSTR, c_int]
user32.GetWindowTextW.restype = c_int
user32.GetWindowTextLengthW.argtypes = [HWND]
user32.GetWindowTextLengthW.restype = c_int
user32.GetClassNameW.argtypes = [HWND, LPWSTR, c_int]
user32.GetClassNameW.restype = c_int
user32.GetClientRect.argtypes = [HWND, POINTER(RECT)]
user32.GetClientRect.restype = BOOL
user32.GetWindowRect.argtypes = [HWND, POINTER(RECT)]
user32.GetWindowRect.restype = BOOL
user32.ClientToScreen.argtypes = [HWND, POINTER(POINT)]
user32.ClientToScreen.restype = BOOL
user32.ScreenToClient.argtypes = [HWND, POINTER(POINT)]
user32.ScreenToClient.restype = BOOL
user32.IsHungAppWindow.argtypes = [HWND]
user32.IsHungAppWindow.restype = BOOL
user32.IsIconic.argtypes = [HWND]
user32.IsIconic.restype = BOOL
user32.IsZoomed.argtypes = [HWND]
user32.IsZoomed.restype = BOOL
user32.SendMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
user32.SendMessageW.restype = LRESULT
user32.SendMessageTimeoutW.argtypes = [
    HWND,
    UINT,
    WPARAM,
    LPARAM,
    UINT,
    UINT,
    POINTER(ULONG_PTR),
]
user32.SendMessageTimeoutW.restype = LRESULT
user32.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = HWND
user32.SetForegroundWindow.argtypes = [HWND]
user32.SetForegroundWindow.restype = BOOL
user32.BringWindowToTop.argtypes = [HWND]
user32.BringWindowToTop.restype = BOOL
user32.ShowWindow.argtypes = [HWND, c_int]
user32.ShowWindow.restype = BOOL
user32.AttachThreadInput.argtypes = [DWORD, DWORD, BOOL]
user32.AttachThreadInput.restype = BOOL
user32.GetSystemMetrics.argtypes = [c_int]
user32.GetSystemMetrics.restype = c_int
user32.MapVirtualKeyW.argtypes = [UINT, UINT]
user32.MapVirtualKeyW.restype = UINT
user32.VkKeyScanW.argtypes = [c_wchar]
user32.VkKeyScanW.restype = c_int
user32.GetWindowDC.argtypes = [HWND]
user32.GetWindowDC.restype = HDC
user32.GetDC.argtypes = [HWND]
user32.GetDC.restype = HDC
user32.ReleaseDC.argtypes = [HWND, HDC]
user32.ReleaseDC.restype = c_int
user32.PrintWindow.argtypes = [HWND, HDC, UINT]
user32.PrintWindow.restype = BOOL
user32.SendInput.argtypes = [UINT, POINTER(INPUT), c_int]
user32.SendInput.restype = UINT
user32.SetCursorPos.argtypes = [c_int, c_int]
user32.SetCursorPos.restype = BOOL
user32.GetCursorPos.argtypes = [POINTER(POINT)]
user32.GetCursorPos.restype = BOOL
user32.SetProcessDPIAware.argtypes = []
user32.SetProcessDPIAware.restype = BOOL
try:  # Windows 10 1703+
    user32.SetProcessDpiAwarenessContext.argtypes = [HANDLE]
    user32.SetProcessDpiAwarenessContext.restype = BOOL
except Exception:  # pragma: no cover - very old Windows
    pass

# ---------------------------------------------------------------------------
# gdi32 / kernel32 / shell32 / ole32 signatures
# ---------------------------------------------------------------------------
gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleBitmap.argtypes = [HDC, c_int, c_int]
gdi32.CreateCompatibleBitmap.restype = HBITMAP
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteObject.restype = BOOL
gdi32.DeleteDC.argtypes = [HDC]
gdi32.DeleteDC.restype = BOOL
gdi32.GetDIBits.argtypes = [
    HDC,
    HBITMAP,
    UINT,
    UINT,
    LPVOID,
    POINTER(BITMAPINFO),
    UINT,
]
gdi32.GetDIBits.restype = c_int
gdi32.GetDeviceCaps.argtypes = [HDC, c_int]
gdi32.GetDeviceCaps.restype = c_int

kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = DWORD
kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = DWORD

shell32.SHGetKnownFolderPath.argtypes = [
    POINTER(GUID),
    DWORD,
    HANDLE,
    POINTER(c_wchar_p),
]
shell32.SHGetKnownFolderPath.restype = HRESULT

ole32.CoTaskMemFree.argtypes = [LPVOID]
ole32.CoTaskMemFree.restype = None

shcore.SetProcessDpiAwareness.argtypes = [c_int]
shcore.SetProcessDpiAwareness.restype = HRESULT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DPI_AWARENESS_SET = False


def enable_dpi_awareness() -> bool:
    """Make this process per-monitor DPI aware (idempotent).

    Without this, Windows virtualises GetClientRect/SendInput coordinates on
    high-DPI screens and screenshots no longer line up with clicks.
    """
    global _DPI_AWARENESS_SET
    if _DPI_AWARENESS_SET or not IS_WINDOWS:
        return _DPI_AWARENESS_SET
    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
    try:
        set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if set_context is not None and bool_at(set_context(ctypes.c_void_p(-4))):
            _DPI_AWARENESS_SET = True
            return True
    except Exception:
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE == 2
        if int(shcore.SetProcessDpiAwareness(2)) == 0:
            _DPI_AWARENESS_SET = True
            return True
    except Exception:
        pass
    try:
        _DPI_AWARENESS_SET = bool_at(user32.SetProcessDPIAware())
    except Exception:
        _DPI_AWARENESS_SET = False
    return _DPI_AWARENESS_SET


def last_error() -> int:
    """Return the calling thread's last Win32 error code."""
    return int(ctypes.get_last_error())


def bool_at(value: int) -> bool:
    return bool(value & 0xFF)


def loword(value: int) -> int:
    return value & 0xFFFF


def hiword(value: int) -> int:
    return (value >> 16) & 0xFFFF


def make_word(low: int, high: int) -> int:
    """MAKEWORD"""
    return (low & 0xFFFF) | ((high & 0xFFFF) << 16)


def make_lparam(x: int, y: int) -> int:
    """MAKELPARAM with 16-bit signed truncation (as Windows expects)."""
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


def get_xy_lparam(lparam: int) -> tuple[int, int]:
    """Extract *signed* x/y from a mouse lParam."""
    x = loword(lparam)
    y = hiword(lparam)
    if x >= 0x8000:
        x -= 0x10000
    if y >= 0x8000:
        y -= 0x10000
    return x, y


def enum_windows() -> list[int]:
    """All top-level windows, in Z-order."""
    hwnds: list[int] = []

    @WNDENUMPROC
    def _cb(hwnd, _lparam):  # pragma: no cover - needs Windows
        try:
            hwnds.append(int(hwnd))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        return []
    return hwnds


def enum_child_windows(hwnd: int) -> list[int]:
    hwnds: list[int] = []

    @WNDENUMPROC
    def _cb(child, _lparam):  # pragma: no cover - needs Windows
        try:
            hwnds.append(int(child))
        except Exception:
            pass
        return True

    try:
        user32.EnumChildWindows(hwnd, _cb, 0)
    except Exception:
        return []
    return hwnds


def get_window_text(hwnd: int, maxlen: int = 4096) -> str:
    buf = ctypes.create_unicode_buffer(maxlen)
    try:
        user32.GetWindowTextW(hwnd, buf, maxlen)
    except Exception:
        return ""
    return buf.value


def get_class_name(hwnd: int, maxlen: int = 512) -> str:
    buf = ctypes.create_unicode_buffer(maxlen)
    try:
        user32.GetClassNameW(hwnd, buf, maxlen)
    except Exception:
        return ""
    return buf.value


def get_window_text_timeout(hwnd: int, timeout_ms: int = 1000) -> str:
    """Read window text without risking a hang on an unresponsive game."""
    length = 0
    result = ULONG_PTR(0)
    try:
        length = int(
            user32.SendMessageTimeoutW(
                hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG | SMTO_BLOCK, timeout_ms, byref(result)
            )
        )
    except Exception:
        length = 0
    size = max(256, min(int(length) + 1, 65536))
    buf = ctypes.create_unicode_buffer(size)
    try:
        user32.SendMessageTimeoutW(
            hwnd,
            WM_GETTEXT,
            size,
            ctypes.addressof(buf),
            SMTO_ABORTIFHUNG | SMTO_BLOCK,
            timeout_ms,
            byref(result),
        )
    except Exception:
        return ""
    return buf.value


def send_message_timeout(
    hwnd: int,
    msg: int,
    wparam: int = 0,
    lparam: int = 0,
    flags: int = SMTO_ABORTIFHUNG,
    timeout_ms: int = 2000,
) -> tuple[bool, int]:
    result = ULONG_PTR(0)
    try:
        res = int(
            user32.SendMessageTimeoutW(
                hwnd, msg, wparam, lparam, flags, timeout_ms, byref(result)
            )
        )
    except Exception:
        return False, 0
    return res != 0, int(result.value)


def is_window(hwnd: int | None) -> bool:
    if not hwnd:
        return False
    try:
        return bool_at(user32.IsWindow(hwnd))
    except Exception:
        return False


def is_window_visible(hwnd: int) -> bool:
    try:
        return bool_at(user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def is_iconic(hwnd: int) -> bool:
    try:
        return bool_at(user32.IsIconic(hwnd))
    except Exception:
        return False


def window_pid(hwnd: int) -> int:
    pid = DWORD(0)
    try:
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
    except Exception:
        return 0
    return int(pid.value)


def window_thread_id(hwnd: int) -> int:
    """Return the thread id that owns *hwnd* (GetWindowThreadProcessId)."""
    try:
        return int(user32.GetWindowThreadProcessId(hwnd, None))
    except Exception:
        return 0


def get_client_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = RECT()
    try:
        if not user32.GetClientRect(hwnd, byref(rect)):
            return (0, 0, 0, 0)
    except Exception:
        return (0, 0, 0, 0)
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = RECT()
    try:
        if not user32.GetWindowRect(hwnd, byref(rect)):
            return (0, 0, 0, 0)
    except Exception:
        return (0, 0, 0, 0)
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    pt = POINT(int(x), int(y))
    try:
        if not user32.ClientToScreen(hwnd, byref(pt)):
            return (int(x), int(y))
    except Exception:
        return (int(x), int(y))
    return (int(pt.x), int(pt.y))


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    pt = POINT(int(x), int(y))
    try:
        if not user32.ScreenToClient(hwnd, byref(pt)):
            return (int(x), int(y))
    except Exception:
        return (int(x), int(y))
    return (int(pt.x), int(pt.y))


def get_foreground_window() -> int:
    try:
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def get_system_metrics(index: int) -> int:
    try:
        return int(user32.GetSystemMetrics(index))
    except Exception:
        return 0


def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, width, height) of the virtual desktop."""
    x = get_system_metrics(SM_XVIRTUALSCREEN)
    y = get_system_metrics(SM_YVIRTUALSCREEN)
    w = get_system_metrics(SM_CXVIRTUALSCREEN)
    h = get_system_metrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        w = get_system_metrics(SM_CXSCREEN) or 1920
        h = get_system_metrics(SM_CYSCREEN) or 1080
        x, y = 0, 0
    return x, y, w, h


def known_folder_documents() -> str | None:
    """Resolve the Documents known folder (handles OneDrive redirection)."""
    if not IS_WINDOWS:
        return None
    out = c_wchar_p()
    try:
        hr = shell32.SHGetKnownFolderPath(byref(FOLDERID_Documents), 0, None, byref(out))
        if hr != 0 or not out.value:
            return None
        path = out.value
        return str(path)
    except Exception:
        return None
    finally:
        try:
            if out.value if out else False:
                ole32.CoTaskMemFree(ctypes.cast(out, LPVOID))
        except Exception:
            pass


def send_inputs(inputs: list[INPUT]) -> int:
    """Send an array of INPUT structures; returns the number accepted."""
    if not inputs:
        return 0
    arr = (INPUT * len(inputs))(*inputs)
    try:
        return int(user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)))
    except Exception:
        return 0


def make_mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = MOUSEINPUT(int(dx), int(dy), int(data) & 0xFFFFFFFF, int(flags), 0, 0)
    return inp


def make_key_input(
    vk: int = 0,
    scan: int = 0,
    flags: int = 0,
    char: int | None = None,
) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    if char is not None:
        # KEYEVENTF_UNICODE: wVk must be 0, wScan carries the UTF-16 code unit
        inp.ki = KEYBDINPUT(0, int(char) & 0xFFFF, flags | KEYEVENTF_UNICODE, 0, 0)
    else:
        inp.ki = KEYBDINPUT(int(vk) & 0xFFFF, int(scan) & 0xFFFF, int(flags), 0, 0)
    return inp


if ctypes.sizeof(c_void_p) == 8 and ctypes.sizeof(INPUT) != 40:  # pragma: no cover
    raise RuntimeError(
        f"Win32 INPUT structure size is {ctypes.sizeof(INPUT)}, expected 40"
    )
