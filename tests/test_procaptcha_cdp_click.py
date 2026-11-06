#!/usr/bin/env python3
"""Headed confirmation: CDP clicks a Prosopo-style checkbox in open shadow DOM.

The widget ignores untrusted (scripted) clicks, matching production Procaptcha.
A trusted Input.dispatchMouseEvent must fill procaptcha-response.

Requires google-chrome-stable (or $CHROME_BIN) and Xvfb. Skips otherwise.
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
    _PAGE_SNAPSHOT_JS,
    procaptcha_state,
    procaptcha_token,
    wait_for_cdp,
)

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
class ProcaptchaCdpClickTest(unittest.TestCase):
    def test_trusted_cdp_click_solves_procaptcha_checkbox(self) -> None:
        record = asyncio.run(self._run())
        print("\n[procaptcha-cdp-click] " + "; ".join(f"{k}={v}" for k, v in record.items()), flush=True)
        self.assertTrue(record["untrusted_before"], record)
        self.assertFalse(record["token_before"], record)
        self.assertEqual(record["inspect_before_kind"], "checkbox")
        self.assertTrue(record["solved"], record)
        self.assertEqual(record["token"], "procaptcha-test-token")
        self.assertTrue(record["trusted"], record)

    async def _run(self) -> dict:
        display = os.environ.get("PROSOPO_CLICK_DISPLAY", ":96")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        httpd.daemon_threads = True
        http_port = httpd.server_address[1]
        http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        http_thread.start()
        fixture_url = f"http://127.0.0.1:{http_port}/procaptcha_widget.html"

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
        tmp = tempfile.TemporaryDirectory(prefix="procaptcha-click-")
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
                    "--window-size=900,700",
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
                {"url": fixture_url},
                session_id=session_id,
                timeout=15.0,
            )
            if nav.get("errorText"):
                raise RuntimeError(nav["errorText"])
            try:
                await browser.wait_for_event(
                    "Page.loadEventFired",
                    session_id=session_id,
                    timeout=15.0,
                )
            except TimeoutError:
                pass

            # Scripted click is untrusted and must not mint a token.
            await browser.evaluate(
                """(() => {
                  const host = document.querySelector('.prosopo-checkbox');
                  const input = host && host.shadowRoot && host.shadowRoot.querySelector('input');
                  if (input) input.click();
                })()""",
                session_id,
            )
            untrusted_before = bool(
                await browser.evaluate("window.__untrusted === true", session_id)
            )
            token_before = await browser.evaluate(
                """(() => {
                  const el = document.querySelector('[name="procaptcha-response"]');
                  return el && el.value ? el.value : '';
                })()""",
                session_id,
            )
            inspect_before = procaptcha_state(
                await browser.evaluate(_PAGE_SNAPSHOT_JS, session_id) or {}
            )

            snapshot, timed_out = await browser._wait_for_page_clearance(
                target_id,
                session_id,
                timeout=8.0,
            )
            trusted = bool(await browser.evaluate("window.__trusted === true", session_id))
            return {
                "chrome": CHROME_BIN,
                "display": display,
                "fixture": fixture_url,
                "untrusted_before": untrusted_before,
                "token_before": token_before,
                "inspect_before_kind": inspect_before.get("kind"),
                "solved": not timed_out,
                "timed_out": timed_out,
                "token": procaptcha_token(snapshot),
                "trusted": trusted,
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
        time.sleep(0.4)


if __name__ == "__main__":
    unittest.main()
