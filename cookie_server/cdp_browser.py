"""Chrome DevTools Protocol automation: open tab, load URL, collect cookies, close."""

from __future__ import annotations

import asyncio
import base64
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

BROWSER_HEADER_KEYS = (
    "User-Agent",
    "Accept-Language",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "sec-ch-ua-full-version-list",
)

_CAPTURE_HEADERS_JS = """(async () => {
  const out = {};
  out["User-Agent"] = navigator.userAgent;
  const langs = navigator.languages && navigator.languages.length
    ? navigator.languages
    : [navigator.language || "en-US"];
  out["Accept-Language"] = langs
    .map((lang, i) => (i === 0 ? lang : `${lang};q=${(1 - i * 0.1).toFixed(1)}`))
    .join(", ");
  const data = navigator.userAgentData;
  if (!data) {
    return out;
  }
  out["sec-ch-ua-mobile"] = data.mobile ? "?1" : "?0";
  out["sec-ch-ua-platform"] = `"${data.platform}"`;
  out["sec-ch-ua"] = data.brands
    .map((b) => `"${b.brand}";v="${b.version}"`)
    .join(", ");
  try {
    const hints = await data.getHighEntropyValues(["platformVersion", "fullVersionList"]);
    out["sec-ch-ua-platform-version"] = `"${hints.platformVersion}"`;
    out["sec-ch-ua-full-version-list"] = hints.fullVersionList
      .map((b) => `"${b.brand}";v="${b.version}"`)
      .join(", ");
  } catch (e) {}
  return out;
})()"""


def pick_browser_headers(raw: dict[str, str]) -> dict[str, str]:
    lower = {key.lower(): value for key, value in raw.items()}
    picked: dict[str, str] = {}
    for key in BROWSER_HEADER_KEYS:
        value = raw.get(key) or lower.get(key.lower())
        if value:
            picked[key] = str(value)
    return picked


def get_browser_user_agent(cdp_http: str) -> str:
    url = cdp_http.rstrip("/") + "/json/version"
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read().decode())
    ua = data.get("User-Agent")
    if not ua:
        raise RuntimeError(f"no User-Agent in CDP version at {cdp_http}")
    return str(ua)


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

    async def capture_document_headers(
        self,
        url: str,
        timeout: float = 15.0,
    ) -> dict[str, str]:
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

            captured: dict[str, str] = {}
            headers_found = asyncio.get_event_loop().create_future()

            async def collect_document_headers() -> None:
                deadline = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    try:
                        params = await self.wait_for_event(
                            "Network.requestWillBeSent",
                            session_id=session_id,
                            timeout=remaining,
                        )
                    except TimeoutError:
                        return

                    if params.get("type") != "Document":
                        continue

                    request = params.get("request") or {}
                    request_url = str(request.get("url") or "")
                    if request_url.rstrip("/") != url.rstrip("/"):
                        continue

                    picked = pick_browser_headers(request.get("headers") or {})
                    if picked and not headers_found.done():
                        headers_found.set_result(picked)
                        return

            collector = asyncio.create_task(collect_document_headers())
            try:
                nav = await self.call(
                    "Page.navigate",
                    {"url": url},
                    session_id=session_id,
                    timeout=timeout,
                )
                if nav.get("errorText"):
                    raise RuntimeError(f"navigation failed: {nav['errorText']}")

                try:
                    captured = await asyncio.wait_for(headers_found, timeout=timeout)
                except TimeoutError:
                    captured = {}
            finally:
                collector.cancel()
                try:
                    await collector
                except asyncio.CancelledError:
                    pass

            js_headers = pick_browser_headers(
                await self.evaluate(_CAPTURE_HEADERS_JS, session_id) or {}
            )
            for key, value in js_headers.items():
                captured.setdefault(key, value)
            return pick_browser_headers(captured)
        finally:
            if target_id:
                try:
                    await self.call("Target.closeTarget", {"targetId": target_id})
                except Exception:
                    pass

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

    async def _fetch_response_body(
        self,
        request_id: str | None,
        session_id: str,
    ) -> bytes:
        if not request_id:
            return b""
        try:
            body_result = await self.call(
                "Network.getResponseBody",
                {"requestId": request_id},
                session_id=session_id,
            )
        except Exception:
            return b""

        body = body_result.get("body") or ""
        if body_result.get("base64Encoded"):
            return base64.b64decode(body)
        if isinstance(body, str):
            return body.encode("utf-8")
        return bytes(body)

    async def fetch_page(
        self,
        url: str,
        timeout: float = 60.0,
        min_wait: float = 0.0,
    ) -> dict[str, Any]:
        """Load ``url`` and return status, headers, body, final URL, and cookies."""
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

            document_responses: list[dict[str, Any]] = []

            async def collect_document_responses() -> None:
                deadline = asyncio.get_event_loop().time() + timeout + min_wait + 5.0
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    try:
                        params = await self.wait_for_event(
                            "Network.responseReceived",
                            session_id=session_id,
                            timeout=min(remaining, 5.0),
                        )
                    except TimeoutError:
                        if asyncio.get_event_loop().time() >= deadline:
                            return
                        continue

                    if params.get("type") != "Document":
                        continue

                    response = params.get("response") or {}
                    document_responses.append(
                        {
                            "status": response.get("status", 200),
                            "headers": dict(response.get("headers") or {}),
                            "url": str(response.get("url") or ""),
                            "requestId": params.get("requestId"),
                        }
                    )

            collector = asyncio.create_task(collect_document_responses())
            try:
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

                final_url = await self.evaluate("location.href || ''", session_id) or url

                doc: dict[str, Any] = {}
                if document_responses:
                    for candidate in reversed(document_responses):
                        if candidate["url"].rstrip("/") == final_url.rstrip("/"):
                            doc = candidate
                            break
                    if not doc:
                        doc = document_responses[-1]

                status_code = int(doc.get("status") or 200)
                headers = dict(doc.get("headers") or {})
                content = await self._fetch_response_body(doc.get("requestId"), session_id)
                if not content:
                    html = await self.evaluate(
                        "document.documentElement ? document.documentElement.outerHTML : ''",
                        session_id,
                    ) or ""
                    content = html.encode("utf-8")

                cookies_result = await self.call(
                    "Network.getCookies",
                    {"urls": [url, final_url]},
                    session_id=session_id,
                )
                cookies = list(cookies_result.get("cookies") or [])

                return {
                    "url": url,
                    "final_url": final_url,
                    "status_code": status_code,
                    "headers": headers,
                    "content": content,
                    "cookies": cookies,
                    "redirects": document_responses[:-1] if len(document_responses) > 1 else [],
                }
            finally:
                collector.cancel()
                try:
                    await collector
                except asyncio.CancelledError:
                    pass
        finally:
            if target_id:
                try:
                    await self.call("Target.closeTarget", {"targetId": target_id})
                except Exception:
                    pass


def default_browser_headers_probe_url() -> str:
    port = os.environ.get("FILE_SERVER_PORT", "9377")
    override = os.environ.get("BROWSER_HEADERS_PROBE_URL")
    if override:
        return override
    return f"http://127.0.0.1:{port}/"


async def capture_browser_headers(
    cdp_http: str,
    probe_url: str | None = None,
    timeout: float = 30.0,
) -> dict[str, str]:
    probe = probe_url or default_browser_headers_probe_url()
    await wait_for_cdp(cdp_http, timeout=timeout)
    browser = await CdpBrowser.connect(cdp_http)
    try:
        headers = await browser.capture_document_headers(probe, timeout=timeout)
    finally:
        await browser.close()

    if "User-Agent" not in headers:
        headers["User-Agent"] = get_browser_user_agent(cdp_http)
    return pick_browser_headers(headers)


def save_browser_headers(path: str, headers: dict[str, str]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pick_browser_headers(headers), fh, indent=2, sort_keys=True)
        fh.write("\n")


def load_browser_headers(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return pick_browser_headers({str(key): str(value) for key, value in data.items()})


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
