"""Multi-instance E2E test over MCP stdio (Windows only).

Starts TWO ``tests/fake_game.py`` processes with separate event logs, attaches
them over the MCP server as instances "p1" and "p2" (exactly like
``mcp_e2e_test.py``), then proves that input is routed per instance and that
one instance can crash/die while the other keeps running.

Note: the fake games are 64-bit Python processes, so the virtual cursor /
LAN wrapper does not apply to them.

Run on Windows with the plugin venv:  python tests\\multi_instance_test.py
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import asyncio
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
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
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


def read_events(path: Path) -> list[dict]:
    events: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return events


def wait_events(path: Path, predicate, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = predicate(read_events(path))
        if found:
            return found
        time.sleep(0.1)
    return predicate(read_events(path))


def mouse_downs(events: list[dict], x: int, y: int) -> list[dict]:
    return [
        e
        for e in events
        if e.get("type") == "mouse"
        and e.get("kind") == "down"
        and abs(int(e.get("x", -999)) - x) <= 2
        and abs(int(e.get("y", -999)) - y) <= 2
    ]


def typed_text(events: list[dict]) -> str:
    return "".join(
        e.get("char") or "" for e in events if e.get("type") == "key"
    )


async def attach_with_retry(session, pid: int, instance: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = payload(
            await session.call_tool(
                "wc3_attach", {"pid": pid, "instance": instance}
            )
        )
        if last.get("ok") and last.get("pid") == pid:
            return last
        await asyncio.sleep(0.5)
    return last


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wc3-multi-"))
    log1, log2 = tmp / "p1.jsonl", tmp / "p2.jsonl"
    err1, err2, shared_errors = tmp / "errors1", tmp / "errors2", tmp / "errors"
    for directory in (err1, err2, shared_errors, tmp / "out"):
        directory.mkdir(parents=True, exist_ok=True)

    env = dict(
        os.environ,
        WC3_ERRORS_DIRS=str(shared_errors),
        WC3_TESTER_OUT=str(tmp / "out"),
        WC3_INPUT_MODE="background",
    )
    python = getattr(sys, "_base_executable", sys.executable)
    games = [
        subprocess.Popen(
            [python, str(ROOT / "tests" / "fake_game.py"), str(log1), str(err1)],
            cwd=str(ROOT),
        ),
        subprocess.Popen(
            [python, str(ROOT / "tests" / "fake_game.py"), str(log2), str(err2)],
            cwd=str(ROOT),
        ),
    ]
    params = StdioServerParameters(
        command="cmd.exe",
        args=["/d", "/s", "/c", "scripts\\run_server.cmd"],
        cwd=str(ROOT),
        env=env,
    )

    try:
        time.sleep(2.0)  # let both fake windows appear
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()

                att1 = await attach_with_retry(s, games[0].pid, "p1")
                check(
                    "attach p1",
                    att1.get("instance") == "p1" and att1.get("pid") == games[0].pid,
                    str(att1)[:200],
                )
                att2 = await attach_with_retry(s, games[1].pid, "p2")
                check(
                    "attach p2",
                    att2.get("instance") == "p2" and att2.get("pid") == games[1].pid,
                    str(att2)[:200],
                )

                inst = payload(await s.call_tool("wc3_instances", {}))
                ids = {i.get("instance") for i in inst.get("instances", [])}
                check(
                    "wc3_instances lists both instances",
                    {"p1", "p2"} <= ids,
                    json.dumps(inst)[:400],
                )
                check(
                    "wc3_instances marks the current one",
                    any(
                        i.get("instance") == "p2" and i.get("current")
                        for i in inst.get("instances", [])
                    ),
                    json.dumps(inst)[:400],
                )

                # -- input is routed per instance --------------------------
                clk = payload(
                    await s.call_tool(
                        "wc3_click", {"instance": "p2", "x": 50, "y": 60}
                    )
                )
                check("click result names p2", clk.get("instance") == "p2", str(clk)[:200])
                got2 = wait_events(log2, lambda ev: mouse_downs(ev, 50, 60))
                check("p2 received the click", bool(got2), str(got2[:2]))
                check(
                    "p1 did not receive the click",
                    not mouse_downs(read_events(log1), 50, 60),
                    str(mouse_downs(read_events(log1), 50, 60)[:2]),
                )

                chat = payload(
                    await s.call_tool(
                        "wc3_chat", {"instance": "p1", "text": "hello"}
                    )
                )
                check("chat result names p1", chat.get("instance") == "p1", str(chat)[:200])
                wait_events(log1, lambda ev: "hello" in typed_text(ev))
                check(
                    "p1 received the chat text",
                    "hello" in typed_text(read_events(log1)),
                    repr(typed_text(read_events(log1))[-40:]),
                )
                check(
                    "p2 did not receive the chat text",
                    "hello" not in typed_text(read_events(log2)),
                    repr(typed_text(read_events(log2))[-40:]),
                )

                # -- crash one instance, the other keeps running -----------
                await s.call_tool("wc3_key", {"instance": "p1", "keys": "f12"})
                wt = payload(
                    await s.call_tool(
                        "wc3_wait",
                        {"instance": "p1", "seconds": 25, "until": "exit"},
                    )
                )
                check(
                    "wc3_wait saw p1 exit",
                    bool(wt.get("until_met")),
                    str(wt)[:300],
                )
                st1 = payload(
                    await s.call_tool("wc3_status", {"instance": "p1"})
                )
                check(
                    "p1 crashed after F12",
                    st1.get("state") == "crashed"
                    and st1.get("exit_code_name") == "ACCESS_VIOLATION",
                    str(st1)[:300],
                )
                st2 = payload(
                    await s.call_tool("wc3_status", {"instance": "p2"})
                )
                check(
                    "p2 is still running",
                    st2.get("state") == "running" and bool(st2.get("running")),
                    str(st2)[:300],
                )
                check(
                    "status is per instance",
                    st1.get("instance") == "p1" and st2.get("instance") == "p2",
                    f"p1={st1.get('instance')} p2={st2.get('instance')}",
                )

                # -- kill the survivor -------------------------------------
                killed = payload(
                    await s.call_tool("wc3_kill", {"instance": "p2"})
                )
                check(
                    "wc3_kill targets p2",
                    killed.get("instance") == "p2",
                    str(killed)[:200],
                )
                check(
                    "p2 is no longer running",
                    not killed.get("running"),
                    f"state={killed.get('state')}",
                )
    finally:
        for proc in games:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    print(
        f"\n{'ALL PASSED' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}"
    )
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
