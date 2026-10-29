"""Game process lifecycle: launch, attach, status, kill.

Several game instances can be registered at once (LAN multi-player testing).
Each :class:`Session` is stored in an insertion-ordered registry keyed by a
short instance id (``"p1"``, ``"p2"``, ...); ``_current`` names the instance
the MCP tools act on when no explicit ``instance`` is passed.  Lifecycle state
is intentionally kept in memory.
"""

from __future__ import annotations

import functools
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
    instance: str = "p1"
    lan_ip: str | None = None
    player_name: str | None = None
    lan_binding: str | None = None  # "builtin" or "wrapper"
    bind_hook: dict | None = None
    background: bool = True
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


_instances: dict[str, Session] = {}
_current: str | None = None
_launching = 0  # launches in progress, see busy_count()

# Session states that mean "no live process anymore".
_DEAD_STATES = {"exited", "crashed", "not_started"}

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


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _known_ids() -> str:
    return ", ".join(repr(name) for name in _instances) or "none"


def get_session(instance: str | None = None) -> Session | None:
    """The session for *instance*, or the current one when *instance* is None.

    With no current instance set, a lone registered instance is returned.
    An unknown explicit id raises ``KeyError``.
    """
    if instance is None:
        if _current is not None and _current in _instances:
            return _instances[_current]
        if len(_instances) == 1:
            return next(iter(_instances.values()))
        return None
    try:
        return _instances[instance]
    except KeyError:
        raise KeyError(
            f"unknown game instance {instance!r} (known instances: {_known_ids()})"
        ) from None


def set_session(session: Session | None) -> None:
    """Register *session* under its ``instance`` id and make it current."""
    global _current
    if session is None:
        _current = None
        return
    _instances[session.instance] = session
    _current = session.instance


def _instance_state(name: str) -> str:
    try:
        return str(status(name).get("state") or "unknown")
    except Exception:
        return "unknown"


def _instance_running(name: str) -> bool:
    try:
        state = status(name)
    except Exception:
        return False
    if state.get("running"):
        return True
    return state.get("state") in ("running", "hung")


def next_instance_id() -> str:
    """First unused ``p1``, ``p2``, ... id.

    Dead registry entries are dropped first, but only when nothing is running
    at all: while a game is alive, dead entries are kept so their crash
    reports stay addressable.
    """
    global _current
    if not any(_instance_running(name) for name in list(_instances)):
        for name in list(_instances):
            if _instance_state(name) in _DEAD_STATES:
                _instances.pop(name, None)
                if _current == name:
                    _current = None
    number = 1
    while f"p{number}" in _instances:
        number += 1
    return f"p{number}"


def list_instances() -> list[dict]:
    """All registered instances, in registration order."""
    result: list[dict] = []
    for name, session in list(_instances.items()):
        result.append(
            {
                "instance": name,
                "pid": session.pid,
                "hwnd": session.hwnd,
                "player_name": session.player_name,
                "lan_ip": session.lan_ip,
                "lan_binding": session.lan_binding,
                "current": name == _current,
                "state": _instance_state(name),
            }
        )
    return result


def busy_count() -> int:
    """Live instances plus launches in progress, for the shared server.

    Uses only process handles (no window messages), so it is fast and cannot
    block on a busy or hung game.
    """
    alive = 0
    for session in list(_instances.values()):
        if session.proc_handle:
            alive += _handle_exit_code(session.proc_handle) is None
        elif session.pid:
            alive += bool(_process_info(session.pid)[0])
    return alive + _launching


def _counted_launch(fn):
    """Count *fn* in busy_count() while it runs: a launch registers its
    instance only at the end, and the shared server must not be replaced
    in between."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _launching
        _launching += 1
        try:
            return fn(*args, **kwargs)
        finally:
            _launching -= 1

    return wrapper


def select(instance: str) -> dict:
    """Make *instance* the current one and return its status."""
    global _current
    get_session(instance)  # validates, raises KeyError for unknown ids
    _current = instance
    return status(instance)


def _runs_root() -> Path:
    env = os.environ.get("WC3_TESTER_OUT")
    if env:
        return Path(os.path.expandvars(os.path.expanduser(env)))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "wc3-tester" / "runs"
    return Path.home() / "wc3-tester" / "runs"


def out_dir(instance: str | None = None) -> Path:
    """Per-instance output directory (created on demand).

    ``<runs_root>/<start-timestamp>-<instance>`` for launched games and
    ``<runs_root>/attached-<instance>`` for attached ones.
    """
    session = get_session(instance)
    if session and session.out_dir:
        path = Path(session.out_dir)
    else:
        if session and session.start_time:
            name = f"{session.start_time.strftime('%Y%m%d-%H%M%S')}-{session.instance}"
        else:
            name = f"attached-{instance}" if instance else "attached"
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


def _window_for_pid(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        candidate = window.find_window(pid=pid)
        if candidate:
            candidate_pid = window.window_pid(candidate)
            if (
                window.process_name_of(candidate_pid)
                not in window.EXCLUDED_PROCESS_NAMES
            ):
                return candidate
    except Exception:
        pass
    return None


def _new_window_after(
    started_ts: float, exclude_pids: set[int] | None = None
) -> int | None:
    """Fallback for Reforged, which re-spawns the real game process.

    Windows owned by the pids of other registered instances are ignored so a
    new instance cannot accidentally grab a sibling's window.
    """
    import psutil

    exclude = set(exclude_pids or ())
    for info in window.list_windows():
        pid = info.get("pid") or 0
        if not pid or int(pid) in exclude:
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


def _wait_for_window(
    pid: int | None,
    popen: subprocess.Popen | None,
    started_ts: float,
    wait_window_sec: int,
    exclude_pids: set[int] | None = None,
) -> tuple[int | None, str]:
    deadline = time.time() + max(5, int(wait_window_sec))
    hwnd: int | None = None
    last_error = ""
    while time.time() < deadline:
        try:
            hwnd = _window_for_pid(pid)
        except Exception as exc:
            last_error = str(exc)
        if not hwnd:
            # Also covers the Reforged re-spawn / dead launcher case.
            hwnd = _new_window_after(started_ts, exclude_pids=exclude_pids)
        if hwnd:
            break
        time.sleep(0.5)
    return hwnd, last_error


def _terminate_tree(
    pid: int | None, popen: subprocess.Popen | None = None
) -> tuple[list[int], list[str]]:
    """Terminate a game process tree; never raises."""
    killed: list[int] = []
    errors: list[str] = []
    if not pid:
        return killed, errors
    try:
        import psutil

        proc = psutil.Process(int(pid))
        procs = proc.children(recursive=True) + [proc]
        if (
            popen is not None
            and popen.pid != pid
            and popen.poll() is None
        ):
            try:
                procs.append(psutil.Process(popen.pid))
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
        if popen is not None:
            try:
                popen.poll()
            except Exception:
                pass
    except Exception as exc:
        errors.append(str(exc))
    return killed, errors


@_counted_launch
def launch(
    exe: str | None = None,
    map_path: str | None = None,
    windowed: bool = True,
    nowfpause: bool = True,
    extra_args: list[str] | None = None,
    flavor: str = "auto",
    wait_window_sec: int = 90,
    instance: str | None = None,
    background: bool = True,
    maximized: bool = True,
    lan_ip: str | None = None,
    player_name: str | None = None,
    wrapper: str | None = None,
) -> dict:
    """Start Warcraft III and wait for its window (one process per instance).

    With ``lan_ip`` the instance joins the LAN discovery relay.  By default its
    sockets are forced onto that address by the built-in bind hook
    (:mod:`bindhook`), which is the part ForceBindIP would otherwise do.  A
    configured launch wrapper (``wrapper`` / ``WC3_LAUNCH_WRAPPER`` /
    ``wc3_set_launch_wrapper``) is used instead when present.
    """
    from . import lanrelay, launcher
    from . import winapi

    winapi.enable_dpi_awareness()

    target_instance = instance or next_instance_id()
    if _instance_running(target_instance):
        raise RuntimeError(
            f"instance {target_instance} is still running; "
            "kill it first or pick another id"
        )

    wrapper_cmd = launcher.launch_wrapper(wrapper) if lan_ip else None
    lan_binding = "wrapper" if wrapper_cmd else ("builtin" if lan_ip else None)

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
    out = _runs_root() / f"{started.strftime('%Y%m%d-%H%M%S')}-{target_instance}"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    exclude_pids = {
        int(session.pid)
        for name, session in _instances.items()
        if name != target_instance and session.pid
    }

    log.info(
        "Launching instance %s: %s %s (cwd=%s, background=%s)",
        target_instance,
        exe_path,
        args,
        exe_dir,
        background,
    )
    released = launcher.release_single_instance()
    early_hook = None
    with launcher.lan_identity(player_name):
        start_pid, popen = launcher.start_process(
            exe_path, args, exe_dir, background, wrapper=wrapper_cmd, ip=lan_ip
        )
        if lan_binding == "builtin" and resolved_flavor == "classic":
            # Classic WC3 may create its UDP 6112 socket while the window is
            # still being created.  Try to get the hook in as early as
            # possible; if the process is not ready yet, the post-window
            # install below is the authoritative attempt.
            from . import bindhook

            try:
                early_hook = bindhook.install(start_pid, lan_ip, timeout=10)
                log.info("early bind hook installed in pid %s", start_pid)
            except Exception as exc:
                log.info("early bind hook for pid %s not ready: %s", start_pid, exc)
        hwnd, last_error = _wait_for_window(
            start_pid,
            popen,
            started_ts,
            wait_window_sec,
            exclude_pids=exclude_pids,
        )

    if not hwnd:
        if lan_binding == "builtin":
            from . import bindhook

            if early_hook is not None:
                try:
                    early_hook.uninstall()
                except Exception:
                    pass
            bindhook.forget(start_pid)
        code = popen.poll() if popen is not None else None
        detail = ""
        if code is not None:
            code_u = code & 0xFFFFFFFF
            detail = f"; launcher exited with 0x{code_u:08X} ({exit_code_name(code_u)})"
        raise RuntimeError(
            "Warcraft III window did not appear within "
            f"{wait_window_sec}s{detail}. {last_error}".strip()
        )

    placement = launcher.place_window(hwnd, maximized, to_back=background)

    pid = window.window_pid(hwnd) or start_pid
    if popen is not None and popen.pid != pid:
        popen = None  # the wrapper, not the game
    bind_info: dict | None = None
    if lan_binding == "builtin":
        from . import bindhook

        if pid != start_pid:
            # An early hook on a launcher is useless once the real game runs;
            # restore its imports before targeting the game process.
            if early_hook is not None:
                try:
                    early_hook.uninstall()
                except Exception:
                    pass
            bindhook.forget(start_pid)
        try:
            bind_info = bindhook.install(pid, lan_ip, timeout=20).info()
        except Exception as exc:
            _terminate_tree(pid, popen)
            if pid != start_pid:
                bindhook.forget(start_pid)
            raise RuntimeError(
                f"Could not force instance {target_instance} onto LAN address "
                f"{lan_ip!r}: {exc}"
            ) from exc

    lan: dict | None = None
    if lan_ip:
        try:
            lanrelay.relay().add(lan_ip)
            lan = {"binding": lan_binding, "relay": True}
            if wrapper_cmd:
                lan["wrapper"] = wrapper_cmd.format(ip=lan_ip)
            if bind_info:
                lan["bind_hook"] = bind_info
        except Exception as exc:
            _terminate_tree(pid, popen)
            if bind_info:
                from . import bindhook

                bindhook.forget(pid)
            raise RuntimeError(
                f"Could not add LAN address {lan_ip!r} of instance "
                f"{target_instance} to the discovery relay: {exc}"
            ) from exc

    session = Session(
        instance=target_instance,
        lan_ip=lan_ip,
        player_name=player_name,
        lan_binding=lan_binding,
        bind_hook=bind_info,
        background=background,
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
    log.info("Launched instance %s pid=%s hwnd=%s", target_instance, pid, hwnd)
    return {
        "instance": target_instance,
        "pid": pid,
        "hwnd": hwnd,
        "args": args,
        "out_dir": str(out),
        "exe": exe_path,
        "map_path": map_path,
        "flavor": resolved_flavor,
        "launcher_pid": start_pid,
        "released_single_instance_markers": released,
        "window": window.describe(hwnd),
        "background": background,
        "maximized": maximized,
        "lan_ip": lan_ip,
        "player_name": player_name,
        "lan_binding": lan_binding,
        "bind_hook": bind_info,
        "placement": placement,
        "lan": lan,
    }


def launch_lan(
    count: int = 2,
    exe: str | None = None,
    map_path: str | None = None,
    names: list[str] | None = None,
    base_ip: str = "127.0.0.",
    background: bool = True,
    maximized: bool = True,
    wait_window_sec: int = 90,
    wrapper: str | None = None,
) -> dict:
    """Launch ``count`` instances that can see each other's LAN games.

    Instances are ``p1``..``pN``; ``pK`` gets LAN address ``<base_ip>K`` and
    player name ``names[K-1]`` (default ``"PK"``).  Each instance's Winsock
    sockets are forced onto its address by :mod:`bindhook` unless an external
    launch wrapper is configured.  ``map_path`` is passed to the first instance
    (the host) only.  Stops at the first failure and reports which instances
    did start.
    """
    try:
        count = int(count)
    except (TypeError, ValueError) as exc:
        raise ValueError("count must be an integer between 2 and 12") from exc
    if count < 2 or count > 12:
        raise ValueError("count must be between 2 and 12")
    if names is not None and not isinstance(names, (list, tuple)):
        raise ValueError("names must be a list of player names or None")

    started: list[dict] = []
    for k in range(1, count + 1):
        target_instance = f"p{k}"
        raw_name = (
            names[k - 1]
            if names is not None and len(names) >= k
            else None
        )
        player_name = str(raw_name) if raw_name else f"P{k}"
        try:
            info = launch(
                exe=exe,
                map_path=map_path if k == 1 else None,
                instance=target_instance,
                background=background,
                maximized=maximized,
                lan_ip=f"{base_ip}{k}",
                player_name=player_name,
                wait_window_sec=wait_window_sec,
                wrapper=wrapper,
            )
        except Exception as exc:
            log.error("launch_lan failed at %s: %s", target_instance, exc)
            return {
                "ok": False,
                "error": str(exc),
                "failed_instance": target_instance,
                "started_count": len(started),
                "instances": started,
            }
        started.append(info)

    return {"ok": True, "count": len(started), "instances": started}


def attach(
    pid: int | None = None,
    title: str | None = None,
    instance: str | None = None,
) -> dict:
    """Attach to an already-running Warcraft III instance."""
    from . import winapi

    winapi.enable_dpi_awareness()
    target_instance = instance or next_instance_id()
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
    out = _runs_root() / f"attached-{target_instance}"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    session = Session(
        instance=target_instance,
        background=False,
        pid=actual_pid,
        hwnd=hwnd,
        exe=exe_path,
        start_time=created or datetime.now(timezone.utc),
        out_dir=out,
        flavor="reforged" if exe_path and "_retail_" in exe_path.lower() else "classic",
        proc_handle=_open_process_handle(actual_pid),
    )
    set_session(session)
    log.info("Attached instance %s pid=%s hwnd=%s", target_instance, actual_pid, hwnd)
    return {
        "instance": target_instance,
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


def status(instance: str | None = None) -> dict:
    """Full lifecycle status of one instance, JSON-serialisable, never raises."""
    from . import crash  # local import avoids an import cycle

    session = get_session(instance)
    name = instance or (session.instance if session else None)
    if session is None:
        return {
            "instance": name,
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
        fatal_dialog = crash.find_fatal_dialog(
            pid,
            # Never another registered instance's window.
            exclude_pids={int(s.pid) for n, s in _instances.items() if s.pid and n != session.instance},
            # Another process's dialog (a crash reporter) only once the game is
            # gone, and only from a process started after this game.
            foreign_since=None if running else (create_time or session.start_time.timestamp()),
        )
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

    bind_info = session.bind_hook
    if session.lan_binding == "builtin" and session.pid:
        try:
            from . import bindhook

            live = bindhook.status(session.pid)
            if live:
                bind_info = live
        except Exception:
            pass

    title = ""
    client = [0, 0]
    if hwnd:
        try:
            title = window.window_title(hwnd)
            client = list(window.client_size(hwnd))
        except Exception:
            pass

    return {
        "instance": session.instance,
        "state": state,
        "running": bool(running),
        "pid": pid,
        "hwnd": hwnd,
        "exe": session.exe,
        "map_path": session.map_path,
        "flavor": session.flavor,
        "start_time": session.start_time.isoformat() if session.start_time else None,
        "out_dir": str(session.out_dir) if session.out_dir else None,
        "lan_ip": session.lan_ip,
        "player_name": session.player_name,
        "lan_binding": session.lan_binding,
        "bind_hook": bind_info,
        "background": session.background,
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


def kill(instance: str | None = None) -> dict:
    """Terminate one instance's process tree and return the final status."""
    session = get_session(instance)
    name = instance or (session.instance if session else None)
    if session is None or not session.pid:
        return {
            "instance": name,
            "killed": False,
            "reason": "no session",
            **status(instance),
        }

    killed, errors = _terminate_tree(session.pid, session.popen)
    if session.lan_ip:
        from . import bindhook, lanrelay

        bindhook.forget(session.pid)
        lanrelay.relay().remove(session.lan_ip)

    time.sleep(0.2)
    final = status(instance)
    final["instance"] = name or final.get("instance")
    final["killed"] = bool(killed)
    final["killed_pids"] = killed
    if errors:
        final["kill_errors"] = errors
    return final


def kill_all() -> list[dict]:
    """Terminate every registered instance and return their final statuses."""
    results: list[dict] = []
    for name in list(_instances):
        try:
            results.append(kill(name))
        except Exception as exc:
            results.append(
                {"instance": name, "killed": False, "error": str(exc)}
            )
    return results
