"""Two chats, one server: two stdio proxies must share a single daemon (Windows).

Uses its own LOCALAPPDATA so it never touches a real shared server.  Checks:
both proxies reach the same daemon; an instance attached in chat A is visible
in chat B; a long wc3_wait in A does not block B; a new chat still sees the
instance; requests without the token are refused; a killed daemon is
restarted by the next call.

Run:  %LOCALAPPDATA%\\wc3-tester\\venv\\Scripts\\python tests\\shared_server_test.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
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
            with contextlib.suppress(Exception):
                return json.loads(item.text)
    return {}


@contextlib.asynccontextmanager
async def chat(env: dict):
    params = StdioServerParameters(command="cmd.exe", args=["/d", "/s", "/c", "scripts\\run_server.cmd"],
                                   cwd=str(ROOT), env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wc3-shared-"))
    appdata = tmp / "appdata"
    appdata.mkdir()
    venv = Path(os.environ["LOCALAPPDATA"]) / "wc3-tester" / "venv"
    env = dict(os.environ, LOCALAPPDATA=str(appdata), WC3_TESTER_VENV=str(venv),
               WC3_TESTER_OUT=str(tmp / "out"), WC3_SHARED_SERVER="1")
    state_path = appdata / "wc3-tester" / "daemon.json"
    python = getattr(sys, "_base_executable", sys.executable)
    game = subprocess.Popen([python, str(ROOT / "tests" / "fake_game.py"), str(tmp / "g.jsonl"), str(tmp / "err")],
                            cwd=str(ROOT))
    try:
        time.sleep(2.0)
        async with chat(env) as a, chat(env) as b:
            tools = (await b.list_tools()).tools
            check("proxy lists 20 tools", len(tools) == 20, str(len(tools)))
            att = payload(await a.call_tool("wc3_attach", {"pid": game.pid, "instance": "p1"}))
            check("chat A attaches p1", att.get("pid") == game.pid, str(att)[:150])
            inst_a = payload(await a.call_tool("wc3_instances", {}))
            inst_b = payload(await b.call_tool("wc3_instances", {}))
            state = json.loads(state_path.read_text())
            check("both chats reach the same daemon",
                  inst_a["server"]["pid"] == inst_b["server"]["pid"] == state["pid"]
                  and inst_b["server"]["shared"], f"{inst_a['server']} {inst_b['server']} state={state['pid']}")
            check("chat B sees chat A's instance",
                  [i["instance"] for i in inst_b["instances"]] == ["p1"], json.dumps(inst_b["instances"]))

            async def timed(coro):
                t0 = time.time()
                await coro
                return time.time() - t0

            wait_t, status_t = await asyncio.gather(
                timed(a.call_tool("wc3_wait", {"seconds": 4})),
                timed(b.call_tool("wc3_status", {"instance": "p1"})),
            )
            check("B is not blocked by A's 4 s wait", status_t < 2.0 and wait_t >= 3.5,
                  f"wait={wait_t:.1f}s status={status_t:.1f}s")

        async with chat(env) as c:
            inst_c = payload(await c.call_tool("wc3_instances", {}))
            check("a new chat still sees p1", [i["instance"] for i in inst_c["instances"]] == ["p1"]
                  and inst_c["server"]["pid"] == state["pid"], json.dumps(inst_c)[:200])

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{state['port']}/health", timeout=3)
            refused = False
        except urllib.error.HTTPError as exc:
            refused = exc.code == 401
        check("request without token is refused", refused)

        psutil.Process(state["pid"]).kill()
        time.sleep(0.5)
        async with chat(env) as d:
            inst_d = payload(await d.call_tool("wc3_instances", {}))
            new_state = json.loads(state_path.read_text())
            check("a killed daemon is restarted on the next call",
                  inst_d.get("ok") and new_state["pid"] != state["pid"] and inst_d["server"]["pid"] == new_state["pid"],
                  f"old={state['pid']} new={new_state['pid']}")
        # A daemon running other code is replaced when no game is running.
        game.kill()
        time.sleep(1.0)
        os.environ["LOCALAPPDATA"] = str(appdata)
        sys.path.insert(0, str(ROOT / "src"))
        from wc3_tester import shared

        before = json.loads(state_path.read_text())["pid"]
        real_hash = shared.code_hash
        shared.code_hash = lambda: "outdated-proxy"
        try:
            await asyncio.to_thread(shared.ensure_daemon)
        finally:
            shared.code_hash = real_hash
        after = json.loads(state_path.read_text())["pid"]
        check("an outdated idle daemon is replaced", after != before and not psutil.pid_exists(before),
              f"before={before} after={after}")
    finally:
        game.kill()
        with contextlib.suppress(Exception):
            psutil.Process(json.loads(state_path.read_text())["pid"]).kill()
    print("ALL PASSED" if not FAILS else "FAILED: " + ", ".join(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
