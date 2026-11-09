#!/usr/bin/env python3
"""Live Procaptcha solves against js.prosopo.io / demo.prosopo.io.

Skipped unless headed Chrome, Xvfb, and PROSOPO_LIVE=1 are available.
Unknown dialogs (`kind=visual`) are left unsolved; image-grid and icon-order
demos are solved when CLIP / the icon matcher are available.
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

from cookie_server.cdp_browser import (  # noqa: E402
    CdpBrowser,
    procaptcha_state,
    procaptcha_token,
    wait_for_cdp,
)

FIXTURE_DIR = ROOT / "tests" / "fixtures"
CHROME_BIN = os.environ.get("CHROME_BIN") or shutil.which("google-chrome-stable") or shutil.which("google-chrome")
XVFB_BIN = shutil.which("Xvfb")
LIVE = os.environ.get("PROSOPO_LIVE") == "1"

POW_DEMO = "https://demo.prosopo.io/pow-implicit.html"
FRICTIONLESS_DEMO = "https://demo.prosopo.io/frictionless-implicit.html"
PUZZLE_DEMO = "https://demo.prosopo.io/puzzle-implicit.html"
IMAGE_DEMO = "https://demo.prosopo.io/image-implicit.html"
ICON_ORDER_DEMO = "https://demo.prosopo.io/icon-order-implicit.html"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURE_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        return


@unittest.skipUnless(LIVE and CHROME_BIN and XVFB_BIN, "set PROSOPO_LIVE=1 with headed Chrome + Xvfb")
class ProcaptchaLiveTest(unittest.TestCase):
    def test_always_pass_sitekey_emits_token(self) -> None:
        record = asyncio.run(self._solve_local("procaptcha_always_pass.html", timeout=25.0))
        print("\n[procaptcha-live always-pass] " + "; ".join(f"{k}={v}" for k, v in record.items()), flush=True)
        self.assertTrue(record["solved"], record)
        self.assertTrue(record["token"], record)
        self.assertNotEqual(record["kind"], "visual", record)

    def test_pow_demo_checkbox(self) -> None:
        record = asyncio.run(self._solve_url(POW_DEMO, timeout=30.0))
        print("\n[procaptcha-live pow] " + "; ".join(f"{k}={v}" for k, v in record.items()), flush=True)
        self.assertTrue(record["solved"], record)
        self.assertTrue(record["token"], record)
        self.assertNotEqual(record["kind"], "visual", record)

    def test_frictionless_demo(self) -> None:
        record = asyncio.run(self._solve_url(FRICTIONLESS_DEMO, timeout=30.0))
        print("\n[procaptcha-live frictionless] " + "; ".join(f"{k}={v}" for k, v in record.items()), flush=True)
        if record.get("kind") == "visual":
            self.skipTest(
                "frictionless detector served an unknown visual challenge"
            )
        self.assertTrue(record["solved"], record)
        self.assertTrue(record["token"], record)

    def test_puzzle_demo(self) -> None:
        record = asyncio.run(self._solve_url(PUZZLE_DEMO, timeout=45.0))
        print("\n[procaptcha-live puzzle] " + "; ".join(f"{k}={v}" for k, v in record.items()), flush=True)
        self.assertNotEqual(record["kind"], "visual", record)
        self.assertTrue(record["solved"], record)
        self.assertTrue(record["token"], record)

    def test_image_demo(self) -> None:
        record = asyncio.run(self._solve_url(IMAGE_DEMO, timeout=70.0))
        print("\n[procaptcha-live image] " + "; ".join(f"{k}={v}" for k, v in record.items()), flush=True)
        self.assertNotEqual(record["kind"], "visual", record)
        self.assertTrue(record["solved"], record)
        self.assertTrue(record["token"], record)

    def test_icon_order_demo(self) -> None:
        record = asyncio.run(self._solve_url(ICON_ORDER_DEMO, timeout=50.0))
        print("\n[procaptcha-live icon-order] " + "; ".join(f"{k}={v}" for k, v in record.items()), flush=True)
        self.assertNotEqual(record["kind"], "visual", record)
        self.assertTrue(record["solved"], record)
        self.assertTrue(record["token"], record)

    async def _solve_local(self, filename: str, timeout: float) -> dict:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        httpd.daemon_threads = True
        http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        http_thread.start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/{filename}"
        try:
            return await self._solve_url(url, timeout=timeout)
        finally:
            httpd.shutdown()
            httpd.server_close()

    async def _solve_url(self, url: str, timeout: float) -> dict:
        display = os.environ.get("PROSOPO_LIVE_DISPLAY", ":95")
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
        chrome: subprocess.Popen | None = None
        browser: CdpBrowser | None = None
        tmp = tempfile.TemporaryDirectory(prefix="procaptcha-live-")
        try:
            self._wait_display(display)
            env = os.environ.copy()
            env["DISPLAY"] = display
            env["HOME"] = tmp.name
            port = _free_port()
            profile = Path(tmp.name) / "chrome"
            profile.mkdir()
            chrome = subprocess.Popen(
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
                    "--window-size=1100,800",
                    "about:blank",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await wait_for_cdp(f"http://127.0.0.1:{port}", timeout=20.0)
            browser = await CdpBrowser.connect(
                f"http://127.0.0.1:{port}",
                focus_windows=False,
            )

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
                {"url": url},
                session_id=session_id,
                timeout=20.0,
            )
            if nav.get("errorText"):
                raise RuntimeError(nav["errorText"])
            try:
                await browser.wait_for_event(
                    "Page.loadEventFired",
                    session_id=session_id,
                    timeout=20.0,
                )
            except TimeoutError:
                pass

            snapshot, timed_out = await browser._wait_for_page_clearance(
                target_id,
                session_id,
                timeout=timeout,
            )
            state = procaptcha_state(snapshot)
            return {
                "url": url,
                "solved": not timed_out,
                "timed_out": timed_out,
                "token": procaptcha_token(snapshot),
                "kind": state.get("kind"),
                "present": state.get("present"),
                "href": snapshot.get("href"),
                "title": snapshot.get("title"),
            }
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if chrome:
                chrome.terminate()
            xvfb.terminate()
            for proc in [p for p in (chrome, xvfb) if p is not None]:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
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
        time.sleep(0.4)


if __name__ == "__main__":
    unittest.main()
