"""Chrome DevTools Protocol automation: open tab, load URL, collect cookies, close."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from cookie_server.ws import ws_close, ws_connect, ws_mask, ws_recv

CF_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "enable javascript and cookies",
)


def discover_browser_ws(cdp_http: str) -> str:
    url = cdp_http.rstrip("/") + "/json/version"
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read().decode())
    ws_url = data.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError(f"no browser WebSocket at {cdp_http}")
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


class CdpBrowser:
    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._event_waiters: list[tuple[str, str | None, asyncio.Future]] = []
        self._recv_task: asyncio.Task | None = None
        self._closed = False

    @classmethod
    async def connect(cls, cdp_http: str) -> "CdpBrowser":
        ws_url = discover_browser_ws(cdp_http)
        reader, writer = await ws_connect(ws_url)
        browser = cls(reader, writer)
        browser._recv_task = asyncio.create_task(browser._recv_loop())
        return browser

    async def close(self) -> None:
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        await ws_close(self.writer)

    def _dispatch_event(self, method: str, session_id: str | None, params: dict) -> None:
        remaining: list[tuple[str, str | None, asyncio.Future]] = []
        for waited_method, waited_session, fut in self._event_waiters:
            if fut.done():
                continue
            if waited_method != method:
                remaining.append((waited_method, waited_session, fut))
                continue
            if waited_session is not None and waited_session != session_id:
                remaining.append((waited_method, waited_session, fut))
                continue
            fut.set_result(params)
        self._event_waiters = remaining

    async def _recv_loop(self) -> None:
        try:
            while not self._closed:
                raw = await ws_recv(self.reader)
                data = json.loads(raw)
                msg_id = data.get("id")
                if msg_id is not None:
                    fut = self._pending.pop(msg_id, None)
                    if fut and not fut.done():
                        fut.set_result(data)
                    continue
                method = data.get("method")
                if method:
                    self._dispatch_event(method, data.get("sessionId"), data.get("params") or {})
        except (asyncio.CancelledError, ConnectionError):
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("CDP connection closed"))
            for _, _, fut in self._event_waiters:
                if not fut.done():
                    fut.set_exception(ConnectionError("CDP connection closed"))
            self._pending.clear()
            self._event_waiters.clear()

    async def call(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> Any:
        self._next_id += 1
        msg_id = self._next_id
        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        self.writer.write(ws_mask(json.dumps(payload).encode(), opcode=0x1))
        await self.writer.drain()
        try:
            data = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP {method} timed out after {timeout}s")
        if "error" in data:
            raise RuntimeError(f"CDP {method}: {data['error']}")
        return data.get("result")

    async def wait_for_event(
        self,
        method: str,
        session_id: str | None = None,
        timeout: float = 60.0,
    ) -> dict:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._event_waiters.append((method, session_id, fut))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._event_waiters = [
                item for item in self._event_waiters if item[2] is not fut
            ]
            raise TimeoutError(f"timed out waiting for {method}")

    async def evaluate(self, expression: str, session_id: str) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            session_id=session_id,
        )
        inner = (result or {}).get("result") or {}
        if inner.get("subtype") == "error":
            raise RuntimeError(inner.get("description") or inner)
        return inner.get("value")

    async def _wait_minimum(
        self,
        session_id: str,
        min_wait: float,
        deadline: float,
    ) -> None:
        if min_wait <= 0:
            return

        loop = asyncio.get_event_loop()
        wait_end = loop.time() + min_wait
        last_href = await self.evaluate("location.href || ''", session_id) or ""

        while loop.time() < wait_end:
            if loop.time() >= deadline:
                return
            await asyncio.sleep(0.25)
            href = await self.evaluate("location.href || ''", session_id) or ""
            if href == last_href:
                continue
            last_href = href
            try:
                remaining = min(min_wait, deadline - loop.time())
                if remaining > 0:
                    await self.wait_for_event(
                        "Page.loadEventFired",
                        session_id=session_id,
                        timeout=remaining,
                    )
            except TimeoutError:
                pass
            wait_end = loop.time() + min_wait

    async def fetch_cookies(
        self,
        url: str,
        timeout: float = 60.0,
        min_wait: float = 0.0,
    ) -> list[dict]:
        target_id: str | None = None
        session_id: str | None = None
        try:
            created = await self.call("Target.createTarget", {"url": "about:blank"})
            target_id = created["targetId"]
            attached = await self.call(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_id = attached["sessionId"]

            await self.call("Page.enable", session_id=session_id)
            await self.call("Network.enable", session_id=session_id)

            nav = await self.call(
                "Page.navigate",
                {"url": url},
                session_id=session_id,
                timeout=timeout,
            )
            if nav.get("errorText"):
                raise RuntimeError(f"navigation failed: {nav['errorText']}")

            try:
                await self.wait_for_event(
                    "Page.loadEventFired",
                    session_id=session_id,
                    timeout=timeout,
                )
            except TimeoutError:
                pass

            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                title = await self.evaluate("document.title || ''", session_id) or ""
                href = await self.evaluate("location.href || ''", session_id) or ""
                ready = await self.evaluate("document.readyState", session_id) or ""
                blob = f"{title}\n{href}".lower()
                on_cf = any(marker in blob for marker in CF_MARKERS)
                if ready == "complete" and not on_cf:
                    break
                await asyncio.sleep(0.5)

            await self._wait_minimum(session_id, min_wait, deadline)

            cookies_result = await self.call(
                "Network.getCookies",
                {"urls": [url]},
                session_id=session_id,
            )
            return list(cookies_result.get("cookies") or [])
        finally:
            if target_id:
                try:
                    await self.call("Target.closeTarget", {"targetId": target_id})
                except Exception:
                    pass


async def wait_for_cdp(cdp_http: str, timeout: float = 30.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    last: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            discover_browser_ws(cdp_http)
            return
        except Exception as exc:
            last = exc
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Chrome CDP not reachable at {cdp_http}: {last}")


def default_cdp_http() -> str:
    return os.environ.get("CDP_URL", "http://127.0.0.1:9222")
