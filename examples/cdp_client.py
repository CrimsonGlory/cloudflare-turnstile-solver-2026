#!/usr/bin/env python3
"""Minimal Chrome DevTools Protocol client. Stdlib only."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse, urlunparse

from request_token import ws_close, ws_connect, ws_mask, ws_recv


def discover(cdp_http: str) -> dict:
    url = cdp_http.rstrip("/") + "/json/version"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode())


def list_targets(cdp_http: str) -> list[dict]:
    url = cdp_http.rstrip("/") + "/json/list"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError:
        with urllib.request.urlopen(cdp_http.rstrip("/") + "/json", timeout=5) as resp:
            return json.loads(resp.read().decode())


def pick_page(cdp_http: str) -> dict | None:
    pages = [
        t
        for t in list_targets(cdp_http)
        if t.get("type") == "page" and not str(t.get("url", "")).startswith("chrome://")
    ]
    if not pages:
        pages = [t for t in list_targets(cdp_http) if t.get("type") == "page"]
    return pages[0] if pages else None


def rewrite_ws(ws_url: str, cdp_http: str) -> str:
    """Chrome reports ws://127.0.0.1:9222/... even inside Docker.

    Rewrite the host/port to match the HTTP endpoint we actually reached.
    """
    parsed_ws = urlparse(ws_url)
    parsed_http = urlparse(cdp_http)
    return urlunparse(
        (
            "ws",
            parsed_http.netloc,
            parsed_ws.path,
            parsed_ws.params,
            parsed_ws.query,
            parsed_ws.fragment,
        )
    )


class CdpSession:
    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer
        self._next_id = 0

    @classmethod
    async def connect(cls, cdp_http: str) -> "CdpSession":
        target = pick_page(cdp_http)
        if not target or not target.get("webSocketDebuggerUrl"):
            raise RuntimeError(f"no Chrome page target at {cdp_http}")
        ws_url = rewrite_ws(target["webSocketDebuggerUrl"], cdp_http)
        reader, writer = await ws_connect(ws_url)
        return cls(reader, writer)

    async def close(self) -> None:
        await ws_close(self.writer)

    async def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> Any:
        import asyncio

        self._next_id += 1
        msg_id = self._next_id
        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        self.writer.write(ws_mask(json.dumps(payload).encode(), opcode=0x1))
        await self.writer.drain()
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"CDP {method} timed out")
            raw = await asyncio.wait_for(ws_recv(self.reader), timeout=remaining)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP {method}: {data['error']}")
                return data.get("result")

    async def evaluate(self, expression: str) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        inner = (result or {}).get("result") or {}
        if inner.get("subtype") == "error":
            raise RuntimeError(inner.get("description") or inner)
        return inner.get("value")


def default_cdp_http() -> str:
    return os.environ.get("CDP_URL", "http://127.0.0.1:9222")
