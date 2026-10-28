"""Checks for the built-in per-process socket bind hook (``bindhook.py``).

The first half is pure PE parsing.  The rest injects the hook into:

* a 64-bit Python socket probe (``tests/bind_probe.py``), and
* a small 32-bit C probe (``tests/native/bind_probe.c``) when an x86 MSVC
  toolchain is available.  That 32-bit case is the classic WC3 path.

Run on Windows:  python tests\\test_bindhook.py
Exit code 0 = all checks passed (skips are not failures).
"""

from __future__ import annotations

import glob
import os
import queue
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from wc3_tester import bindhook  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(condition)


def skip(name: str, why: str) -> None:
    print(f"[SKIP] {name} — {why}")


def readline(proc: subprocess.Popen, timeout: float = 10.0) -> str:
    box: queue.Queue[str] = queue.Queue()

    def read() -> None:
        try:
            box.put(proc.stdout.readline() if proc.stdout else "")
        except Exception as exc:  # pragma: no cover - defensive
            box.put(f"<read error: {exc}>")

    threading.Thread(target=read, daemon=True).start()
    try:
        return box.get(timeout=timeout).strip()
    except queue.Empty:
        return "<timeout>"


def find_vcvarsall() -> str | None:
    roots = [
        r"C:\Program Files\Microsoft Visual Studio",
        r"C:\Program Files (x86)\Microsoft Visual Studio",
    ]
    for root in roots:
        pattern = os.path.join(root, "**", "VC", "Auxiliary", "Build", "vcvarsall.bat")
        found = glob.glob(pattern, recursive=True)
        if found:
            return sorted(found)[-1]
    return None


def build_x86_probe(folder: Path) -> Path | None:
    vcvarsall = find_vcvarsall()
    if not vcvarsall:
        return None
    out = folder / "bind_probe32.exe"
    source = ROOT / "tests" / "native" / "bind_probe.c"
    if not source.is_file():
        return None
    script = folder / "build_probe.bat"
    script.write_text(
        "@echo off\r\n"
        f'call "{vcvarsall}" x86 >nul\r\n'
        f'cl /nologo /W3 /O2 "{source}" /Fo:"{folder / "bind_probe.obj"}" /Fe:"{out}" /link ws2_32.lib\r\n',
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["cmd", "/c", str(script)], cwd=str(folder), capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0 or not out.is_file():
        print(proc.stdout[-1000:])
        print(proc.stderr[-1000:])
        return None
    return out


def test_pe_parsing() -> None:
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    ws2 = system / "System32" / "ws2_32.dll"
    ws2_wow = system / "SysWOW64" / "ws2_32.dll"
    if ws2.is_file():
        names = bindhook.export_ordinal_names(str(ws2))
        check("x64 ws2_32 exports map ordinals for bind",
              names.get(2) == "bind", str(names))
        check("x64 ws2_32 exports map ordinals for sendto",
              names.get(20) == "sendto", str(names))
    else:
        skip("x64 ws2_32 export parsing", "not present")
    if ws2_wow.is_file():
        names = bindhook.export_ordinal_names(str(ws2_wow))
        check("x86 ws2_32 exports map ordinals for bind",
              names.get(2) == "bind", str(names))
        wsock = bindhook.export_ordinal_names(str(system / "SysWOW64" / "wsock32.dll"))
        check("x86 wsock32 exports map ordinals for bind",
              wsock.get(2) == "bind", str(wsock))
    else:
        skip("x86 ws2_32 export parsing", "not present")

    import _socket

    ordinal_maps = {
        "ws2_32.dll": bindhook.export_ordinal_names(str(ws2)),
        "wsock32.dll": bindhook.export_ordinal_names(str(system / "System32" / "wsock32.dll")),
    }
    slots = bindhook.import_slots(_socket.__file__, ordinal_maps=ordinal_maps)
    check("ordinal import slots of _socket.pyd",
          bool(slots.get("bind")) and bool(slots.get("sendto")), str(slots))
    expected_machine = (
        bindhook.IMAGE_FILE_MACHINE_AMD64
        if struct.calcsize("P") == 8
        else bindhook.IMAGE_FILE_MACHINE_I386
    )
    check("PE machine matches the running interpreter",
          bindhook.pe_machine(_socket.__file__) == expected_machine)


def run_python_probe(folder: Path) -> None:
    probe = ROOT / "tests" / "bind_probe.py"
    # In a venv, Scripts\python.exe is a launcher; hook the real interpreter.
    python = getattr(sys, "_base_executable", sys.executable)
    proc = subprocess.Popen(
        [python, "-u", str(probe)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    try:
        ready = readline(proc)
        check("x64 probe started", '"ready": true' in ready, ready)
        hook = bindhook.install(proc.pid, "127.0.0.9", timeout=20)
        check("x64 hook installed", hook.arch == "x64" and hook.patched >= 2, str(hook.info()))
        proc.stdin.write("bind\n")
        proc.stdin.flush()
        bound = readline(proc)
        check("x64 bind was forced to 127.0.0.9", '"bound": "127.0.0.9"' in bound, bound)
        proc.stdin.write("send\n")
        proc.stdin.flush()
        sent = readline(proc)
        check("x64 unbound sendto was pre-bound",
              '"source_ip": "127.0.0.9"' in sent, sent)
        proc.stdin.write("connect\n")
        proc.stdin.flush()
        connected = readline(proc)
        check("x64 unbound connect was pre-bound",
              '"local_ip": "127.0.0.9"' in connected
              and '"server_peer_ip": "127.0.0.9"' in connected,
              connected)

        again = bindhook.install(proc.pid, "127.0.0.9", timeout=5)
        check("x64 install is idempotent", again is hook and again.patched >= 2,
              f"same={again is hook} patched={again.patched}")

        block = hook.block
        bindhook.forget(proc.pid)  # emulate an MCP server restart
        time.sleep(0.2)
        adopted = bindhook.install(proc.pid, "127.0.0.9", timeout=20)
        check("x64 hook block is reused after a server restart",
              adopted.reused and adopted.block == block,
              f"reused={adopted.reused} {block}->{adopted.block}")
        adopted.uninstall()
        proc.stdin.write("again\n")
        proc.stdin.flush()
        after = readline(proc)
        check("uninstall restores the original bind", '"bound": "0.0.0.0"' in after, after)
    finally:
        try:
            if proc.stdin:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        bindhook.forget(proc.pid)


def run_x86_probe(exe: Path) -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5)
    port = receiver.getsockname()[1]
    proc = subprocess.Popen(
        [str(exe), str(port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    try:
        ready = readline(proc)
        check("x86 probe started", ready == "ready", ready)
        hook = bindhook.install(proc.pid, "127.0.0.10", timeout=20)
        check("x86 hook installed", hook.arch == "x86" and hook.patched >= 2, str(hook.info()))
        proc.stdin.write("go\n")
        proc.stdin.flush()
        first = readline(proc)
        second = readline(proc)
        third = readline(proc)
        fourth = readline(proc)
        fifth = readline(proc)
        check("x86 bind was forced to 127.0.0.10",
              first.startswith("bind 127.0.0.10 "), first)
        check("x86 unbound sendto was pre-bound",
              second.startswith("send 127.0.0.10 "), second)
        check("x86 unbound WSASendTo was pre-bound",
              third.startswith("wsasend 127.0.0.10 "), third)
        check("x86 unbound connect was pre-bound",
              fourth.startswith("connect 127.0.0.10 ") and fourth.endswith(" 127.0.0.10"),
              fourth)
        check("x86 unbound WSAConnect was pre-bound",
              fifth.startswith("wsaconnect 127.0.0.10 ") and fifth.endswith(" 127.0.0.10"),
              fifth)
        data, source = receiver.recvfrom(64)
        data2, source2 = receiver.recvfrom(64)
        check("x86 peer sees the forced source address",
              data == b"x" and source[0] == "127.0.0.10",
              f"{data!r} from {source}")
        check("x86 peer sees the WSASendTo source address",
              data2 == b"y" and source2[0] == "127.0.0.10",
              f"{data2!r} from {source2}")
    finally:
        try:
            if proc.stdin:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        bindhook.forget(proc.pid)
        receiver.close()


def main() -> int:
    if sys.platform != "win32":
        skip("bind hook integration", "Windows only")
        print("\nALL PASSED (with skips)")
        return 0
    test_pe_parsing()
    folder = Path(tempfile.mkdtemp(prefix="wc3-bindhook-"))
    try:
        run_python_probe(folder)
        exe = build_x86_probe(folder)
        if exe:
            run_x86_probe(exe)
        else:
            skip("x86 (classic WC3) bind hook", "no x86 MSVC toolchain found")
    finally:
        for path in folder.glob("*"):
            try:
                path.unlink()
            except Exception:
                pass
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{'ALL PASSED' if not failed else 'FAILED: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
