#!/usr/bin/env python3
"""Forward 0.0.0.0:CDP_PROXY_PORT -> 127.0.0.1:CDP_PORT so the host can reach Chrome."""

from __future__ import annotations

import os
import select
import socket
import sys


def pipe(a: socket.socket, b: socket.socket) -> None:
    sockets = [a, b]
    while True:
        readable, _, errored = select.select(sockets, [], sockets, 60)
        if errored:
            break
        if not readable:
            continue
        for src in readable:
            dest = b if src is a else a
            data = src.recv(65536)
            if not data:
                return
            dest.sendall(data)


def main() -> int:
    listen_port = int(os.environ.get("CDP_PROXY_PORT", "9223"))
    dest_port = int(os.environ.get("CDP_PORT", "9222"))
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", listen_port))
    server.listen(32)
    print(f"[cdp-proxy] 0.0.0.0:{listen_port} -> 127.0.0.1:{dest_port}", flush=True)
    while True:
        client, _ = server.accept()
        try:
            upstream = socket.create_connection(("127.0.0.1", dest_port), timeout=5)
        except OSError as exc:
            print(f"[cdp-proxy] upstream failed: {exc}", flush=True)
            client.close()
            continue
        pid = os.fork()
        if pid == 0:
            try:
                pipe(client, upstream)
            finally:
                client.close()
                upstream.close()
                os._exit(0)
        client.close()
        upstream.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
