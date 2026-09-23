"""Minimal WebSocket helpers (stdlib only)."""

from __future__ import annotations

import asyncio
import base64
import os
import struct
from urllib.parse import urlparse


async def ws_connect(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "http"}:
        raise ValueError(f"unsupported url: {url}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
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
