"""Socket probe used by tests/test_bindhook.py.

The parent installs the built-in bind hook, then sends commands on stdin:
``bind``    bind a UDP socket to 0.0.0.0 and report the resulting address
``send``    send from an unbound UDP socket and report its local address
``connect`` connect a TCP socket without an explicit bind
``again``   bind again after the hook was uninstalled
"""

from __future__ import annotations

import json
import socket
import sys


def emit(**values) -> None:
    print(json.dumps(values), flush=True)


def main() -> int:
    # Make sure the parent knows the interpreter is up before it injects.
    emit(ready=True, python=sys.version.split()[0], bits=8 * __import__("struct").calcsize("P"))
    receiver_port = None
    for line in sys.stdin:
        command = line.strip()
        if not command:
            continue
        if command == "bind":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", 0))
            emit(bound=sock.getsockname()[0], port=sock.getsockname()[1])
            sock.close()
        elif command == "send":
            receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            receiver.bind(("127.0.0.1", 0))
            receiver_port = receiver.getsockname()[1]
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(b"probe", ("127.0.0.1", receiver_port))
            local_ip, local_port = sender.getsockname()
            data, source = receiver.recvfrom(64)
            emit(
                local_ip=local_ip,
                local_port=local_port,
                source_ip=source[0],
                source_port=source[1],
                payload=data.decode("ascii", "replace"),
            )
            sender.close()
            receiver.close()
        elif command == "connect":
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind(("0.0.0.0", 0))
            server.listen(1)
            host, port = server.getsockname()
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect((host, port))
            conn, peer = server.accept()
            emit(
                local_ip=client.getsockname()[0],
                peer_ip=peer[0],
                listener_ip=host,
                server_peer_ip=conn.getpeername()[0],
            )
            conn.close()
            client.close()
            server.close()
        elif command == "again":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", 0))
            emit(bound=sock.getsockname()[0], port=sock.getsockname()[1])
            sock.close()
        elif command == "quit":
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
