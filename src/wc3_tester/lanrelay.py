"""LAN game discovery between instances that each use their own 127.0.0.N.

Warcraft III finds LAN games with UDP broadcasts to port 6112.  When each
instance is bound to its own loopback address (by the built-in bind hook, or
by a user-installed launch wrapper such as ForceBindIP) those broadcasts reach
no other instance, so the games never show up in each other's LAN list.

This relay is a separate thread speaking the ordinary LAN protocol; it does not
touch the game processes.  Every second it asks each instance for its hosted
game (W3GS_SEARCHGAME, unicast to ``ip:6112``) from a socket bound to that same
``ip``, and forwards each reply (W3GS_GAMEINFO) unchanged to all other
instances from that socket.  The receiving game takes the host address from
the packet's source IP, so it joins ``ip`` at the TCP port the host advertised.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

log = logging.getLogger(__name__)

LAN_PORT = 6112
W3GS = 0xF7
SEARCHGAME, GAMEINFO = 0x2F, 0x30
PRODUCTS = (b"PX3W", b"3RAW")  # TFT, RoC (reversed four-cc, as on the wire)
DEFAULT_VERSIONS = tuple(range(24, 32))  # 1.24 .. 1.31


def searchgame_packet(product: bytes, version: int) -> bytes:
    body = product + struct.pack("<II", version, 0)
    return struct.pack("<BBH", W3GS, SEARCHGAME, 4 + len(body)) + body


def parse_gameinfo(data: bytes) -> dict | None:
    """Fields of a W3GS_GAMEINFO packet, or None if it is not one."""
    if len(data) < 20 or data[0] != W3GS or data[1] != GAMEINFO:
        return None
    if struct.unpack_from("<H", data, 2)[0] != len(data):
        return None
    product = data[4:8]
    version, host_counter = struct.unpack_from("<II", data, 8)
    end = data.find(b"\0", 20)
    name = data[20:end].decode("utf-8", "replace") if end > 0 else ""
    port = struct.unpack_from("<H", data, len(data) - 2)[0]
    return {"product": product[::-1].decode("ascii", "replace"), "version": version,
            "host_counter": host_counter, "name": name, "port": port}


class Relay:
    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self._socks: dict[str, socket.socket] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.version: int | None = None
        self.games: dict[str, dict] = {}  # host ip -> last GAMEINFO fields + time
        self.forwarded = 0
        self.errors: list[str] = []

    # -- membership -------------------------------------------------------
    def add(self, ip: str) -> None:
        with self._lock:
            if ip in self._socks:
                return
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, "SIO_UDP_CONNRESET"):
                # Do not turn an ICMP "port unreachable" (instance not on the
                # LAN screen yet) into an error on the next recv.
                s.ioctl(socket.SIO_UDP_CONNRESET, False)
            s.bind((ip, 0))
            s.setblocking(False)
            self._socks[ip] = s
        self.start()

    def remove(self, ip: str) -> None:
        with self._lock:
            s = self._socks.pop(ip, None)
            self.games.pop(ip, None)
        if s:
            s.close()

    def ips(self) -> list[str]:
        with self._lock:
            return list(self._socks)

    # -- thread -----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wc3-lan-relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            for s in self._socks.values():
                s.close()
            self._socks.clear()
            self.games.clear()

    def _search_packets(self) -> list[bytes]:
        # Keep searching every version, the last one heard first: a host
        # ignores searches for other versions, and a later test in the same
        # server may use a different client.
        versions = dict.fromkeys([v for v in (self.version,) if v] + list(DEFAULT_VERSIONS))
        return [searchgame_packet(p, v) for p in PRODUCTS for v in versions]

    def _run(self) -> None:
        next_search = 0.0
        while not self._stop.is_set():
            with self._lock:
                socks = dict(self._socks)
            now = time.time()
            if now >= next_search:
                next_search = now + self.interval
                packets = self._search_packets()
                for ip, s in socks.items():
                    for pkt in packets:
                        self._send(s, pkt, ip)
            for ip, s in socks.items():
                while True:
                    try:
                        data, src = s.recvfrom(4096)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError:
                        break
                    if src[0] != ip:
                        continue
                    info = parse_gameinfo(data)
                    if not info:
                        continue
                    self.version = info["version"]
                    info["seen"] = now
                    self.games[ip] = info
                    for other in socks:
                        if other != ip:
                            self._send(s, data, other)
                            self.forwarded += 1
            self._stop.wait(0.1)

    def _send(self, s: socket.socket, data: bytes, ip: str) -> None:
        try:
            s.sendto(data, (ip, LAN_PORT))
        except OSError as exc:
            if len(self.errors) < 20:
                self.errors.append(f"{ip}: {exc}")

    def status(self) -> dict:
        now = time.time()
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "instances": self.ips(),
            "lan_version": self.version,
            "games": {ip: {k: v for k, v in g.items() if k != "seen"} | {"age_sec": round(now - g["seen"], 1)}
                      for ip, g in self.games.items()},
            "forwarded_packets": self.forwarded,
            "errors": self.errors[-5:],
        }


_relay: Relay | None = None


def relay() -> Relay:
    global _relay
    if _relay is None:
        _relay = Relay()
    return _relay


def status() -> dict:
    return relay().status() if _relay else {"running": False, "instances": []}
