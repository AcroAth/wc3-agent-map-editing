"""End-to-end smoke test that does not need Warcraft III.

Starts ``fake_game.py`` (a tkinter window titled "Warcraft III"), attaches to
it, exercises screenshots, background input, chat, scrolling, then triggers a
synthetic access violation and verifies the crash report contains the exit
code and the parsed WC3-style error log.

Run on Windows:  python tests\\smoke_test.py
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from wc3_tester import capture, crash, game, inputs, winapi, window  # noqa: E402

winapi.enable_dpi_awareness()

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    status = "PASS" if condition else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return bool(condition)


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


def wait_for(predicate, timeout: float, interval: float = 0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="wc3-tester-smoke-"))
    log_path = temp / "events.jsonl"
    errors_dir = temp / "errors"
    out_dir = temp / "out"
    errors_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ["WC3_ERRORS_DIRS"] = str(errors_dir)
    os.environ["WC3_TESTER_OUT"] = str(out_dir)
    os.environ["WC3_INPUT_MODE"] = "background"

    print(f"temp dir : {temp}")
    print(f"fake game: {ROOT / 'tests' / 'fake_game.py'}")

    stderr_path = temp / "fake_stderr.txt"
    proc = subprocess.Popen(
        # A venv's python.exe is a redirector that spawns the real interpreter
        # as a child, so its pid would not own the window. Use the base one.
        [getattr(sys, "_base_executable", sys.executable),
         str(ROOT / "tests" / "fake_game.py"), str(log_path), str(errors_dir)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=open(stderr_path, "w", encoding="utf-8", errors="ignore"),
    )

    hwnd = None
    try:
        hwnd = wait_for(
            lambda: window.find_window(pid=proc.pid, title="Warcraft III"), 20
        )
        if not check("fake window appears", bool(hwnd), str(hwnd)):
            return 1

        attach_info = game.attach(pid=proc.pid, title="Warcraft III", instance="p1")
        session = game.get_session("p1")
        if session is not None:
            session.popen = proc  # ensure the exit code is available
        check(
            "attach returns pid/hwnd",
            attach_info.get("pid") == proc.pid and attach_info.get("hwnd") == hwnd,
            str(attach_info.get("pid")),
        )
        check(
            "attach registers the instance",
            attach_info.get("instance") == "p1"
            and game.get_session("p1") is session
            and game.get_session() is session,
            str(attach_info.get("instance")),
        )
        time.sleep(0.4)  # let tkinter paint the initial canvas

        # -- screenshot -----------------------------------------------------
        image, info = capture.capture(hwnd, max_width=800, instance="p1")
        saved = Path(info.get("path", ""))
        check("screenshot has pixels", image.width > 0 and image.height > 0)
        check("screenshot is not blank", not capture.is_blank(image))
        check("screenshot saved as PNG", saved.is_file() and saved.suffix == ".png", str(saved))
        check(
            "screenshot reports client/scale",
            (info.get("client_width") or 0) > 0
            and (info.get("client_height") or 0) > 0
            and info.get("scale") is not None,
            f"{info.get('client_width')}x{info.get('client_height')} scale={info.get('scale')}",
        )

        # -- background clicks ---------------------------------------------
        inputs.click(hwnd, 100, 100, mode="background")
        time.sleep(0.2)
        inputs.click(hwnd, 400, 300, mode="background")
        time.sleep(0.4)

        events = read_events(log_path)
        mouse_downs = [
            e for e in events if e.get("type") == "mouse" and e.get("kind") == "down"
        ]
        near_100 = [
            e for e in mouse_downs if abs(e.get("x", -999) - 100) <= 2 and abs(e.get("y", -999) - 100) <= 2
        ]
        near_400 = [
            e for e in mouse_downs if abs(e.get("x", -999) - 400) <= 2 and abs(e.get("y", -999) - 300) <= 2
        ]
        check("background click at (100,100)", bool(near_100), str(mouse_downs[:4]))
        check("background click at (400,300)", bool(near_400), str(mouse_downs[:4]))

        # -- keys / text / chat / wheel ------------------------------------
        inputs.key(hwnd, "a", mode="background")
        time.sleep(0.15)
        inputs.type_text(hwnd, "hi", mode="background")
        time.sleep(0.2)
        inputs.send_chat(hwnd, "iseedeadpeople", mode="background")
        time.sleep(0.4)
        inputs.scroll(hwnd, 400, 300, 1, mode="background")
        time.sleep(0.4)

        events = read_events(log_path)
        key_events = [e for e in events if e.get("type") == "key"]
        chars = "".join(e.get("char") or "" for e in key_events)
        keysyms = [(e.get("keysym") or "").lower() for e in key_events]
        check("key 'a' logged", "a" in chars or "a" in keysyms, chars[-40:])
        check("key 'a' not doubled", not chars.startswith("aa"), repr(chars[:6]))
        check("typed text 'hi' logged", "hi" in chars, chars[-40:])
        check(
            "chat text logged",
            "iseedeadpeople" in chars,
            chars[-60:],
        )
        wheels = [e for e in events if e.get("type") == "wheel"]
        check("wheel event logged", bool(wheels), str(wheels[:2]))

        # -- synthetic crash -------------------------------------------------
        inputs.key(hwnd, "f12", mode="background")

        def _exited_with_code():
            state = game.status("p1")
            if state.get("state") in ("exited", "crashed") and state.get("exit_code") is not None:
                return state
            return None

        final = wait_for(_exited_with_code, 25)
        check(
            "game process exits after F12",
            bool(final) and final.get("state") in ("exited", "crashed"),
            str(final.get("state") if final else None),
        )
        if not final:
            return 1
        check(
            "exit code is ACCESS_VIOLATION",
            final.get("exit_code_name") == "ACCESS_VIOLATION",
            f"{final.get('exit_code_hex')} {final.get('exit_code_name')}",
        )

        report = crash.crash_report(instance="p1")
        check(
            "crash report names the instance",
            report.get("instance") == "p1",
            str(report.get("instance")),
        )
        check(
            "crash report sees crash state",
            report.get("state") in ("crashed", "exited"),
            str(report.get("state")),
        )
        check(
            "crash report has ACCESS_VIOLATION",
            report.get("exit_code_name") == "ACCESS_VIOLATION",
            str(report.get("exit_code_name")),
        )
        error_files = [f for f in report.get("new_files", []) if f.get("kind") == "error_txt"]
        check("error txt found", bool(error_files), str([f.get("path") for f in error_files]))
        parsed = report.get("parsed") or []
        check("error txt parsed", bool(parsed), str(parsed[:1]))
        if parsed:
            first = parsed[0]
            check(
                "parsed exception code 0xC0000005",
                first.get("exception_code") == "0xC0000005",
                str(first.get("exception_code")),
            )
            check(
                "parsed exception name ACCESS_VIOLATION",
                first.get("exception_name") == "ACCESS_VIOLATION",
                str(first.get("exception_name")),
            )
            check(
                "parsed script reference",
                any("war3map" in ref.lower() for ref in first.get("script_refs") or []),
                str(first.get("script_refs", [])[:2]),
            )
        check(
            "verdict mentions crash",
            "crash" in (report.get("verdict") or "").lower(),
            report.get("verdict", "")[:160],
        )

        try:
            if proc.poll() is None:
                game.kill("p1")
        except Exception:
            pass
    except Exception as exc:
        import traceback

        traceback.print_exc()
        check("smoke test completed without exception", False, str(exc))
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        if stderr_path.is_file():
            text = stderr_path.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                print("\n--- fake_game stderr ---")
                print(text[-4000:])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
