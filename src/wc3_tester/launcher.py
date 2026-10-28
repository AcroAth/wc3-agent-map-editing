"""Starting game processes: background start, launch wrapper, several instances.

* ``start_process`` can start the game without it taking keyboard focus: a
  process created through WMI is not allowed to grab the foreground window.
* A *launch wrapper* is an optional user-installed command placed in front of
  the game's command line for LAN instances, e.g. ForceBindIP so that instance
  N uses its own address 127.0.0.N.  The plugin's built-in bind hook
  (:mod:`bindhook`) normally does this itself; a wrapper is only an override
  for tools the user prefers.  The plugin does not ship or download any such
  tool.  Configure it with the ``WC3_LAUNCH_WRAPPER`` environment variable or
  ``launch_wrapper`` in ``%LOCALAPPDATA%\\wc3-tester\\config.json``; ``{ip}``
  is replaced by the instance's LAN address.
* ``release_single_instance`` closes the "Warcraft III Game Application" event
  held by running games, which otherwise makes a second game exit at start.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import os
import subprocess
import time
from ctypes import wintypes as wt
from pathlib import Path

log = logging.getLogger(__name__)

SINGLE_INSTANCE_EVENT = "Warcraft III Game Application"
GAME_EXE_NAMES = {"warcraft iii.exe", "war3.exe", "frozen throne.exe"}
_STRING_KEY = r"Software\Blizzard Entertainment\Warcraft III\String"


# ---------------------------------------------------------------------------
# Launch wrapper setting
# ---------------------------------------------------------------------------


def config_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "wc3-tester" / "config.json"


def launch_wrapper(override: str | None = None) -> str | None:
    """The wrapper command template: explicit value, env var, then config file."""
    if override:
        return override
    env = os.environ.get("WC3_LAUNCH_WRAPPER")
    if env:
        return env
    try:
        value = json.loads(config_path().read_text(encoding="utf-8")).get("launch_wrapper")
        return str(value) if value else None
    except Exception:
        return None


def set_launch_wrapper(template: str | None) -> dict:
    """Save (or clear with None/"") the wrapper template in config.json."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if template:
        if "{ip}" not in template:
            raise ValueError("the wrapper template must contain {ip}, e.g. C:\\Tools\\ForceBindIP.exe {ip}")
        data["launch_wrapper"] = template
    else:
        data.pop("launch_wrapper", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"config": str(path), "launch_wrapper": data.get("launch_wrapper")}


def command_line(exe: str, args: list[str], wrapper: str | None = None, ip: str | None = None) -> str:
    """Full command line; the wrapper is used only when an ``ip`` is given."""
    game = subprocess.list2cmdline([exe, *args])
    if wrapper and ip:
        return f"{wrapper.format(ip=ip)} {game}"
    return game


# ---------------------------------------------------------------------------
# Process start
# ---------------------------------------------------------------------------


def _wmi_create(cmdline: str, cwd: str) -> int:
    """Win32_Process.Create: the new process cannot take the foreground."""
    def q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"
    script = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
        f"@{{CommandLine={q(cmdline)}; CurrentDirectory={q(cwd)}}}; "
        "if ($r.ReturnValue -ne 0) { throw ('Win32_Process.Create returned ' + $r.ReturnValue) }; "
        "$r.ProcessId"
    )
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if out.returncode != 0 or not lines or not lines[-1].isdigit():
        raise RuntimeError(f"background start failed: {(out.stderr or out.stdout).strip()[:500]}")
    return int(lines[-1])


def start_process(
    exe: str,
    args: list[str],
    cwd: str,
    background: bool,
    wrapper: str | None = None,
    ip: str | None = None,
) -> tuple[int, subprocess.Popen | None]:
    """Start the game (through the wrapper when ``wrapper`` and ``ip`` are set).

    Returns ``(pid, popen)``.  ``popen`` is None for background starts.  With a
    wrapper the pid is the wrapper's; the caller finds the game by its window.
    """
    cmdline = command_line(exe, args, wrapper, ip)
    log.info("start_process background=%s: %s", background, cmdline)
    if background:
        return _wmi_create(cmdline, cwd), None
    popen = subprocess.Popen(cmdline, cwd=cwd)
    return popen.pid, popen


# ---------------------------------------------------------------------------
# Single-instance marker
# ---------------------------------------------------------------------------


class _HandleEntry(ctypes.Structure):
    _fields_ = [
        ("Object", ctypes.c_void_p), ("UniqueProcessId", ctypes.c_size_t), ("HandleValue", ctypes.c_size_t),
        ("GrantedAccess", wt.ULONG), ("CreatorBackTraceIndex", wt.USHORT), ("ObjectTypeIndex", wt.USHORT),
        ("HandleAttributes", wt.ULONG), ("Reserved", wt.ULONG),
    ]


def _game_pids() -> set[int]:
    import psutil

    pids = set()
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() in GAME_EXE_NAMES:
            pids.add(p.pid)
    return pids


def _object_name(ntdll, handle) -> str:
    buf = ctypes.create_string_buffer(4096)
    if ntdll.NtQueryObject(handle, 1, buf, 4096, None) != 0:  # ObjectNameInformation
        return ""
    length = ctypes.c_ushort.from_buffer(buf).value
    ptr = ctypes.c_void_p.from_buffer(buf, ctypes.sizeof(ctypes.c_void_p)).value
    return ctypes.wstring_at(ptr, length // 2) if ptr else ""


def release_single_instance() -> int:
    """Close the single-instance event in every running game. Never raises."""
    try:
        pids = _game_pids()
        if not pids:
            return 0
        ntdll = ctypes.WinDLL("ntdll")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.DuplicateHandle.argtypes = [wt.HANDLE, wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.HANDLE),
                                        wt.DWORD, wt.BOOL, wt.DWORD]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        ntdll.NtQueryObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
        size = 1 << 22
        while True:
            buf = ctypes.create_string_buffer(size)
            status = ntdll.NtQuerySystemInformation(64, buf, size, None) & 0xFFFFFFFF  # SystemExtendedHandleInformation
            if status != 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
                break
            size *= 2
        if status != 0:
            return 0
        count = ctypes.c_size_t.from_buffer(buf).value
        entries = (_HandleEntry * count).from_buffer(buf, 2 * ctypes.sizeof(ctypes.c_size_t))
        me = k32.GetCurrentProcess()
        closed = 0
        procs: dict[int, int] = {}
        for e in entries:
            pid = int(e.UniqueProcessId)
            if pid not in pids:
                continue
            if pid not in procs:
                procs[pid] = k32.OpenProcess(0x0040, False, pid) or 0  # PROCESS_DUP_HANDLE
            hp = procs[pid]
            if not hp:
                continue
            dup = wt.HANDLE()
            if not k32.DuplicateHandle(hp, e.HandleValue, me, ctypes.byref(dup), 0, False, 2):
                continue
            name = _object_name(ntdll, dup)
            k32.CloseHandle(dup)
            if name.endswith("\\" + SINGLE_INSTANCE_EVENT):
                # DUPLICATE_CLOSE_SOURCE closes the handle inside the game.
                closed += bool(k32.DuplicateHandle(hp, e.HandleValue, None, None, 0, False, 1))
        for hp in procs.values():
            if hp:
                k32.CloseHandle(hp)
        return closed
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("release_single_instance failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# LAN player name
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def lan_identity(player_name: str | None):
    """While active, a newly started game reads ``player_name`` as its LAN name
    (registry String\\userlocal); the previous value is restored afterwards."""
    if not player_name:
        yield
        return
    import winreg

    old = None
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _STRING_KEY) as key:
        try:
            old = winreg.QueryValueEx(key, "userlocal")
        except OSError:
            pass
        winreg.SetValueEx(key, "userlocal", 0, winreg.REG_SZ, player_name)
    try:
        yield
    finally:
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _STRING_KEY) as key:
                if old is None:
                    winreg.DeleteValue(key, "userlocal")
                else:
                    winreg.SetValueEx(key, "userlocal", 0, old[1], old[0])
        except OSError as exc:
            log.warning("could not restore LAN name: %s", exc)


# ---------------------------------------------------------------------------
# Window placement
# ---------------------------------------------------------------------------


def place_window(hwnd: int, maximized: bool, to_back: bool) -> dict:
    """Maximize without activating and/or move to the bottom. Never raises."""
    info: dict = {"maximized": False, "to_back": False}
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.ShowWindowAsync.argtypes = [wt.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
        if maximized and not u.IsZoomed(wt.HWND(hwnd)):
            u.ShowWindowAsync(wt.HWND(hwnd), 3)  # SW_MAXIMIZE, posted: no focus change
            for _ in range(20):
                if u.IsZoomed(wt.HWND(hwnd)):
                    break
                time.sleep(0.1)
        info["maximized"] = bool(u.IsZoomed(wt.HWND(hwnd)))
        if to_back:
            # HWND_BOTTOM, SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE|SWP_ASYNCWINDOWPOS
            info["to_back"] = bool(u.SetWindowPos(wt.HWND(hwnd), wt.HWND(1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x4000))
        info["foreground"] = u.GetForegroundWindow() == hwnd
    except Exception as exc:
        info["error"] = str(exc)
    return info
