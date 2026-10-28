"""Game process lifecycle: launch, attach, status, kill.

A single module-level :class:`Session` describes the game instance the MCP
server is currently working with.  Lifecycle state is intentionally kept in
memory: launching a new game replaces the previous session.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import window

log = logging.getLogger(__name__)

# Windows NTSTATUS values commonly seen as process exit codes.
EXIT_CODE_NAMES: dict[int, str] = {
    0x00000000: "OK",
    0xC0000005: "ACCESS_VIOLATION",
    0xC00000FD: "STACK_OVERFLOW",
    0xC0000409: "STACK_BUFFER_OVERRUN",
    0xC0000374: "HEAP_CORRUPTION",
    0x80000003: "BREAKPOINT",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC0000094: "INT_DIVIDE_BY_ZERO",
    0xC0000096: "PRIVILEGED_INSTRUCTION",
    0xC0000017: "NO_MEMORY",
    0xC0000018: "CONFLICTING_ADDRESSES",
    0xC0000135: "DLL_NOT_FOUND",
    0xC0000139: "ENTRYPOINT_NOT_FOUND",
    0xC0000142: "DLL_INIT_FAILED",
    0xE06D7363: "C++ EXCEPTION",
    0xC0000602: "FAIL_FAST_EXCEPTION",
    0xC0000420: "ASSERTION_FAILURE",
    0x80000002: "DATATYPE_MISALIGNMENT",
}
CRASH_CODES = set(EXIT_CODE_NAMES) - {0x00000000}

REFORGED_EXE_REL = r"_retail_\x86_64\Warcraft III.exe"


@dataclass
class Session:
    pid: int | None = None
    hwnd: int | None = None
    exe: str | None = None
    map_path: str | None = None
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    out_dir: Path | None = None
    popen: subprocess.Popen | None = None
    flavor: str | None = None
    exit_code: int | None = None
    args: list[str] = field(default_factory=list)
    # Handle to the game process opened at launch/attach time. Windows keeps
    # the exit code readable through it after the process dies, which psutil
    # cannot do for a process we did not spawn (attach, Reforged re-spawn).
    proc_handle: int | None = None


_session: Session | None = None

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259


def _open_process_handle(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, 0, int(pid))
        return int(h) if h else None
    except Exception:
        return None


def _handle_exit_code(handle: int | None) -> int | None:
    """Exit code via a held process handle, or None while still running."""
    if not handle:
        return None
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        code = ctypes.c_uint32(0)
        if not k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return None
        return None if code.value == _STILL_ACTIVE else int(code.value)
    except Exception:
        return None


def get_session() -> Session | None:
    return _session


def set_session(session: Session | None) -> None:
    global _session
    _session = session


def _runs_root() -> Path:
    env = os.environ.get("WC3_TESTER_OUT")
    if env:
        return Path(os.path.expandvars(os.path.expanduser(env)))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "wc3-tester" / "runs"
    return Path.home() / "wc3-tester" / "runs"


def out_dir() -> Path:
    """Session output directory (created on demand)."""
    if _session and _session.out_dir:
        path = Path(_session.out_dir)
    else:
        if _session and _session.start_time:
            name = _session.start_time.strftime("%Y%m%d-%H%M%S")
        else:
            name = "attached"
        path = _runs_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def exe_candidates() -> list[str]:
    """Common Warcraft III install locations, most likely first."""
    candidates = [
        rf"C:\Program Files (x86)\Warcraft III\{REFORGED_EXE_REL}",
        r"C:\Program Files (x86)\Warcraft III\Warcraft III.exe",
        r"C:\Program Files (x86)\Warcraft III\war3.exe",
        r"C:\Program Files\Warcraft III\_retail_\x86_64\Warcraft III.exe",
        r"C:\Program Files\Warcraft III\Warcraft III.exe",
        r"C:\Program Files\Warcraft III\war3.exe",
        r"C:\Program Files (x86)\Warcraft III\Frozen Throne.exe",
    ]
    expanded: list[str] = []
    for path in candidates:
        path = os.path.expandvars(path)
        if path not in expanded:
            expanded.append(path)
    return expanded


def resolve_exe(exe: str | None = None) -> str:
    """Find the game executable: explicit arg, then WC3_EXE, then defaults."""
    candidates: list[str] = []
    if exe:
        candidates.append(exe)
    env_exe = os.environ.get("WC3_EXE")
    if env_exe:
        candidates.append(env_exe)
    candidates.extend(exe_candidates())
    for candidate in candidates:
        path = os.path.expandvars(os.path.expanduser(candidate.strip().strip('"')))
        if path and os.path.isfile(path):
            return str(Path(path).resolve())
    raise FileNotFoundError(
        "Warcraft III executable not found. Set the WC3_EXE environment variable "
        "or pass exe=... . Tried: " + "; ".join(candidates)
    )


def flavor_for(exe: str, flavor: str = "auto") -> str:
    flavor = (flavor or "auto").lower()
    if flavor in ("reforged", "classic"):
        return flavor
    return "reforged" if "_retail_" in exe.lower() else "classic"


def build_args(
    exe: str,
    map_path: str | None = None,
    windowed: bool = True,
    nowfpause: bool = True,
    flavor: str = "auto",
    extra_args: list[str] | None = None,
) -> list[str]:
    if flavor_for(exe, flavor) == "reforged":
        args: list[str] = ["-launch"]
        if windowed:
            args += ["-windowmode", "windowed"]
        if nowfpause:
            args.append("-nowfpause")
        if map_path:
            args += ["-loadfile", str(Path(map_path).resolve())]
    else:
        args = []
        if windowed:
            args.append("-window")
        if nowfpause:
            args.append("-nowfpause")
        if map_path:
            args += ["-loadfile", str(Path(map_path).resolve())]
    if extra_args:
        args.extend(str(a) for a in extra_args)
    return args


def _new_window_after(started_ts: float) -> int | None:
    """Fallback for Reforged, which re-spawns the real game process."""
    import psutil

    for info in window.list_windows():
        pid = info.get("pid") or 0
        if not pid:
            continue
        if (info.get("process") or "").lower() in window.EXCLUDED_PROCESS_NAMES:
            continue
        matches = (
            (info.get("class_name") or "").lower() == "warcraft iii"
            or (info.get("process") or "").lower() in window.GAME_PROCESS_NAMES
            or "warcraft iii" in (info.get("title") or "").lower()
        )
        if not matches:
            continue
        try:
            create_time = psutil.Process(pid).create_time()
        except Exception:
            create_time = 0
        if create_time >= started_ts - 10:
            return int(info["hwnd"])
    return None


def launch(
    exe: str | None = None,
    map_path: str | None = None,
    windowed: bool = True,
    nowfpause: bool = True,
    extra_args: list[str] | None = None,
    flavor: str = "auto",
    wait_window_sec: int = 90,
) -> dict:
    """Start Warcraft III and wait for its window."""
    from . import winapi

    winapi.enable_dpi_awareness()
    exe_path = resolve_exe(exe)
    exe_dir = str(Path(exe_path).parent)
    resolved_flavor = flavor_for(exe_path, flavor)

    if map_path:
        map_path = str(Path(os.path.expandvars(os.path.expanduser(map_path))).resolve())
        if not os.path.isfile(map_path):
            raise FileNotFoundError(f"Map file not found: {map_path}")

    args = build_args(
        exe_path,
        map_path=map_path,
        windowed=windowed,
        nowfpause=nowfpause,
        flavor=resolved_flavor,
        extra_args=extra_args,
    )

    started = datetime.now(timezone.utc)
    started_ts = time.time()
    out = _runs_root() / started.strftime("%Y%m%d-%H%M%S")
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    log.info("Launching %s %s (cwd=%s)", exe_path, args, exe_dir)
    popen = subprocess.Popen(
        [exe_path, *args],
        cwd=exe_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

    deadline = time.time() + max(5, int(wait_window_sec))
    hwnd: int | None = None
    last_error = ""
    while time.time() < deadline:
        if popen.poll() is None:
            try:
                candidate = window.find_window(pid=popen.pid)
                if candidate:
                    candidate_pid = window.window_pid(candidate)
                    if (
                        window.process_name_of(candidate_pid)
                        not in window.EXCLUDED_PROCESS_NAMES
                    ):
                        hwnd = candidate
            except Exception as exc:
                last_error = str(exc)
        if not hwnd:
            # Also covers the Reforged re-spawn / dead launcher case.
            hwnd = _new_window_after(started_ts)
        if hwnd:
            break
        time.sleep(0.5)

    if not hwnd:
        code = popen.poll()
        detail = ""
        if code is not None:
            code_u = code & 0xFFFFFFFF
            detail = f"; launcher exited with 0x{code_u:08X} ({exit_code_name(code_u)})"
        raise RuntimeError(
            "Warcraft III window did not appear within "
            f"{wait_window_sec}s{detail}. {last_error}".strip()
        )

    pid = window.window_pid(hwnd) or popen.pid
    session = Session(
        pid=pid,
        hwnd=hwnd,
        exe=exe_path,
        map_path=map_path,
        start_time=started,
        out_dir=out,
        popen=popen,
        flavor=resolved_flavor,
        args=args,
        proc_handle=_open_process_handle(pid),
    )
    set_session(session)
    log.info("Launched pid=%s hwnd=%s", pid, hwnd)
    return {
        "pid": pid,
        "hwnd": hwnd,
        "args": args,
        "out_dir": str(out),
        "exe": exe_path,
        "map_path": map_path,
        "flavor": resolved_flavor,
        "launcher_pid": popen.pid,
        "window": window.describe(hwnd),
    }


def attach(pid: int | None = None, title: str | None = None) -> dict:
    """Attach to an already-running Warcraft III instance."""
    from . import winapi

    winapi.enable_dpi_awareness()
    hwnd = window.find_window(pid=pid, title=title)
    if not hwnd:
        raise RuntimeError(
            f"No Warcraft III window found (pid={pid!r}, title={title!r}). "
            "Start the game first or pass a different pid/title."
        )
    actual_pid = window.window_pid(hwnd)
    exe_path: str | None = None
    created: datetime | None = None
    try:
        import psutil

        proc = psutil.Process(actual_pid)
        exe_path = proc.exe()
        created = datetime.fromtimestamp(proc.create_time(), tz=timezone.utc)
    except Exception:
        pass
    out = _runs_root() / "attached"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    session = Session(
        pid=actual_pid,
        hwnd=hwnd,
        exe=exe_path,
        start_time=created or datetime.now(timezone.utc),
        out_dir=out,
        flavor="reforged" if exe_path and "_retail_" in exe_path.lower() else "classic",
        proc_handle=_open_process_handle(actual_pid),
    )
    set_session(session)
    log.info("Attached to pid=%s hwnd=%s", actual_pid, hwnd)
    return {
        "pid": actual_pid,
        "hwnd": hwnd,
        "exe": exe_path,
        "out_dir": str(out),
        "window": window.describe(hwnd),
    }


def exit_code_name(code: int | None) -> str:
    if code is None:
        return "UNKNOWN"
    code_u = int(code) & 0xFFFFFFFF
    return EXIT_CODE_NAMES.get(code_u, f"0x{code_u:08X}")


def is_crash_code(code: int | None) -> bool:
    if code is None:
        return False
    return (int(code) & 0xFFFFFFFF) in CRASH_CODES


def _process_info(pid: int | None) -> tuple[bool, int | None, float | None, float | None, float | None]:
    """Return (running, exit_code, create_time, cpu_percent, rss_bytes)."""
    if not pid:
        return False, None, None, None, None
    try:
        import psutil

        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return False, None, None, None, None
        except psutil.AccessDenied:
            # Cannot inspect it, but the process exists.
            return True, None, None, None, None
        status = proc.status()
        if status in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
            running = False
        else:
            running = proc.is_running()
        if running:
            try:
                cpu = proc.cpu_percent(interval=None)
            except Exception:
                cpu = None
            try:
                rss = float(proc.memory_info().rss)
            except Exception:
                rss = None
            return True, None, float(proc.create_time()), cpu, rss
        # Not running: reap the exit code if psutil still knows it.
        code: int | None = None
        try:
            code = int(proc.wait(timeout=0))
        except Exception:
            try:
                code = int(proc.returncode) if proc.returncode is not None else None
            except Exception:
                code = None
        return False, code, float(proc.create_time()), None, None
    except Exception:
        return False, None, None, None, None


def status() -> dict:
    """Full lifecycle status, JSON-serialisable and never raises."""
    from . import crash  # local import avoids an import cycle

    session = _session
    if session is None:
        return {
            "state": "not_started",
            "running": False,
            "pid": None,
            "hwnd": None,
            "hint": "Call wc3_launch or wc3_attach first.",
        }

    pid = session.pid
    running, code, create_time, cpu, rss = _process_info(pid)

    # A stale window handle is refreshed when possible.
    hwnd = session.hwnd
    if hwnd and not window.is_window(hwnd):
        hwnd = window.find_window(pid=pid) if pid else None
        session.hwnd = hwnd

    if not running and hwnd and window.is_window(hwnd) and window.window_pid(hwnd) == pid:
        # psutil was unable to inspect the process but its window still exists.
        running = True
        code = None

    if code is not None:
        session.exit_code = code
    elif not running and session.exit_code is not None:
        code = session.exit_code
    elif not running and _handle_exit_code(session.proc_handle) is not None:
        code = _handle_exit_code(session.proc_handle)
        session.exit_code = code
    elif not running and session.popen is not None and session.popen.pid == pid:
        poll = session.popen.poll()
        if poll is not None:
            code = int(poll)
            session.exit_code = code

    fatal_dialog = None
    try:
        fatal_dialog = crash.find_fatal_dialog(pid)
    except Exception as exc:
        log.debug("fatal dialog lookup failed: %s", exc)

    hung = False
    if running and hwnd:
        try:
            hung = window.is_hung(hwnd)
        except Exception:
            hung = False

    if not running:
        state = "crashed" if (is_crash_code(code) or fatal_dialog) else "exited"
    elif hung:
        state = "hung"
    else:
        state = "running"

    code_u = (int(code) & 0xFFFFFFFF) if code is not None else None
    now_ts = time.time()
    uptime = None
    if create_time is not None:
        uptime = max(0.0, now_ts - create_time)
    elif session.start_time:
        uptime = max(0.0, now_ts - session.start_time.timestamp())

    title = ""
    client = [0, 0]
    if hwnd:
        try:
            title = window.window_title(hwnd)
            client = list(window.client_size(hwnd))
        except Exception:
            pass

    return {
        "state": state,
        "running": bool(running),
        "pid": pid,
        "hwnd": hwnd,
        "exe": session.exe,
        "map_path": session.map_path,
        "flavor": session.flavor,
        "start_time": session.start_time.isoformat() if session.start_time else None,
        "out_dir": str(session.out_dir) if session.out_dir else None,
        "exit_code": code_u,
        "exit_code_hex": f"0x{code_u:08X}" if code_u is not None else None,
        "exit_code_name": exit_code_name(code) if code_u is not None else None,
        "hung": hung,
        "uptime_sec": round(uptime, 1) if uptime is not None else None,
        "cpu_percent": round(cpu, 1) if cpu is not None else None,
        "memory_rss_mb": round(rss / (1024 * 1024), 1) if rss else None,
        "window_title": title,
        "client_size": client,
        "fatal_dialog": fatal_dialog,
    }


def kill() -> dict:
    """Terminate the game process tree and return the final status."""
    session = _session
    if session is None or not session.pid:
        return {"killed": False, "reason": "no session", **status()}

    killed: list[int] = []
    errors: list[str] = []
    try:
        import psutil

        proc = psutil.Process(session.pid)
        procs = proc.children(recursive=True) + [proc]
        if (
            session.popen is not None
            and session.popen.pid != session.pid
            and session.popen.poll() is None
        ):
            try:
                procs.append(psutil.Process(session.popen.pid))
            except Exception:
                pass
        # De-duplicate while preserving order.
        seen: set[int] = set()
        procs = [p for p in procs if not (p.pid in seen or seen.add(p.pid))]
        for child in procs:
            try:
                child.terminate()
                killed.append(child.pid)
            except Exception as exc:
                errors.append(f"terminate {child.pid}: {exc}")
        _gone, alive = psutil.wait_procs(procs, timeout=3)
        for leftover in alive:
            try:
                leftover.kill()
            except Exception as exc:
                errors.append(f"kill {leftover.pid}: {exc}")
        if alive:
            psutil.wait_procs(alive, timeout=3)
        if session.popen is not None:
            try:
                session.popen.poll()
            except Exception:
                pass
    except Exception as exc:
        errors.append(str(exc))

    time.sleep(0.2)
    final = status()
    final["killed"] = bool(killed)
    final["killed_pids"] = killed
    if errors:
        final["kill_errors"] = errors
    return final
