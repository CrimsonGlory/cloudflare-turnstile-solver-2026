#!/usr/bin/env python3
"""Cookie API pool tests. Fake browsers; no display and no Cloudflare."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cookie_server import server
from cookie_server.cdp_browser import CdpBrowser
from cookie_server.pool import BrowserPool, SlotBusyError, discover_cdp_urls


class InFlight:
    def __init__(self) -> None:
        self.current = 0
        self.max = 0
        self._lock = threading.Lock()

    def inc(self) -> None:
        with self._lock:
            self.current += 1
            self.max = max(self.max, self.current)

    def dec(self) -> None:
        with self._lock:
            self.current -= 1


class FakeBrowser:
    def __init__(
        self,
        slot_id: str,
        *,
        delay: float = 0.05,
        hold: threading.Event | None = None,
        error: BaseException | None = None,
        in_flight: InFlight | None = None,
    ) -> None:
        self.slot_id = slot_id
        self.delay = delay
        self.hold = hold
        self.error = error
        self.in_flight = in_flight
        self.started = threading.Event()
        self.finished = threading.Event()
        self._closed = False
        self.timeouts: list[float] = []
        self.calls: list[tuple[str, str]] = []
        self.intervals: list[tuple[float, float]] = []

    async def close(self) -> None:
        self._closed = True

    async def _work(self, kind: str, url: str, timeout: float, min_wait: float) -> None:
        self.calls.append((kind, url))
        self.timeouts.append(timeout)
        self.started.set()
        if self.in_flight:
            self.in_flight.inc()
        started = time.monotonic()
        try:
            if self.hold is not None:
                while not self.hold.is_set():
                    await asyncio.sleep(0.01)
            elif self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
        finally:
            if self.in_flight:
                self.in_flight.dec()
            self.intervals.append((started, time.monotonic()))
            self.finished.set()

    def _marker(self, url: str) -> str:
        return f"{self.slot_id}|{url}"

    def _cookies(self, url: str) -> list[dict[str, Any]]:
        return [
            {
                "name": "slot",
                "value": self._marker(url),
                "domain": "example.com",
                "path": "/",
            }
        ]

    async def fetch_cookies(self, url: str, timeout: float = 60.0, min_wait: float = 0.0) -> list[dict[str, Any]]:
        await self._work("cookies", url, timeout, min_wait)
        return self._cookies(url)

    async def fetch_page(self, url: str, timeout: float = 60.0, min_wait: float = 0.0) -> dict[str, Any]:
        await self._work("page", url, timeout, min_wait)
        marker = self._marker(url)
        return {
            "url": url,
            "final_url": url,
            "status_code": 200,
            "headers": {"X-Slot": self.slot_id},
            "content": marker.encode("utf-8"),
            "cookies": self._cookies(url),
            "redirects": [],
        }


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {"error": exc.reason}
        except json.JSONDecodeError:
            body = {"error": raw}
        return exc.code, body, dict(exc.headers)


class CookieApiPoolTest(unittest.TestCase):
    def setUp(self) -> None:
        server._start_loop()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.CookieHandler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        self.httpd.server_close()
        self.thread.join(timeout=2)
        if server._pool is not None:
            try:
                server._run(server._pool.close())
            except Exception:
                pass
            server._pool = None

    def _install_pool(self, browsers: list[FakeBrowser], queue_timeout: float = 10.0) -> BrowserPool:
        mapping = {
            f"http://127.0.0.1:{9222 + index}": browser
            for index, browser in enumerate(browsers)
        }

        async def connect(url: str) -> FakeBrowser:
            browser = mapping[url]
            browser._closed = False
            return browser

        pool = BrowserPool(
            list(mapping),
            queue_timeout=queue_timeout,
            connect=connect,
            wait_cdp=None,
            connect_timeout=1.0,
            reconnect_wait=0.05,
        )
        server._pool = pool
        server._run(pool.start())
        return pool

    def _post(self, path: str, payload: dict[str, Any], timeout: float = 8.0):
        return _http_json("POST", self.base + path, payload, timeout=timeout)

    def _get(self, path: str, timeout: float = 5.0):
        return _http_json("GET", self.base + path, timeout=timeout)

    def test_health_ok(self) -> None:
        self._install_pool([FakeBrowser("A")])
        for path in ("/health", "/healthz"):
            status, body, _ = self._get(path)
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "ok")

    def test_two_slots_overlap_without_waiting_out_timeout(self) -> None:
        hold = threading.Event()
        inflight = InFlight()
        browsers = [
            FakeBrowser("A", hold=hold, in_flight=inflight),
            FakeBrowser("B", hold=hold, in_flight=inflight),
        ]
        self._install_pool(browsers, queue_timeout=2.0)
        payload_a = {"url": "https://a.example/1", "timeout": 30, "min_wait": 0}
        payload_b = {"url": "https://b.example/2", "timeout": 30, "min_wait": 0}

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(self._post, "/v1/get", payload_a)
            future_b = pool.submit(self._post, "/v1/get", payload_b)
            self._wait_started(browsers)
            self.assertGreaterEqual(inflight.max, 2)
            hold.set()
            status_a, body_a, _ = future_a.result(timeout=5)
            status_b, body_b, _ = future_b.result(timeout=5)

        self.assertEqual(status_a, 200)
        self.assertEqual(status_b, 200)
        self.assertEqual(body_a["status_code"], 200)
        self.assertEqual(body_b["status_code"], 200)
        self.assertNotEqual(body_a["content_b64"], body_b["content_b64"])
        self.assertLess(inflight.max, 3)

    def test_slot_results_stay_isolated_on_same_host(self) -> None:
        hold = threading.Event()
        browsers = [FakeBrowser("A", hold=hold), FakeBrowser("B", hold=hold)]
        self._install_pool(browsers, queue_timeout=2.0)
        url = "https://example.com/shared"
        extra = {"url": url, "timeout": 15, "min_wait": 0, "cookies": [{"name": "x", "value": "y"}]}

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(self._post, "/v1/get", extra)
            future_b = pool.submit(self._post, "/v1/cookies", extra)
            self._wait_started(browsers)
            hold.set()
            status_a, body_a, _ = future_a.result(timeout=5)
            status_b, body_b, _ = future_b.result(timeout=5)

        self.assertEqual(status_a, 200)
        self.assertEqual(status_b, 200)
        get_marker = base64.b64decode(body_a["content_b64"]).decode("utf-8")
        get_cookie = body_a["cookies"][0]["value"]
        cookies_cookie = body_b["cookies"][0]["value"]
        self.assertEqual(get_marker, get_cookie)
        self.assertIn(get_marker, {browsers[0]._marker(url), browsers[1]._marker(url)})
        self.assertIn(cookies_cookie, {browsers[0]._marker(url), browsers[1]._marker(url)})
        self.assertNotEqual(get_cookie, cookies_cookie)
        self.assertTrue(get_cookie.startswith("A|") or get_cookie.startswith("B|"))
        self.assertTrue(cookies_cookie.startswith("A|") or cookies_cookie.startswith("B|"))

    def test_one_slot_second_call_waits_then_runs(self) -> None:
        browser = FakeBrowser("A", delay=0.2)
        self._install_pool([browser], queue_timeout=2.0)
        payload_one = {"url": "https://example.com/one", "timeout": 10, "min_wait": 0}
        payload_two = {"url": "https://example.com/two", "timeout": 10, "min_wait": 0}

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_one = pool.submit(self._post, "/v1/get", payload_one)
            time.sleep(0.05)
            future_two = pool.submit(self._post, "/v1/get", payload_two)
            status_one, body_one, _ = future_one.result(timeout=5)
            status_two, body_two, _ = future_two.result(timeout=5)

        self.assertEqual(status_one, 200)
        self.assertEqual(status_two, 200)
        self.assertEqual(len(browser.intervals), 2)
        first_start, first_end = browser.intervals[0]
        second_start, _second_end = browser.intervals[1]
        self.assertGreaterEqual(second_start, first_end - 0.02)
        self.assertGreaterEqual(first_end - first_start, 0.15)
        self.assertEqual(
            base64.b64decode(body_one["content_b64"]).decode(),
            "A|https://example.com/one",
        )
        self.assertEqual(
            base64.b64decode(body_two["content_b64"]).decode(),
            "A|https://example.com/two",
        )
        self.assertEqual(browser.timeouts, [10.0, 10.0])

    def test_busy_pool_returns_503_without_disturbing_in_flight(self) -> None:
        hold = threading.Event()
        browsers = [FakeBrowser("A", hold=hold), FakeBrowser("B", hold=hold)]
        self._install_pool(browsers, queue_timeout=0.4)
        payload = {"url": "https://example.com/x", "timeout": 20, "min_wait": 0}

        with ThreadPoolExecutor(max_workers=3) as pool:
            future_a = pool.submit(self._post, "/v1/get", {**payload, "url": "https://example.com/a"})
            future_b = pool.submit(self._post, "/v1/get", {**payload, "url": "https://example.com/b"})
            self._wait_started(browsers)
            status_health, body_health, _ = self._get("/health")
            self.assertEqual(status_health, 200)
            self.assertEqual(body_health["status"], "ok")
            started = time.monotonic()
            status_c, body_c, headers_c = self._post(
                "/v1/get",
                {**payload, "url": "https://example.com/c"},
                timeout=5,
            )
            waited = time.monotonic() - started
            self.assertEqual(status_c, 503)
            self.assertIn("error", body_c)
            self.assertTrue(str(body_c["error"]))
            retry_after = headers_c.get("Retry-After") or headers_c.get("retry-after")
            self.assertIsNotNone(retry_after)
            int(retry_after)
            self.assertLess(waited, 2.0)
            hold.set()
            status_a, body_a, _ = future_a.result(timeout=5)
            status_b, body_b, _ = future_b.result(timeout=5)

        self.assertEqual(status_a, 200)
        self.assertEqual(status_b, 200)
        marker_a = base64.b64decode(body_a["content_b64"]).decode()
        marker_b = base64.b64decode(body_b["content_b64"]).decode()
        self.assertCountEqual(
            [marker_a, marker_b],
            ["A|https://example.com/a", "B|https://example.com/b"],
        )
        self.assertNotEqual(marker_a, marker_b)

    def test_dropped_cdp_fails_only_that_slot(self) -> None:
        hold = threading.Event()
        browsers = [
            FakeBrowser("A", hold=hold, error=ConnectionError("CDP connection closed")),
            FakeBrowser("B", hold=hold),
        ]
        self._install_pool(browsers, queue_timeout=2.0)

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(
                self._post, "/v1/get", {"url": "https://example.com/a", "timeout": 10, "min_wait": 0}
            )
            future_b = pool.submit(
                self._post, "/v1/get", {"url": "https://example.com/b", "timeout": 10, "min_wait": 0}
            )
            self._wait_started(browsers)
            hold.set()
            status_a, body_a, _ = future_a.result(timeout=5)
            status_b, body_b, _ = future_b.result(timeout=5)

        statuses = {status_a, status_b}
        self.assertIn(500, statuses)
        self.assertIn(200, statuses)
        if status_a == 200:
            ok_body, err_body = body_a, body_b
        else:
            ok_body, err_body = body_b, body_a
        ok_marker = base64.b64decode(ok_body["content_b64"]).decode()
        self.assertTrue(ok_marker.startswith("B|https://example.com/"), ok_marker)
        self.assertEqual(ok_body["cookies"][0]["value"], ok_marker)
        self.assertIn("error", err_body)
        self.assertIn("CDP", str(err_body["error"]))

    def _wait_started(self, browsers: list[FakeBrowser], timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(browser.started.is_set() for browser in browsers):
                return
            time.sleep(0.01)
        raise AssertionError("expected all fake browsers to start; deadlock likely means requests were serialized")


class DiscoverCdpUrlsTest(unittest.TestCase):
    def test_sequential_ports_from_base(self) -> None:
        urls = discover_cdp_urls("http://127.0.0.1:9222", browser_count=4)
        self.assertEqual(
            urls,
            [
                "http://127.0.0.1:9222",
                "http://127.0.0.1:9223",
                "http://127.0.0.1:9224",
                "http://127.0.0.1:9225",
            ],
        )

    def test_browser_count_one(self) -> None:
        self.assertEqual(
            discover_cdp_urls("http://127.0.0.1:9222", browser_count=1),
            ["http://127.0.0.1:9222"],
        )


class FocusWindowsTest(unittest.TestCase):
    def test_focus_target_is_noop_when_disabled(self) -> None:
        calls: list[str] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append(method)
                return {}

            browser.call = call  # type: ignore[method-assign]
            await browser._focus_target("target", "session")

        asyncio.run(run())
        self.assertEqual(calls, [])

    def test_focus_target_activates_when_enabled(self) -> None:
        calls: list[str] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=True)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append(method)
                return {}

            browser.call = call  # type: ignore[method-assign]
            await browser._focus_target("target", "session")

        asyncio.run(run())
        self.assertEqual(calls, ["Target.activateTarget", "Page.bringToFront"])

    def test_clearance_loop_skips_bring_to_front_when_unfocused(self) -> None:
        calls: list[str] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append(method)
                return {}

            async def evaluate(expression: str, session_id: str):
                return {
                    "ready": "complete",
                    "title": "Example Domain",
                    "href": "https://example.com/",
                    "html": "<html><body>ok</body></html>",
                }

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            snapshot, timed_out = await browser._wait_for_page_clearance("t", "s", timeout=1.0)
            self.assertFalse(timed_out)
            self.assertEqual(snapshot["href"], "https://example.com/")

        asyncio.run(run())
        self.assertNotIn("Target.activateTarget", calls)
        self.assertNotIn("Page.bringToFront", calls)

    def test_click_turnstile_uses_cdp_mouse_events(self) -> None:
        calls: list[tuple[str, dict | None]] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append((method, params))
                return {}

            async def evaluate(expression: str, session_id: str):
                return {"x": 68, "y": 72, "width": 300, "height": 65}

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            clicked = await browser._click_turnstile("session")
            self.assertTrue(clicked)

        asyncio.run(run())
        methods = [name for name, _params in calls]
        self.assertEqual(methods, ["Input.dispatchMouseEvent"] * 3)
        self.assertEqual(calls[0][1]["type"], "mouseMoved")
        self.assertEqual(calls[1][1]["type"], "mousePressed")
        self.assertEqual(calls[2][1]["type"], "mouseReleased")
        self.assertNotIn("Target.activateTarget", methods)

    def test_multi_slot_pool_disables_focus(self) -> None:
        pool = BrowserPool(
            ["http://127.0.0.1:9222", "http://127.0.0.1:9223"],
            connect=lambda url: None,  # type: ignore[arg-type]
        )
        self.assertFalse(pool.focus_windows)

    def test_single_slot_pool_keeps_focus(self) -> None:
        pool = BrowserPool(["http://127.0.0.1:9222"], connect=lambda url: None)  # type: ignore[arg-type]
        self.assertTrue(pool.focus_windows)

    def test_slot_busy_error_retry_after(self) -> None:
        err = SlotBusyError("all browser slots are busy", retry_after=10)
        self.assertEqual(err.retry_after, 10)
        self.assertEqual(str(err), "all browser slots are busy")


if __name__ == "__main__":
    unittest.main()
