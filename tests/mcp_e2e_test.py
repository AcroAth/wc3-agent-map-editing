"""End-to-end test over MCP stdio, launching the server the way Codex does
(cmd.exe /c scripts\\run_server.cmd) and driving fake_game.py through tools.

Run on Windows with the plugin venv:  python tests\\mcp_e2e_test.py
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def payload(result) -> dict:
    for item in result.content:
        if getattr(item, "type", "") == "text":
            try:
                return json.loads(item.text)
            except Exception:
                continue
    return {}


def write_minidump(pid: int, path: Path) -> bool:
    """Write a small minidump of a live process with dbghelp (no exception
    stream, but exercises module-list parsing)."""
    k32, dbg = ctypes.windll.kernel32, ctypes.windll.dbghelp
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x0410 | 0x0040, False, pid)  # QUERY_INFO|VM_READ|DUP_HANDLE
    if not h:
        return False
    f = open(path, "wb")
    try:
        import msvcrt
        fh = msvcrt.get_osfhandle(f.fileno())
        dbg.MiniDumpWriteDump.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                          ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        return bool(dbg.MiniDumpWriteDump(h, pid, fh, 0, None, None, None))
    finally:
        f.close()
        k32.CloseHandle(ctypes.c_void_p(h))


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wc3-e2e-"))
    log, errs = tmp / "events.jsonl", tmp / "errors"
    env = dict(os.environ, WC3_ERRORS_DIRS=str(errs), WC3_TESTER_OUT=str(tmp / "out"))
    game = subprocess.Popen([getattr(sys, "_base_executable", sys.executable),
                             str(ROOT / "tests" / "fake_game.py"), str(log), str(errs)])
    # A window on top of the game, to prove capture/input work while covered.
    # (Not notepad.exe: on Windows 11 it hands off to the Store app and the
    # window would outlive cover.kill().)
    cover = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c",
                              "import tkinter as t; r=t.Tk(); r.title('cover');"
                              "r.geometry('900x700+40+40'); r.attributes('-topmost', 1); r.mainloop()"])
    time.sleep(2.0)
    params = StdioServerParameters(command="cmd.exe",
                                   args=["/d", "/s", "/c", "scripts\\run_server.cmd"],
                                   cwd=str(ROOT), env=env)
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = {t.name for t in (await s.list_tools()).tools}
                check("16 tools listed", len(tools) == 16, ", ".join(sorted(tools)))

                att = payload(await s.call_tool("wc3_attach", {"pid": game.pid}))
                check("attach by pid", att.get("pid") == game.pid, str(att)[:200])

                shot = await s.call_tool("wc3_screenshot", {"max_width": 640})
                kinds = [c.type for c in shot.content]
                info = payload(shot)
                check("screenshot returns image + info", "image" in kinds and bool(info), str(kinds))
                check("screenshot downscaled", info.get("scale") == 0.8, str(info.get("scale")))
                check("captured while covered", not info.get("blank", False) and info.get("method") == "printwindow",
                      f"method={info.get('method')} attempts={info.get('attempts')}")

                clk = payload(await s.call_tool("wc3_click", {"x": 0.25, "y": 0.5, "normalized": True}))
                check("click reports game_state", (clk.get("game_state") or {}).get("state") == "running", str(clk)[:200])
                await s.call_tool("wc3_chat", {"text": "greedisgood 500"})
                await asyncio.sleep(0.5)
                ev = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
                downs = [e for e in ev if e.get("type") == "mouse" and e.get("kind") == "down"]
                check("normalized click lands at (200,300)", any(e["x"] == 200 and e["y"] == 300 for e in downs), str(downs))
                text = "".join(e.get("char", "") for e in ev if e.get("type") == "key")
                check("chat typed once", text.count("greedisgood 500") == 1, repr(text))

                st = payload(await s.call_tool("wc3_status", {}))
                check("status running", st.get("state") == "running", str(st)[:300])

                dmp = tmp / "live.dmp"
                if write_minidump(game.pid, dmp):
                    an = payload(await s.call_tool("wc3_analyze_dump", {"path": str(dmp)}))
                    check("analyze_dump parses modules", (an.get("module_count") or 0) > 0 or bool(an.get("cdb")),
                          json.dumps(an)[:300])
                else:
                    check("write test minidump", False)

                await s.call_tool("wc3_key", {"keys": "f12"})
                wt = payload(await s.call_tool("wc3_wait", {"seconds": 15, "until": "exit"}))
                st = wt.get("status") or wt
                check("attached game: crash detected with code", st.get("state") == "crashed"
                      and st.get("exit_code_name") == "ACCESS_VIOLATION", str(st)[:200])
                rep = payload(await s.call_tool("wc3_crash_report", {}))
                check("crash report verdict", "ACCESS_VIOLATION" in rep.get("verdict", ""), rep.get("verdict", "")[:200])
                logs = payload(await s.call_tool("wc3_read_logs", {}))
                check("read_logs finds error txt", "war3map.j" in json.dumps(logs), json.dumps(logs)[:200])
    finally:
        for p in (game, cover):
            if p.poll() is None:
                p.kill()
    print(f"\n{'ALL PASSED' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
