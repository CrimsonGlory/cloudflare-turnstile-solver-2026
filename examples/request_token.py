#!/usr/bin/env python3
"""Ask the token server for one Turnstile token.

No third-party packages. Talks the same binary WebSocket protocol as the
README helpers.

Typical flow:

    1. docker compose up
    2. wait until the logs show a solver registering
       ("[+] Solver N added to queue")
    3. python3 examples/request_token.py
    4. python3 examples/request_token.py --count   # how many browsers are ready

The Docker Chrome window must already be on TARGET_URL with the harvester
override loaded. This script does not open a browser — it only requests a
solve from whatever solvers are already connected.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import struct
import sys
from typing import Mapping
from urllib.parse import urlparse


def build_solve_request(
    proxy_idx: int = 0,
    user_agent: str = "",
    fields: Mapping[str, str] | None = None,
) -> bytes:
    """Header 1 — on-demand solve request.

    Layout (little-endian):
      1
      proxy_idx          u32
      user_agent_len     u8
      user_agent         bytes
      repeated:
          name_len u8 + name + value_len u8 + value
    """
    packet = bytearray()
    packet.append(1)
    packet.extend(struct.pack("<I", proxy_idx))
    ua = user_agent.encode("utf-8")
    if len(ua) > 255:
        raise ValueError("user_agent must be <= 255 bytes")
    packet.append(len(ua))
    packet.extend(ua)
    for name, value in (fields or {}).items():
        nb = str(name).encode("utf-8")
        vb = str(value).encode("utf-8")
        if len(nb) > 255 or len(vb) > 255:
            raise ValueError(f"field {name!r} name/value must be <= 255 bytes")
        packet.append(len(nb))
        packet.extend(nb)
        packet.append(len(vb))
        packet.extend(vb)
    return bytes(packet)


def build_count_request() -> bytes:
    """Header 3 — how many solvers are currently idle."""
    return b"\x03"


def parse_response(data: bytes) -> dict:
    if len(data) == 1:
        return {"status": "no_solvers"}
    if len(data) == 4:
        (solver_idx,) = struct.unpack_from("<I", data, 0)
        return {"status": "failed", "solver_idx": solver_idx}
    if len(data) == 5:
        (count,) = struct.unpack_from("<I", data, 0)
        return {"status": "solver_count", "count": count}
    (solver_idx,) = struct.unpack_from("<I", data, 0)
    return {
        "status": "ok",
        "solver_idx": solver_idx,
        "token": data[4:].decode("utf-8", errors="replace"),
    }


# ---------------------------------------------------------------------------
# Tiny WebSocket client (clients must mask frames). Stdlib only.
# ---------------------------------------------------------------------------

async def ws_connect(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "http"}:
        raise ValueError(f"unsupported url: {url}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    reader, writer = await asyncio.open_connection(host, port)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    writer.write(request.encode("ascii"))
    await writer.drain()
    header = await reader.readuntil(b"\r\n\r\n")
    status = header.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raise RuntimeError(f"websocket handshake failed: {status!r}")
    return reader, writer


def ws_mask(payload: bytes, opcode: int = 0x2) -> bytes:
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", n))
    header.extend(key)
    return bytes(header) + masked


async def ws_recv(reader) -> bytes:
    h = await reader.readexactly(2)
    fin = h[0] & 0x80
    opcode = h[0] & 0x0F
    masked = h[1] & 0x80
    length = h[1] & 0x7F
    if length == 126:
        (length,) = struct.unpack(">H", await reader.readexactly(2))
    elif length == 127:
        (length,) = struct.unpack(">Q", await reader.readexactly(8))
    mask = await reader.readexactly(4) if masked else b""
    data = bytearray(await reader.readexactly(length))
    if masked:
        for i, b in enumerate(data):
            data[i] = b ^ mask[i % 4]
    if opcode == 0x8:
        raise ConnectionError("server closed the websocket")
    if opcode == 0x9:
        return bytes(data)
    if opcode not in (0x1, 0x2):
        raise RuntimeError(f"unexpected websocket opcode {opcode}")
    if not fin:
        raise RuntimeError("fragmented frames are not supported")
    return bytes(data)


async def ws_close(writer) -> None:
    try:
        writer.write(ws_mask(b"", opcode=0x8))
        await writer.drain()
    except Exception:
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def rpc(url: str, packet: bytes, timeout: float) -> dict:
    reader, writer = await ws_connect(url)
    try:
        writer.write(ws_mask(packet))
        await writer.drain()
        raw = await asyncio.wait_for(ws_recv(reader), timeout=timeout)
        return parse_response(raw)
    finally:
        await ws_close(writer)


def parse_fields(items: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise argparse.ArgumentTypeError(
                f"field must be name=value, got {item!r}"
            )
        name, value = item.split("=", 1)
        fields[name] = value
    return fields


async def main_async(args: argparse.Namespace) -> int:
    if args.count:
        result = await rpc(args.url, build_count_request(), args.timeout)
        if result["status"] != "solver_count":
            print(f"unexpected response: {result}", file=sys.stderr)
            return 1
        print(result["count"])
        if result["count"] == 0:
            print(
                "no solvers registered yet. Chrome must be on TARGET_URL "
                "with the harvester override loaded.",
                file=sys.stderr,
            )
            return 2
        return 0

    fields = parse_fields(args.field)
    result = await rpc(
        args.url,
        build_solve_request(args.proxy_idx, args.user_agent, fields),
        args.timeout,
    )

    if result["status"] == "ok":
        print(result["token"])
        return 0
    if result["status"] == "no_solvers":
        print(
            "no solvers available. Is Chrome up, and did the harvester "
            "connect? Try: python3 examples/request_token.py --count",
            file=sys.stderr,
        )
        return 2
    if result["status"] == "failed":
        print(
            f"solver {result['solver_idx']} failed to produce a token",
            file=sys.stderr,
        )
        return 3
    print(f"unexpected response: {result}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--url",
        default=os.environ.get("TOKEN_SERVER_URL", "ws://127.0.0.1:8080"),
        help="token server websocket (default: ws://127.0.0.1:8080)",
    )
    p.add_argument(
        "--proxy-idx",
        type=int,
        default=0,
        help="index into config/proxies.txt (ignored if USE_PROXY_SOLVING is false)",
    )
    p.add_argument(
        "--user-agent",
        default="",
        help="pin a solver by navigator.userAgent; empty = any solver",
    )
    p.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="extra field. Use action=... / cData=... for turnstile.render, "
        "or navigator.language=en-US / window.innerWidth=1920 for JS spoofs",
    )
    p.add_argument(
        "--count",
        action="store_true",
        help="print how many idle solvers are registered and exit",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="seconds to wait for a token (default: 60)",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except (ConnectionError, OSError, TimeoutError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
