#!/usr/bin/env python3
"""Headed confirmation: CDP mouse events hit an unfocused Chrome window.

Two Chrome processes share one Xvfb display. The OS-focused window is B.
A click is dispatched on A's CDP session without Target.activateTarget /
Page.bringToFront. A's fixture widget records the click; B does not.

Requires google-chrome-stable (or $CHROME_BIN) and Xvfb. Skips otherwise.
No Cloudflare: the fixture is tests/fixtures/click_target.html.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cookie_server.cdp_browser import CdpBrowser, wait_for_cdp

FIXTURE_DIR = ROOT / "tests" / "fixtures"
CHROME_BIN = os.environ.get("CHROME_BIN") or shutil.which("google-chrome-stable") or shutil.which("google-chrome")
XVFB_BIN = shutil.which("Xvfb")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURE_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        return


@unittest.skipUnless(CHROME_BIN and XVFB_BIN, "headed Chrome + Xvfb required")
class UnfocusedCdpClickTest(unittest.TestCase):
    def test_cdp_click_on_unfocused_window_does_not_hit_the_other(self) -> None:
        record = asyncio.run(self._run())
        print("\n[unfocused-cdp-click] " + "; ".join(f"{k}={v}" for k, v in record.items()), flush=True)
        self.assertTrue(record["a_clicked"], record)
        self.assertFalse(record["b_clicked"], record)
        self.assertFalse(record["a_focus_calls"], record)

    async def _run(self) -> dict:
        display = os.environ.get("UNFOCUSED_CLICK_DISPLAY", ":97")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        httpd.daemon_threads = True
        http_port = httpd.server_address[1]
        http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        http_thread.start()
        fixture_url = f"http://127.0.0.1:{http_port}/click_target.html"

        xvfb = subprocess.Popen(
            [
                XVFB_BIN,
                display,
                "-screen",
                "0",
                "1280x720x24",
                "-ac",
                "+extension",
                "RANDR",
                "-nolisten",
                "tcp",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        chromes: list[subprocess.Popen] = []
        browsers: list[CdpBrowser] = []
        tmp = tempfile.TemporaryDirectory(prefix="turnstile-unfocused-")
        try:
            self._wait_display(display)
            env = os.environ.copy()
            env["DISPLAY"] = display
            env["HOME"] = tmp.name

            ports = [_free_port(), _free_port()]
            for index, port in enumerate(ports):
                profile = Path(tmp.name) / f"chrome-{index}"
                profile.mkdir()
                chromes.append(
                    subprocess.Popen(
                        [
                            CHROME_BIN,
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--disable-gpu",
                            "--disable-dev-shm-usage",
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-backgrounding-occluded-windows",
                            "--disable-renderer-backgrounding",
                            "--remote-debugging-address=127.0.0.1",
                            f"--remote-debugging-port={port}",
                            "--remote-allow-origins=*",
                            f"--user-data-dir={profile}",
                            f"--window-position={index * 80},{index * 40}",
                            "--window-size=900,700",
                            "about:blank",
                        ],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )

            for port in ports:
                await wait_for_cdp(f"http://127.0.0.1:{port}", timeout=20.0)

            for port in ports:
                browsers.append(
                    await CdpBrowser.connect(
                        f"http://127.0.0.1:{port}",
                        focus_windows=False,
                    )
                )

            sessions = []
            for browser in browsers:
                created = await browser.call("Target.createTarget", {"url": "about:blank"})
                target_id = created["targetId"]
                attached = await browser.call(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                )
                session_id = attached["sessionId"]
                await browser.call("Page.enable", session_id=session_id)
                await browser.call("Runtime.enable", session_id=session_id)
                nav = await browser.call(
                    "Page.navigate",
                    {"url": fixture_url},
                    session_id=session_id,
                    timeout=15.0,
                )
                if nav.get("errorText"):
                    raise RuntimeError(nav["errorText"])
                try:
                    await browser.wait_for_event("Page.loadEventFired", session_id=session_id, timeout=15.0)
                except TimeoutError:
                    pass
                sessions.append((target_id, session_id))

            # Give B OS / CDP focus. A stays unfocused and we never bring it
            # to front — that is the condition the cookie-API pool relies on.
            await browsers[1].call("Target.activateTarget", {"targetId": sessions[1][0]})
            try:
                await browsers[1].call("Page.bringToFront", session_id=sessions[1][1])
            except Exception:
                pass
            await asyncio.sleep(0.4)

            a_focus_before = await browsers[0].evaluate("document.hasFocus()", sessions[0][1])
            b_focus_before = await browsers[1].evaluate("document.hasFocus()", sessions[1][1])

            focus_calls: list[str] = []
            original_call = browsers[0].call

            async def recording_call(method, params=None, session_id=None, timeout=30.0):
                if method in {"Target.activateTarget", "Page.bringToFront"}:
                    focus_calls.append(method)
                return await original_call(method, params=params, session_id=session_id, timeout=timeout)

            browsers[0].call = recording_call  # type: ignore[method-assign]
            clicked = await browsers[0]._click_turnstile(sessions[0][1])
            browsers[0].call = original_call  # type: ignore[method-assign]
            if not clicked:
                raise RuntimeError("CDP click did not find the fixture widget")

            deadline = time.monotonic() + 5.0
            a_clicked = False
            while time.monotonic() < deadline:
                a_clicked = bool(await browsers[0].evaluate("window.__clicked === true", sessions[0][1]))
                if a_clicked:
                    break
                await asyncio.sleep(0.1)

            b_clicked = bool(await browsers[1].evaluate("window.__clicked === true", sessions[1][1]))
            a_clicks = await browsers[0].evaluate("window.__clicks", sessions[0][1])
            a_status = await browsers[0].evaluate(
                "document.getElementById('status') ? document.getElementById('status').textContent : ''",
                sessions[0][1],
            )

            return {
                "chrome": CHROME_BIN,
                "display": display,
                "fixture": fixture_url,
                "clicked_method": "Input.dispatchMouseEvent",
                "widget_found": True,
                "a_clicked": a_clicked,
                "b_clicked": b_clicked,
                "a_status": a_status,
                "a_clicks": a_clicks,
                "a_hasFocus_before": a_focus_before,
                "b_hasFocus_before": b_focus_before,
                "a_focus_calls": focus_calls,
            }
        finally:
            for browser in browsers:
                try:
                    await browser.close()
                except Exception:
                    pass
            for proc in chromes:
                proc.terminate()
            xvfb.terminate()
            for proc in chromes + [xvfb]:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            httpd.shutdown()
            httpd.server_close()
            tmp.cleanup()

    def _wait_display(self, display: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["xdpyinfo", "-display", display],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if probe.returncode == 0:
                return
            time.sleep(0.1)
        # xdpyinfo may be missing; Xvfb still came up.
        time.sleep(0.4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
