"""Crash detection and analysis for Warcraft III.

Sources of truth, from fastest to richest:

* the process exit code (NTSTATUS, e.g. ``0xC0000005`` = ACCESS_VIOLATION),
* a visible ``FATAL ERROR!`` dialog (class ``#32770``),
* WC3's own ``Errors\\*.txt`` / ``*.dmp`` files,
* WER reports (``Report.wer``),
* the Windows Application event log (Event ID 1000/1002),
* optionally ``cdb.exe`` or the pure-Python ``minidump`` package for a dump.

Every public function here is defensive: analysing a crash must never raise a
second exception.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import winapi as w
from . import window

log = logging.getLogger(__name__)

EXCEPTION_NAMES: dict[int, str] = {
    0xC0000005: "ACCESS_VIOLATION",
    0xC00000FD: "STACK_OVERFLOW",
    0xC0000409: "STACK_BUFFER_OVERRUN",
    0xC0000374: "HEAP_CORRUPTION",
    0x80000003: "BREAKPOINT",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC0000094: "INT_DIVIDE_BY_ZERO",
    0xC0000096: "PRIVILEGED_INSTRUCTION",
    0xC0000017: "NO_MEMORY",
    0xC0000135: "DLL_NOT_FOUND",
    0xC0000142: "DLL_INIT_FAILED",
    0xE06D7363: "C++ EXCEPTION",
}

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None, default: datetime | None = None) -> datetime:
    if value is None:
        return default or (_now_utc() - timedelta(hours=24))
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(text)
        except Exception:
            return default or (_now_utc() - timedelta(hours=24))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Fatal dialog
# ---------------------------------------------------------------------------


def _child_texts(hwnd: int) -> list[str]:
    texts: list[str] = []
    for child in w.enum_child_windows(hwnd):
        try:
            text = w.get_window_text_timeout(child, timeout_ms=800).strip()
            if text:
                texts.append(text)
        except Exception:
            continue
    return texts


def find_fatal_dialog(pid: int | None = None) -> dict | None:
    """Find a WC3 fatal-error dialog and return its text, or ``None``."""
    try:
        candidates = w.enum_windows()
    except Exception:
        return None

    best: dict | None = None
    best_score = -1
    for hwnd in candidates:
        try:
            wpid = w.window_pid(hwnd)
            cls = w.get_class_name(hwnd)
            title = w.get_window_text_timeout(hwnd, timeout_ms=750)
            title_l = title.lower()
            cls_l = cls.lower()
            is_dialog = cls_l == "#32770"
            same_pid = pid is not None and wpid == int(pid)
            # A #32770 dialog belonging to the game or explicitly naming
            # Warcraft/Fatal, or a window of the game whose own title says
            # Fatal/Error/Critical.  The ordinary "Warcraft III" main window
            # must never qualify.
            dialog_match = is_dialog and (
                same_pid
                or "warcraft" in title_l
                or "fatal" in title_l
                or "error" in title_l
            )
            fatal_title = any(k in title_l for k in ("fatal", "error", "critical"))
            if not (dialog_match or (same_pid and fatal_title)):
                continue
            parts = [title] if title else []
            parts.extend(_child_texts(hwnd))
            # Preserve order, drop duplicates / empties.
            seen: set[str] = set()
            merged: list[str] = []
            for part in parts:
                clean = " ".join(part.split())
                if clean and clean not in seen:
                    seen.add(clean)
                    merged.append(clean)
            combined = "\n".join(merged)
            contains_fatal = any(
                k in combined.lower() for k in ("fatal", "exception", "critical")
            )
            dialog = {
                "hwnd": hwnd,
                "pid": wpid,
                "class_name": cls,
                "title": title,
                "text": combined,
                "contains_fatal": contains_fatal,
            }
            score = 0
            if same_pid:
                score += 10
            if is_dialog:
                score += 2
            if contains_fatal:
                score += 5
            if "warcraft" in title_l:
                score += 3
            if "fatal" in title_l:
                score += 2
            if score > best_score:
                best_score = score
                best = dialog
        except Exception:
            continue
    return best


# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------


def _documents_dir() -> Path | None:
    docs = None
    try:
        docs = w.known_folder_documents()
    except Exception:
        docs = None
    if not docs:
        profile = os.environ.get("USERPROFILE") or str(Path.home())
        candidate = Path(profile) / "Documents"
        if candidate.is_dir():
            docs = str(candidate)
        else:
            onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
            if onedrive:
                candidate = Path(onedrive) / "Documents"
                if candidate.is_dir():
                    docs = str(candidate)
    return Path(docs) if docs else None


def error_dirs(exe: str | None = None) -> list[str]:
    """All directories that may contain WC3 error logs / dumps."""
    dirs: list[Path] = []

    def add(path: Path | str | None) -> None:
        if not path:
            return
        try:
            p = Path(path)
            if p not in dirs:
                dirs.append(p)
        except Exception:
            pass

    if exe:
        add(Path(exe).parent / "Errors")
        if (Path(exe).parent.name.lower()) == "x86_64":
            add(Path(exe).parent.parent / "Errors")

    docs = _documents_dir()
    if docs:
        add(docs / "Warcraft III" / "Errors")
        add(docs / "Warcraft III" / "Logs")
        add(docs / "Warcraft III")

    env_dirs = os.environ.get("WC3_ERRORS_DIRS")
    if env_dirs:
        for entry in env_dirs.split(";"):
            entry = entry.strip().strip('"')
            if entry:
                add(Path(os.path.expandvars(os.path.expanduser(entry))))

    result: list[str] = []
    for path in dirs:
        try:
            if path.is_dir():
                result.append(str(path))
        except Exception:
            continue
    return result


def _wer_roots() -> list[Path]:
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Microsoft" / "Windows" / "WER")
    program_data = os.environ.get("ProgramData") or r"C:\ProgramData"
    roots.append(Path(program_data) / "Microsoft" / "Windows" / "WER")
    return roots


def _read_text_file(path: Path, max_bytes: int = 2_000_000) -> str:
    try:
        raw = path.read_bytes()[:max_bytes]
    except Exception:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16", "utf-8-sig", "cp1252", "latin-1")
    elif raw.count(b"\x00") > max(8, len(raw) // 4):
        # UTF-16 without a BOM (some WC3 / WER writers do this).
        encodings = ("utf-16-le", "utf-16", "utf-8-sig", "cp1252", "latin-1")
    else:
        encodings = ("utf-8-sig", "cp1252", "utf-16", "latin-1")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def wer_reports(since: datetime | None = None) -> list[dict]:
    """WER reports whose folder name mentions WAR3 / Warcraft."""
    since_ts = _as_utc(since).timestamp() if since else 0
    reports: list[dict] = []
    for root in _wer_roots():
        for sub in ("ReportArchive", "ReportQueue"):
            base = root / sub
            if not base.is_dir():
                continue
            try:
                entries = list(base.iterdir())
            except Exception:
                continue
            for folder in entries:
                try:
                    name_l = folder.name.lower()
                    if "war3" not in name_l and "warcraft" not in name_l:
                        continue
                    report = folder / "Report.wer"
                    if not report.is_file():
                        continue
                    mtime = report.stat().st_mtime
                    if mtime < since_ts - 2:
                        continue
                    text = _read_text_file(report)
                    fields: dict[str, str] = {}
                    for line in text.splitlines():
                        if "=" in line:
                            key, _, value = line.partition("=")
                            key = key.strip()
                            if key and (
                                key.startswith("Sig[")
                                or key
                                in (
                                    "EventType",
                                    "AppName",
                                    "AppPath",
                                    "ReportIdentifier",
                                    "Response.type",
                                    "OsInfo[0].Key",
                                    "OsInfo[1].Key",
                                )
                            ):
                                fields[key] = value.strip()
                    reports.append(
                        {
                            "path": str(report),
                            "folder": str(folder),
                            "mtime": mtime,
                            "fields": fields,
                        }
                    )
                except Exception:
                    continue
    reports.sort(key=lambda r: r.get("mtime", 0), reverse=True)
    return reports[:20]


def crash_dumps(exe: str | None = None, since: datetime | None = None) -> list[str]:
    """Dumps in %LOCALAPPDATA%\\CrashDumps that belong to the game."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    since_ts = _as_utc(since).timestamp() if since else 0
    stem = ""
    if exe:
        stem = Path(exe).stem.lower()
    result: list[str] = []
    try:
        for path in Path(local, "CrashDumps").glob("*.dmp"):
            name = path.name.lower()
            if stem and stem not in name and "war3" not in name and "warcraft" not in name:
                continue
            try:
                if path.stat().st_mtime < since_ts - 2:
                    continue
            except Exception:
                pass
            result.append(str(path))
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def event_log(since: datetime | str | None) -> list[dict]:
    """Application-log crash events (1000/1002) mentioning the game."""
    since_dt = _as_utc(since)
    iso = since_dt.strftime("%Y-%m-%dT%H:%M:%S")
    query = (
        "*[System[(EventID=1000 or EventID=1002) and "
        f"TimeCreated[@SystemTime>='{iso}']]]"
    )
    cmd = [
        "wevtutil",
        "qe",
        "Application",
        f"/q:{query}",
        "/f:text",
        "/c:20",
        "/rd:true",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception as exc:
        return [{"error": f"wevtutil failed: {exc}"}]

    entries: list[dict] = []
    for block in re.split(r"\r?\n\s*\r?\n", output):
        block = block.strip()
        if not block:
            continue
        low = block.lower()
        if not any(k in low for k in ("war3", "warcraft", "frozen throne")):
            continue
        parsed: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                if key and len(key) < 60:
                    parsed[key] = value.strip()
        entries.append({"raw": block[:4000], "fields": parsed})
        if len(entries) >= 20:
            break
    return entries


# ---------------------------------------------------------------------------
# Error text parsing
# ---------------------------------------------------------------------------


def _decode_file(path: str | Path) -> str:
    p = Path(path)
    return _read_text_file(p, max_bytes=4_000_000)


def parse_error_txt(path: str) -> dict:
    """Parse a classic WC3 ``Errors\\*.txt`` crash log; never raises."""
    result: dict = {
        "path": str(path),
        "program": None,
        "exception_code": None,
        "exception_name": None,
        "address": None,
        "fault_address": None,
        "stack": [],
        "script_refs": [],
        "head": [],
        "line_count": 0,
    }
    try:
        text = _decode_file(path)
        lines = text.splitlines()
        result["line_count"] = len(lines)
        result["head"] = lines[:60]
        full = "\n".join(lines)

        m = re.search(
            r"Exception\s*[:=]\s*(0x[0-9A-Fa-f]+|\d+)"
            r"\s*(?:\(([^)]*)\))?"
            r"(?:\s*at\s*([0-9A-Fa-f]{1,4}:[0-9A-Fa-f]{1,16}))?",
            full,
            re.IGNORECASE,
        )
        if m:
            code_text = m.group(1)
            try:
                code = int(code_text, 0)
            except Exception:
                code = None
            if code is not None:
                result["exception_code"] = f"0x{code & 0xFFFFFFFF:08X}"
                result["exception_name"] = EXCEPTION_NAMES.get(
                    code & 0xFFFFFFFF, m.group(2) or None
                )
            elif m.group(2):
                result["exception_name"] = m.group(2).strip()
            result["address"] = m.group(3)
        if not result["exception_name"]:
            m2 = re.search(r"Exception\s*\(([^)]*)\)", full, re.IGNORECASE)
            if m2:
                result["exception_name"] = m2.group(1).strip()

        m = re.search(r"^\s*Program\s*:\s*(.+)$", full, re.IGNORECASE | re.MULTILINE)
        if m:
            result["program"] = m.group(1).strip()
        m = re.search(
            r"^\s*Fault address\s*:\s*(.+)$", full, re.IGNORECASE | re.MULTILINE
        )
        if m:
            result["fault_address"] = m.group(1).strip()

        # Call stack: prefer an explicit "Call stack"/"Stack trace" section.
        stack: list[str] = []
        start = None
        for i, line in enumerate(lines):
            if re.search(r"(call stack|stack trace|stack dump|backtrace)", line, re.I):
                start = i + 1
                break
        if start is not None:
            for line in lines[start : start + 30]:
                if line.strip():
                    stack.append(line.rstrip())
        if not stack:
            for line in lines:
                if re.match(r"^\s*(0x)?[0-9A-Fa-f]{6,16}\b", line) or "->" in line:
                    stack.append(line.rstrip())
                if len(stack) >= 30:
                    break
        result["stack"] = stack

        refs: list[str] = []
        for line in lines:
            if re.search(r"\.j\b|war3map|jass|lua|trigger", line, re.IGNORECASE):
                refs.append(line.strip())
            if len(refs) >= 30:
                break
        result["script_refs"] = refs
    except Exception as exc:
        result["error"] = f"parse failed: {exc}"
    return result


# ---------------------------------------------------------------------------
# Dump analysis
# ---------------------------------------------------------------------------


def _find_cdb() -> str | None:
    found = shutil.which("cdb.exe") or shutil.which("cdb")
    if found:
        return found
    patterns = [
        r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe",
        r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x86\cdb.exe",
        r"C:\Program Files\Windows Kits\10\Debuggers\x64\cdb.exe",
        r"C:\Program Files\Windows Kits\10\Debuggers\x86\cdb.exe",
    ]
    for pattern in patterns:
        if os.path.isfile(pattern):
            return pattern
    return None


def _analyze_with_cdb(path: str, cdb: str) -> dict:
    cmd = [cdb, "-z", path, "-c", ".ecxr; kb 30; lm; !analyze -v; q"]
    sympath = os.environ.get("WC3_SYMPATH")
    if sympath:
        cmd.insert(1, "-y")
        cmd.insert(2, f"srv*{sympath}*")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=180,
            creationflags=CREATE_NO_WINDOW,
        )
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception as exc:
        return {"tool": "cdb", "path": path, "error": f"cdb failed: {exc}"}

    output = output.strip()
    sections: dict[str, str] = {}
    for key, pattern in (
        ("exception", r"(?:ExceptionAddress|EXCEPTION_RECORD|exception code)[^\n]*"),
        ("faulting_module", r"(?:FAULTING_MODULE|MODULE_NAME|IMAGE_NAME)[^\n]*"),
        ("stack", r"(?:ChildEBP|STACK_TEXT|Call Site)[^\n]*"),
    ):
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            sections[key] = match.group(0).strip()
    return {
        "tool": "cdb",
        "path": path,
        "command": cmd,
        "sections": sections,
        "output": output[:30000],
    }


def _analyze_with_minidump(path: str) -> dict:
    try:
        from minidump.minidumpfile import MinidumpFile  # type: ignore
    except Exception as exc:
        return {
            "tool": "none",
            "path": path,
            "message": (
                "No dump analyser available. Install the Windows SDK Debugging "
                "Tools (cdb.exe) or `pip install minidump`."
            ),
            "error": str(exc),
        }

    try:
        dump = MinidumpFile.parse(path)
        result: dict = {"tool": "minidump", "path": path}
        exc_obj = getattr(dump, "exception", None)
        record = getattr(exc_obj, "exception_record", None)
        code = getattr(record, "ExceptionCode", None)
        address = getattr(record, "ExceptionAddress", None)
        thread_id = getattr(exc_obj, "thread_id", None) if exc_obj else None
        if code is None:
            code = getattr(exc_obj, "ExceptionCode", None)
        if address is None:
            address = getattr(exc_obj, "ExceptionAddress", None)
        code_u = int(code) & 0xFFFFFFFF if code is not None else None
        result.update(
            {
                "exception_code": f"0x{code_u:08X}" if code_u is not None else None,
                "exception_name": EXCEPTION_NAMES.get(code_u) if code_u is not None else None,
                "exception_address": f"0x{int(address):X}" if address is not None else None,
                "thread_id": thread_id,
            }
        )

        modules = []
        try:
            modules = list(getattr(getattr(dump, "modules", None), "modules", []) or [])
        except Exception:
            modules = []
        result["module_count"] = len(modules)
        if address is not None:
            addr = int(address)
            for module in modules:
                try:
                    base = int(getattr(module, "baseaddress", 0) or 0)
                    size = int(getattr(module, "size", 0) or 0)
                    if base and base <= addr < base + max(size, 1):
                        name = getattr(module, "name", None) or getattr(
                            module, "filename", None
                        )
                        result["faulting_module"] = {
                            "name": name,
                            "base": f"0x{base:X}",
                            "offset": f"0x{addr - base:X}",
                        }
                        break
                except Exception:
                    continue
        if "faulting_module" not in result:
            result["faulting_module"] = None
        return result
    except Exception as exc:
        return {"tool": "minidump", "path": path, "error": f"minidump parse failed: {exc}"}


def analyze_dump(path: str) -> dict:
    """Analyse a ``.dmp`` with cdb.exe if available, then the minidump package."""
    try:
        if not os.path.isfile(path):
            return {"tool": "none", "path": path, "error": "dump file not found"}
        cdb = _find_cdb()
        if cdb:
            return _analyze_with_cdb(path, cdb)
        return _analyze_with_minidump(path)
    except Exception as exc:
        return {"tool": "none", "path": path, "error": str(exc)}


# ---------------------------------------------------------------------------
# New-file collection
# ---------------------------------------------------------------------------


def _kind_for(path: Path) -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name == "report.wer":
        return "wer"
    if suffix == ".dmp":
        return "dump"
    if suffix == ".txt":
        return "error_txt"
    if suffix == ".mdmp":
        return "dump"
    if suffix == ".log":
        return "log"
    if "error" in str(path.parent).lower():
        return "error_txt"
    return "other"


def _scan_new_files(dirs: list[str], since_ts: float, limit: int = 100) -> list[dict]:
    files: list[dict] = []
    seen: set[str] = set()
    for directory in dirs:
        try:
            base = Path(directory)
            if not base.is_dir():
                continue
            count = 0
            for path in base.rglob("*"):
                count += 1
                if count > 5000:
                    break
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                    if stat.st_mtime < since_ts - 2:
                        continue
                    key = str(path).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    files.append(
                        {
                            "path": str(path),
                            "mtime": datetime.fromtimestamp(
                                stat.st_mtime, tz=timezone.utc
                            ).isoformat(),
                            "mtime_ts": stat.st_mtime,
                            "size": stat.st_size,
                            "kind": _kind_for(path),
                        }
                    )
                except Exception:
                    continue
        except Exception:
            continue
    files.sort(key=lambda f: f.get("mtime_ts", 0), reverse=True)
    for item in files:
        item.pop("mtime_ts", None)
    return files[:limit]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _verdict(state: dict, parsed: list[dict], new_files: list[dict], wer: list[dict]) -> str:
    parts: list[str] = []
    name = state.get("exit_code_name")
    code = state.get("exit_code_hex")
    if state.get("state") == "crashed":
        if code:
            parts.append(f"Game crashed with {code} ({name}).")
        else:
            parts.append("Game crashed (no exit code available).")
    elif state.get("state") == "hung":
        parts.append("Game window is not responding (hung).")
    elif state.get("state") == "exited":
        parts.append(
            f"Game exited" + (f" with {code} ({name})." if code else ".")
        )
    elif state.get("state") == "running":
        parts.append("Game is running; no crash detected.")
    else:
        parts.append("Game was not started or is no longer tracked.")

    dialog = state.get("fatal_dialog")
    if dialog and dialog.get("text"):
        first = " ".join(dialog["text"].split())
        parts.append(f"Fatal dialog: {first[:300]}")

    for item in parsed[:3]:
        if item.get("exception_name") or item.get("exception_code"):
            line = f"Error log reports {item.get('exception_name') or 'exception'}"
            if item.get("exception_code"):
                line += f" {item['exception_code']}"
            if item.get("address"):
                line += f" at {item['address']}"
            parts.append(line + ".")
            if item.get("script_refs"):
                parts.append("Script reference: " + item["script_refs"][0][:200])

    if new_files:
        kinds: dict[str, int] = {}
        for item in new_files:
            kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
        parts.append(f"New files since session start: {summary}.")
    if wer:
        parts.append(f"{len(wer)} WER report(s) found.")
    return " ".join(parts)


def crash_report(
    since: datetime | str | None = None, instance: str | None = None
) -> dict:
    """Collect everything known about crashes since *since* (default: start of
    *instance*, or the current instance when none is given)."""
    from . import game  # local import avoids a cycle

    session = game.get_session(instance)
    instance_name = instance or (session.instance if session else None)
    since_dt = _as_utc(
        since,
        default=session.start_time if session and session.start_time else None,
    )
    since_ts = since_dt.timestamp()

    try:
        state = game.status(instance)
    except Exception as exc:
        state = {"state": "unknown", "error": str(exc)}

    # Every game writes to the same %USERPROFILE%\Documents\Warcraft III\Errors
    # directories, so with several instances alive a file cannot be attributed
    # to one of them.
    shared_error_dirs = len(getattr(game, "_instances", {})) > 1

    exe = session.exe if session else None
    dirs = error_dirs(exe)
    new_files = _scan_new_files(dirs, since_ts)
    for dump in crash_dumps(exe, since_dt):
        if not any(f["path"] == dump for f in new_files):
            try:
                stat = os.stat(dump)
                new_files.append(
                    {
                        "path": dump,
                        "mtime": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                        "size": stat.st_size,
                        "kind": "dump",
                    }
                )
            except Exception:
                pass

    wer = wer_reports(since_dt)
    for report in wer:
        path = report.get("path")
        if path and not any(f["path"] == path for f in new_files):
            try:
                stat = os.stat(path)
                new_files.append(
                    {
                        "path": path,
                        "mtime": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                        "size": stat.st_size,
                        "kind": "wer",
                    }
                )
            except Exception:
                pass
    new_files.sort(key=lambda f: f.get("mtime") or "", reverse=True)

    parsed: list[dict] = []
    for item in new_files:
        if item["kind"] == "error_txt" and len(parsed) < 10:
            parsed.append(parse_error_txt(item["path"]))

    events = event_log(since_dt)
    result = {
        "instance": instance_name,
        "since": since_dt.isoformat(),
        "state": state.get("state"),
        "running": state.get("running"),
        "pid": state.get("pid"),
        "exit_code": state.get("exit_code"),
        "exit_code_hex": state.get("exit_code_hex"),
        "exit_code_name": state.get("exit_code_name"),
        "fatal_dialog": state.get("fatal_dialog"),
        "error_dirs": dirs,
        "new_files": new_files,
        "parsed": parsed,
        "wer": wer,
        "event_log": events,
        "verdict": _verdict(state, parsed, new_files, wer),
    }
    if shared_error_dirs:
        result["shared_error_dirs"] = True
        result["shared_error_note"] = (
            "Several game instances are registered and they share the same "
            "Errors directories; new files are reported since this "
            "instance's start time but cannot be attributed to it."
        )
    return result
