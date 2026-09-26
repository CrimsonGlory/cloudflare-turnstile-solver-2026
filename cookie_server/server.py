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

DEFAULT_PORT = 8081
DEFAULT_TIMEOUT = 60.0
DEFAULT_BROWSER_HEADERS_FILE = "/data/browser_headers.json"

_cdp_http = default_cdp_http()
_browser_headers_file = os.environ.get("BROWSER_HEADERS_FILE", DEFAULT_BROWSER_HEADERS_FILE)
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_browser: CdpBrowser | None = None
_request_lock: asyncio.Lock | None = None


def _start_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread, _request_lock
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
    _request_lock = asyncio.Lock()
    return loop


def _run(coro):
    loop = _start_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


def _load_browser_headers() -> dict[str, str]:
    return load_browser_headers(_browser_headers_file)


async def _ensure_browser() -> CdpBrowser:
    global _browser
    if _browser is None:
        await wait_for_cdp(_cdp_http, timeout=30.0)
        _browser = await CdpBrowser.connect(_cdp_http)
    return _browser


async def fetch_cookies_for_url(
    url: str,
    timeout: float,
    min_wait: float = 0.0,
) -> list[dict[str, Any]]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url must use http or https")
    if not parsed.netloc:
        raise ValueError("url must include a host")

    assert _request_lock is not None
    async with _request_lock:
        browser = await _ensure_browser()
        try:
            return await browser.fetch_cookies(url, timeout=timeout, min_wait=min_wait)
        except (ConnectionError, RuntimeError, TimeoutError):
            if _browser is not None:
                try:
                    await _browser.close()
                except Exception:
                    pass
                globals()["_browser"] = None
            raise


async def fetch_page_for_url(
    url: str,
    timeout: float,
    min_wait: float = 0.0,
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url must use http or https")
    if not parsed.netloc:
        raise ValueError("url must include a host")

    assert _request_lock is not None
    async with _request_lock:
        browser = await _ensure_browser()
        try:
            return await browser.fetch_page(url, timeout=timeout, min_wait=min_wait)
        except (ConnectionError, RuntimeError, TimeoutError):
            if _browser is not None:
                try:
                    await _browser.close()
                except Exception:
                    pass
                globals()["_browser"] = None
            raise


class CookieHandler(BaseHTTPRequestHandler):
    server_version = "CookieServer/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[cookie-server] %s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in {"/health", "/healthz"}:
            self._send_json(200, {"status": "ok"})
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
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except TimeoutError as exc:
            self._send_json(504, {"error": str(exc)})
            return
        except Exception as exc:
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
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except TimeoutError as exc:
            self._send_json(504, {"error": str(exc)})
            return
        except Exception as exc:
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
    args = parser.parse_args()

    global _cdp_http
    _cdp_http = args.cdp
    _start_loop()

    print(
        f"[cookie-server] listening on http://{args.host}:{args.port} "
        f"(cdp={_cdp_http})",
        flush=True,
    )
    httpd = ThreadingHTTPServer((args.host, args.port), CookieHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
