"""LAN relay check with fake games on 127.0.0.11 (host) and 127.0.0.12 (client).

The fake host answers W3GS_SEARCHGAME with a W3GS_GAMEINFO; the client must
receive that packet from the host's address.  Needs UDP port 6112 free on those
addresses.  Run:  python tests\\test_lanrelay.py
"""

import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wc3_tester import lanrelay  # noqa: E402

HOST, CLIENT, VERSION = "127.0.0.11", "127.0.0.12", 29


def gameinfo(name: str, port: int) -> bytes:
    body = b"PX3W" + struct.pack("<III", VERSION, 1, 0xABCD) + name.encode() + b"\0\0" + b"stat\0"
    body += struct.pack("<IIIIII", 12, 1, 1, 11, 12, 5) + struct.pack("<H", port)
    return struct.pack("<BBH", 0xF7, 0x30, 4 + len(body)) + body


def main() -> int:
    host = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    host.bind((HOST, 6112))
    host.settimeout(0.2)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind((CLIENT, 6112))
    client.settimeout(5)
    stop = threading.Event()
    searches: list[int] = []

    def serve():
        while not stop.is_set():
            try:
                data, src = host.recvfrom(100)
            except OSError:
                continue
            if data[:2] == b"\xf7\x2f":
                version = struct.unpack_from("<I", data, 8)[0]
                searches.append(version)
                if data[4:8] == b"PX3W" and version == VERSION:
                    host.sendto(gameinfo("relay test", 6112), src)

    threading.Thread(target=serve, daemon=True).start()
    relay = lanrelay.Relay(interval=0.5)
    relay.add(HOST)
    relay.add(CLIENT)
    checks = []
    try:
        data, src = client.recvfrom(4096)
        while data[1] == lanrelay.SEARCHGAME:  # the relay searches every instance; skip those
            data, src = client.recvfrom(4096)
        info = lanrelay.parse_gameinfo(data)
        checks.append(("client got GAMEINFO from the host address", src[0] == HOST, src))
        checks.append(("packet parsed", bool(info) and info["name"] == "relay test" and info["port"] == 6112, info))
    except socket.timeout:
        checks.append(("client got GAMEINFO from the host address", False, "timeout"))
    time.sleep(1.2)
    st = relay.status()
    checks.append(("version learned", st["lan_version"] == VERSION, st["lan_version"]))
    expected = set(lanrelay.DEFAULT_VERSIONS)
    checks.append(("every version still searched after one is learned",
                   set(searches[-2 * len(expected):]) == expected, searches[-4:]))
    relay.remove(HOST)
    checks.append(("host removed", relay.ips() == [CLIENT], relay.ips()))
    relay.stop()
    stop.set()
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
