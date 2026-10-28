"""Pure-Python checks for the multi-instance registry in ``game.py``.

These run on Linux (no Win32 calls): ``game.status`` is monkeypatched so the
registry logic can be exercised without a game process.

Run:  python tests/test_registry.py
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from wc3_tester import game  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    status = "PASS" if condition else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return bool(condition)


# pid -> state for the monkeypatched status()
_STATES: dict[int, str] = {}
_ORIGINAL_STATUS = game.status


def fake_status(instance: str | None = None) -> dict:
    try:
        session = game.get_session(instance)
    except KeyError:
        raise
    if session is None:
        return {
            "instance": instance,
            "state": "not_started",
            "running": False,
            "pid": None,
        }
    state = _STATES.get(int(session.pid or 0), "running")
    return {
        "instance": session.instance,
        "state": state,
        "running": state in ("running", "hung"),
        "pid": session.pid,
        "hwnd": session.hwnd,
        "exit_code": None,
        "exit_code_hex": None,
        "exit_code_name": None,
    }


def reset() -> None:
    game._instances.clear()
    game._current = None
    _STATES.clear()


def add(name: str, pid: int, state: str = "running") -> game.Session:
    session = game.Session(
        instance=name,
        pid=pid,
        exe="C:/Games/war3.exe",
        start_time=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    _STATES[pid] = state
    game.set_session(session)
    return session


def main() -> int:
    game.status = fake_status
    try:
        # -- empty registry --------------------------------------------------
        reset()
        check("empty next_instance_id is p1", game.next_instance_id() == "p1")
        check("empty get_session returns None", game.get_session() is None)

        # -- set_session / get_session --------------------------------------
        reset()
        p1 = add("p1", 1001)
        check("set_session makes it current", game.get_session() is p1)
        check("explicit id resolves", game.get_session("p1") is p1)
        check("next id after p1 is p2", game.next_instance_id() == "p2", game.next_instance_id())

        p2 = add("p2", 1002)
        check("second instance becomes current", game.get_session() is p2)
        check(
            "list_instances order and current flag",
            [i["instance"] for i in game.list_instances()] == ["p1", "p2"]
            and [i["current"] for i in game.list_instances()] == [False, True],
            str(game.list_instances()),
        )

        # -- KeyError for unknown explicit ids ------------------------------
        raised = None
        try:
            game.get_session("p99")
        except KeyError as exc:
            raised = exc
        message = str(raised) if raised is not None else ""
        check("unknown instance raises KeyError", raised is not None, message)
        check(
            "KeyError lists known ids",
            "'p1'" in message and "'p2'" in message,
            message,
        )
        check(
            "get_session(None) keeps current on unknown lookup",
            game.get_session() is p2,
        )

        # -- select ----------------------------------------------------------
        picked = game.select("p1")
        check("select returns status", picked.get("instance") == "p1", str(picked))
        check("select changes the current instance", game.get_session() is p1)
        raised = None
        try:
            game.select("p99")
        except KeyError:
            raised = "yes"
        check("select unknown raises KeyError", raised == "yes")
        check("failed select keeps current", game.get_session() is p1)

        # -- current unset with several instances ---------------------------
        game._current = None
        check("no current + 2 instances -> None", game.get_session() is None)
        game._current = "gone"
        check("stale current + 2 instances -> None", game.get_session() is None)
        game._current = "p2"
        check("stale/set current resolves", game.get_session() is p2)

        # -- dead entries are dropped only when nothing runs ----------------
        reset()
        add("p1", 2001, state="exited")
        add("p2", 2002, state="crashed")
        new_id = game.next_instance_id()
        check("all dead: ids are reused", new_id == "p1", new_id)
        check("all dead: entries dropped", game._instances == {}, str(game._instances))

        reset()
        add("p1", 3001, state="running")
        add("p2", 3002, state="exited")
        new_id = game.next_instance_id()
        check(
            "one running: dead entry kept and next id is p3",
            new_id == "p3" and set(game._instances) == {"p1", "p2"},
            f"{new_id} {sorted(game._instances)}",
        )
        check(
            "dead entry state still available",
            game.status("p2").get("state") == "exited",
            str(game.status("p2")),
        )

        reset()
        add("p1", 4001, state="hung")
        add("p2", 4002, state="not_started")
        new_id = game.next_instance_id()
        check(
            "hung counts as running (dead entry kept)",
            new_id == "p3" and set(game._instances) == {"p1", "p2"},
            f"{new_id} {sorted(game._instances)}",
        )

        # -- set_session(None) only clears the current pointer --------------
        reset()
        p1 = add("p1", 5001)
        game.set_session(None)
        check("set_session(None) clears current", game._current is None)
        check("lone instance is still returned", game.get_session() is p1)

        # -- out_dir is per instance ----------------------------------------
        reset()
        temp = Path(tempfile.mkdtemp(prefix="wc3-registry-"))
        old_out = os.environ.get("WC3_TESTER_OUT")
        os.environ["WC3_TESTER_OUT"] = str(temp)
        try:
            session = add("p3", 6001)
            session.out_dir = None
            path = game.out_dir("p3")
            check(
                "out_dir uses <timestamp>-<instance>",
                path.parent == temp and path.name.endswith("-p3"),
                str(path),
            )
            session.out_dir = temp / "attached-p3"
            check(
                "out_dir honours session.out_dir",
                game.out_dir("p3") == temp / "attached-p3",
                str(game.out_dir("p3")),
            )
        finally:
            if old_out is None:
                os.environ.pop("WC3_TESTER_OUT", None)
            else:
                os.environ["WC3_TESTER_OUT"] = old_out
    finally:
        game.status = _ORIGINAL_STATUS
        reset()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
