"""One wc3 server shared by every Codex chat.

Codex starts one stdio MCP server per chat.  Separate servers do not know each
other's game instances or LAN relay, so two chats could both call their first
game ``p1`` on 127.0.0.1.  Instead, each stdio process is a thin *proxy*: it
makes sure one background *daemon* is running and forwards ``tools/list`` and
``tools/call`` to it over MCP streamable HTTP on 127.0.0.1.

* The daemon writes ``%LOCALAPPDATA%\\wc3-tester\\daemon.json`` (pid, port, a
  random bearer token, code hash).  Requests without the token get 401.
* Tools run in worker threads, so a long ``wc3_wait`` in one chat does not
  stall the others; lifecycle tools (launch/attach/kill/...) are serialized.
* A daemon running older plugin code is replaced when it has no running game;
  otherwise it keeps serving until its games are gone.
* The daemon exits after ``WC3_DAEMON_IDLE_MIN`` minutes (default 30) without
  requests and without running games; the next call starts a new one.
* ``WC3_SHARED_SERVER=0`` turns sharing off (one in-process server per chat).
  If the daemon cannot be started, the proxy falls back to that as well.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

log = logging.getLogger(__name__)

PKG_DIR = Path(__file__).resolve().parent
SERIAL_TOOLS = {
    "wc3_launch", "wc3_launch_lan", "wc3_attach", "wc3_kill", "wc3_select",
    "wc3_set_launch_wrapper",
}
START_TIMEOUT_SEC = 40


def state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / "wc3-tester"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_file() -> Path:
    return state_dir() / "daemon.json"


def code_hash() -> str:
    h = hashlib.sha1()
    for path in sorted(PKG_DIR.glob("*.py")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _pid_alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).is_running()
    except Exception:
        return False


def _read_state() -> dict | None:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except Exception:
        return None


def _health(state: dict, timeout: float = 3.0) -> dict | None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{state['port']}/health",
        headers={"Authorization": f"Bearer {state['token']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


def _running_instances() -> int:
    from . import game

    try:
        return sum(1 for item in game.list_instances() if item.get("state") in ("running", "hung"))
    except Exception:
        return 0


def _threaded_tools(mcp) -> None:
    """Run sync tools in worker threads; serialize the lifecycle ones."""
    lock = threading.Lock()
    for tool in mcp._tool_manager.list_tools():
        if tool.is_async:
            continue
        fn = tool.fn

        def call(fn=fn, serial=tool.name in SERIAL_TOOLS, **kwargs):
            if serial:
                with lock:
                    return fn(**kwargs)
            return fn(**kwargs)

        async def wrapper(_call=call, **kwargs):
            import anyio

            return await anyio.to_thread.run_sync(functools.partial(_call, **kwargs))

        tool.fn = wrapper
        tool.is_async = True


def run_daemon(mcp) -> None:
    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    logging.basicConfig(
        filename=str(state_dir() / "daemon.log"), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True,
    )
    logging.getLogger("mcp").setLevel(logging.WARNING)  # one line per request otherwise
    token = secrets.token_urlsafe(24)
    my_code = code_hash()
    started = time.time()
    last_request = [time.time()]
    idle_sec = float(os.environ.get("WC3_DAEMON_IDLE_MIN", "30")) * 60

    # Load every module now: Codex may replace the plugin files while this
    # server runs, and a later lazy import would then mix old and new code.
    import importlib

    for path in sorted(PKG_DIR.glob("*.py")):
        if path.stem != "__init__":
            with contextlib.suppress(Exception):
                importlib.import_module(f"{__package__}.{path.stem}")
    _threaded_tools(mcp)
    app = mcp.streamable_http_app()

    async def health(_request):
        return JSONResponse({
            "pid": os.getpid(), "code": my_code, "started": started,
            "running_instances": _running_instances(),
        })

    app.router.routes.append(Route("/health", health))
    expected = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            if dict(scope.get("headers") or []).get(b"authorization") != expected:
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            last_request[0] = time.time()
        await app(scope, receive, send)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    tmp = state_file().with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": token,
                               "code": my_code, "started": started}), encoding="utf-8")
    os.replace(tmp, state_file())
    log.info("daemon pid %s on 127.0.0.1:%s (code %s)", os.getpid(), port, my_code)

    def watchdog():
        while True:
            time.sleep(30)
            if time.time() - last_request[0] > idle_sec and _running_instances() == 0:
                log.info("idle for %.0f min with no running game; exiting", idle_sec / 60)
                _drop_state_if_mine()
                os._exit(0)

    def _drop_state_if_mine():
        st = _read_state()
        if st and st.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                state_file().unlink()

    threading.Thread(target=watchdog, daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(guarded, log_level="warning", lifespan="on"))
    try:
        server.run(sockets=[sock])
    finally:
        _drop_state_if_mine()


# ---------------------------------------------------------------------------
# Starting / finding the daemon (proxy side)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _start_lock():
    import msvcrt

    fh = open(state_dir() / "daemon.lock", "a+b")
    try:
        deadline = time.time() + START_TIMEOUT_SEC + 10
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError("another wc3 proxy is starting the shared server")
                time.sleep(0.2)
        try:
            yield
        finally:
            fh.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        fh.close()


def _spawn_daemon() -> None:
    """Start the daemon through WMI, outside this chat's process tree.

    MCP clients end a stdio server's whole process tree when a chat closes,
    detached children included, so the daemon must not be our child.  WMI does
    not pass our environment; it is handed over in a file the daemon deletes.
    """
    src = str(PKG_DIR.parent)
    env_file = state_dir() / f"daemon-env-{os.getpid()}.json"
    env_file.write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    code = (f"import sys, os, json; sys.path.insert(0, {src!r}); "
            f"p = {str(env_file)!r}; os.environ.update(json.load(open(p))); os.remove(p); "
            "from wc3_tester.server import main; main()")
    from . import launcher

    # Working folder outside the plugin cache: a process whose current
    # directory is inside the cache blocks Codex from replacing the plugin.
    launcher._wmi_create(subprocess.list2cmdline([sys.executable, "-c", code, "--daemon"]), str(state_dir()))


def ensure_daemon() -> dict:
    """Return the state of a healthy daemon running this code (starting one if needed)."""
    st = _read_state()
    if st and _pid_alive(st.get("pid", 0)):
        h = _health(st)
        if h and (h.get("code") == code_hash() or h.get("running_instances")):
            return st
    with _start_lock():
        st = _read_state()
        if st and _pid_alive(st.get("pid", 0)):
            h = _health(st)
            if h and h.get("code") == code_hash():
                return st
            if h and h.get("running_instances"):
                log.warning("shared server runs older code but has running games; using it")
                return st
            # Outdated and idle, or unresponsive: replace it.
            with contextlib.suppress(Exception):
                import psutil

                psutil.Process(st["pid"]).kill()
        with contextlib.suppress(OSError):
            state_file().unlink()
        _spawn_daemon()
        deadline = time.time() + START_TIMEOUT_SEC
        while time.time() < deadline:
            st = _read_state()
            if st and _health(st):
                return st
            time.sleep(0.3)
    raise RuntimeError("the shared wc3 server did not start; see %LOCALAPPDATA%\\wc3-tester\\daemon.log")


# ---------------------------------------------------------------------------
# Proxy (stdio, one per chat)
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _client(st: dict):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        f"http://127.0.0.1:{st['port']}/mcp",
        headers={"Authorization": f"Bearer {st['token']}"},
        timeout=30, sse_read_timeout=1800,
    ) as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=1800)) as session:
            await session.initialize()
            yield session


async def _proxy_main() -> None:
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server("wc3")
    tools_cache: list[types.Tool] = []

    async def daemon_state() -> dict:
        return await anyio.to_thread.run_sync(ensure_daemon)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        st = await daemon_state()
        async with _client(st) as session:
            tools_cache[:] = (await session.list_tools()).tools
        return list(tools_cache)

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        st = await daemon_state()
        async with _client(st) as session:
            return await session.call_tool(name, arguments or {})

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def run_proxy(fallback) -> None:
    try:
        ensure_daemon()
    except Exception as exc:
        print(f"[wc3-tester] shared server unavailable ({exc}); running a private one", file=sys.stderr)
        fallback()
        return
    # Each forwarded call would otherwise log ~6 HTTP/session lines to stderr.
    for name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    asyncio.run(_proxy_main())


def info() -> dict:
    """What the server answering this call is (for wc3_instances)."""
    st = _read_state() or {}
    return {"pid": os.getpid(), "shared": st.get("pid") == os.getpid(), "code": code_hash()}
