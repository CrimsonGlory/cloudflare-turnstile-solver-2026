"""HTTP API: load a URL in Chrome, return cookies, close the tab."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from cookie_server.cdp_browser import (
    CdpBrowser,
    default_cdp_http,
    load_browser_headers,
    wait_for_cdp,
)
from cookie_server.pool import (
    BrowserPool,
    SlotBusyError,
    discover_cdp_urls,
    parse_browser_count,
    parse_queue_timeout,
)

DEFAULT_PORT = 8081
DEFAULT_TIMEOUT = 60.0
DEFAULT_BROWSER_HEADERS_FILE = "/data/browser_headers.json"

_cdp_http = default_cdp_http()
_browser_headers_file = os.environ.get("BROWSER_HEADERS_FILE", DEFAULT_BROWSER_HEADERS_FILE)
_configured_browser_count = parse_browser_count()
_configured_queue_timeout = parse_queue_timeout()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_pool: BrowserPool | None = None
_pool_create_lock = threading.Lock()


def _start_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    if _loop is not None:
        return _loop

    loop = asyncio.new_event_loop()

    def runner() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=runner, name="cookie-server-loop", daemon=True)
    thread.start()
    _loop = loop
    _loop_thread = thread
    return loop


def _run(coro):
    loop = _start_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


def _load_browser_headers() -> dict[str, str]:
    return load_browser_headers(_browser_headers_file)


def _connect_factory(focus_windows: bool):
    async def connect(cdp_http: str) -> CdpBrowser:
        return await CdpBrowser.connect(cdp_http, focus_windows=focus_windows)

    return connect


async def _ensure_pool() -> BrowserPool:
    global _pool
    if _pool is None:
        with _pool_create_lock:
            if _pool is None:
                urls = discover_cdp_urls(_cdp_http, _configured_browser_count)
                _pool = BrowserPool(
                    urls,
                    queue_timeout=_configured_queue_timeout,
                    connect=_connect_factory(focus_windows=len(urls) <= 1),
                    wait_cdp=wait_for_cdp,
                )
    await _pool.start()
    return _pool


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url must use http or https")
    if not parsed.netloc:
        raise ValueError("url must include a host")


async def fetch_cookies_for_url(
    url: str,
    timeout: float,
    min_wait: float = 0.0,
) -> list[dict[str, Any]]:
    _validate_url(url)
    pool = await _ensure_pool()
    async with pool.hold() as browser:
        return await browser.fetch_cookies(url, timeout=timeout, min_wait=min_wait)


async def fetch_page_for_url(
    url: str,
    timeout: float,
    min_wait: float = 0.0,
) -> dict[str, Any]:
    _validate_url(url)
    pool = await _ensure_pool()
    async with pool.hold() as browser:
        return await browser.fetch_page(url, timeout=timeout, min_wait=min_wait)


class CookieHandler(BaseHTTPRequestHandler):
    server_version = "CookieServer/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[cookie-server] %s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(
        self,
        status: int,
        payload: dict,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in {"/health", "/healthz"}:
            payload: dict[str, Any] = {"status": "ok"}
            try:
                if _pool is not None:
                    payload.update(_pool.stats())
            except Exception:
                pass
            self._send_json(200, payload)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/v1/cookies":
            self._handle_cookies()
            return
        if self.path == "/v1/get":
            self._handle_get()
            return
        self._send_json(404, {"error": "not found"})

    def _parse_request_body(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return None, {"error": "invalid json"}
        return data, None

    def _parse_url_request(self, data: dict[str, Any]) -> tuple[str | None, float, float, dict[str, Any] | None]:
        url = str(data.get("url", "")).strip()
        timeout = float(data.get("timeout", DEFAULT_TIMEOUT))
        min_wait = float(data.get("min_wait", 0.0))
        if not url:
            return None, timeout, min_wait, {"error": "url is required"}
        if timeout <= 0:
            return None, timeout, min_wait, {"error": "timeout must be positive"}
        if min_wait < 0:
            return None, timeout, min_wait, {"error": "min_wait must be >= 0"}
        return url, timeout, min_wait, None

    def _send_call_error(self, exc: BaseException) -> bool:
        if isinstance(exc, ValueError):
            self._send_json(400, {"error": str(exc)})
            return True
        if isinstance(exc, SlotBusyError):
            self._send_json(
                503,
                {"error": str(exc)},
                extra_headers={"Retry-After": str(exc.retry_after)},
            )
            return True
        if isinstance(exc, TimeoutError):
            self._send_json(504, {"error": str(exc)})
            return True
        return False

    def _handle_cookies(self) -> None:
        data, err = self._parse_request_body()
        if err:
            self._send_json(400, err)
            return

        url, timeout, min_wait, err = self._parse_url_request(data or {})
        if err:
            self._send_json(400, err)
            return

        try:
            cookies = _run(fetch_cookies_for_url(url, timeout, min_wait))
        except Exception as exc:
            if self._send_call_error(exc):
                return
            self._send_json(500, {"error": str(exc)})
            return

        headers = _load_browser_headers()
        self._send_json(
            200,
            {
                "url": url,
                "cookies": cookies,
                "headers": headers,
                "user_agent": headers.get("User-Agent", ""),
            },
        )

    def _handle_get(self) -> None:
        data, err = self._parse_request_body()
        if err:
            self._send_json(400, err)
            return

        url, timeout, min_wait, err = self._parse_url_request(data or {})
        if err:
            self._send_json(400, err)
            return

        try:
            page = _run(fetch_page_for_url(url, timeout, min_wait))
        except Exception as exc:
            if self._send_call_error(exc):
                return
            self._send_json(500, {"error": str(exc)})
            return

        request_headers = _load_browser_headers()
        content = page.get("content") or b""
        if isinstance(content, str):
            content = content.encode("utf-8")

        self._send_json(
            200,
            {
                "url": page.get("url", url),
                "final_url": page.get("final_url", url),
                "status_code": page.get("status_code", 200),
                "headers": page.get("headers") or {},
                "content_b64": base64.b64encode(content).decode("ascii"),
                "cookies": page.get("cookies") or [],
                "request_headers": request_headers,
                "redirects": page.get("redirects") or [],
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("COOKIE_SERVER_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("COOKIE_SERVER_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument("--cdp", default=default_cdp_http())
    parser.add_argument(
        "--browsers",
        type=int,
        default=parse_browser_count(),
        help="Chrome processes to pool (BROWSER_COUNT). Ports are CDP_PORT, CDP_PORT+1, …",
    )
    parser.add_argument(
        "--queue-timeout",
        type=float,
        default=parse_queue_timeout(),
        help="Seconds to wait for a free slot before HTTP 503 (QUEUE_TIMEOUT)",
    )
    args = parser.parse_args()

    global _cdp_http, _configured_browser_count, _configured_queue_timeout
    _cdp_http = args.cdp
    _configured_browser_count = parse_browser_count(str(args.browsers))
    _configured_queue_timeout = parse_queue_timeout(str(args.queue_timeout))
    loop = _start_loop()
    asyncio.run_coroutine_threadsafe(_ensure_pool(), loop)

    print(
        f"[cookie-server] listening on http://{args.host}:{args.port} "
        f"(cdp={_cdp_http} browsers={_configured_browser_count} "
        f"queue_timeout={_configured_queue_timeout})",
        flush=True,
    )
    httpd = ThreadingHTTPServer((args.host, args.port), CookieHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if _pool is not None:
            try:
                _run(_pool.close())
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
