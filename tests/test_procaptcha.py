#!/usr/bin/env python3
"""Procaptcha frictionless / PoW checkbox solving (mocked CDP)."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cookie_server.cdp_browser import (  # noqa: E402
    CdpBrowser,
    is_human_check_shell,
    page_still_challenged,
    pending_procaptcha,
    procaptcha_token,
    unresolved_procaptcha,
)

HUMAN_CHECK_HTML = (
    "<html><head><title>Confirm you are human</title></head><body>"
    '<p id="prompt">Please confirm you are human to open this link.</p>'
    '<div id="captcha" data-sitekey="test-key" data-sessionid="sess" '
    'data-sessiontoken="tok"></div>'
    '<script type="module" src="https://js.prosopo.io/js/procaptcha.bundle.js">'
    "</script></body></html>"
)
SHELL_HREF = "https://example.com/api/link/test"
DEST_HREF = "https://cdn.example.com/photo.jpg"


def _snapshot(
    *,
    ready: str = "complete",
    href: str = "https://example.com/",
    html: str = "<html><body><div class='procaptcha'></div></body></html>",
    procaptcha: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ready": ready,
        "title": "Example",
        "href": href,
        "html": html,
    }
    if procaptcha is not None:
        payload["procaptcha"] = procaptcha
    return payload


def _shell_snapshot(
    *,
    ready: str = "complete",
    href: str = SHELL_HREF,
    procaptcha: dict[str, Any] | None = None,
    html: str = HUMAN_CHECK_HTML,
    title: str = "Confirm you are human",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ready": ready,
        "title": title,
        "href": href,
        "html": html,
        "procaptcha": procaptcha
        if procaptcha is not None
        else {"present": False, "solved": False},
    }
    return payload


class ProcaptchaStateTest(unittest.TestCase):
    def test_pending_without_state_is_false(self) -> None:
        self.assertFalse(pending_procaptcha({"html": "<html></html>"}))
        self.assertFalse(pending_procaptcha(None))

    def test_pending_unsolved_widget(self) -> None:
        self.assertTrue(
            pending_procaptcha(
                {"procaptcha": {"present": True, "solved": False, "kind": "checkbox"}}
            )
        )

    def test_solved_widget_is_not_pending(self) -> None:
        snap = {
            "procaptcha": {
                "present": True,
                "solved": True,
                "kind": "solved",
                "token": "abc",
            }
        }
        self.assertFalse(pending_procaptcha(snap))
        self.assertEqual(procaptcha_token(snap), "abc")

    def test_unmounted_human_check_shell_is_unresolved(self) -> None:
        snap = _shell_snapshot()
        self.assertTrue(is_human_check_shell(snap))
        self.assertFalse(pending_procaptcha(snap))
        self.assertTrue(unresolved_procaptcha(snap))
        self.assertTrue(page_still_challenged(snap))

    def test_solved_token_on_human_check_shell_is_still_unresolved(self) -> None:
        snap = _shell_snapshot(
            procaptcha={
                "present": True,
                "solved": True,
                "kind": "solved",
                "token": "shell-token",
            }
        )
        self.assertTrue(is_human_check_shell(snap))
        self.assertFalse(pending_procaptcha(snap))
        self.assertTrue(unresolved_procaptcha(snap))

    def test_destination_page_is_not_a_human_check_shell(self) -> None:
        snap = _snapshot(
            href=DEST_HREF,
            html="<html><body>ok</body></html>",
            procaptcha={"present": False, "solved": False},
        )
        self.assertFalse(is_human_check_shell(snap))
        self.assertFalse(unresolved_procaptcha(snap))
        self.assertFalse(page_still_challenged(snap))

    def test_ordinary_bundle_page_clears_once_solved(self) -> None:
        html = (
            "<html><body><div class='procaptcha' data-sitekey='test-key'></div>"
            '<script src="https://js.prosopo.io/js/procaptcha.bundle.js"></script>'
            "</body></html>"
        )
        unsolved = _snapshot(
            html=html,
            procaptcha={"present": False, "solved": False},
        )
        solved = _snapshot(
            html=html,
            procaptcha={
                "present": True,
                "solved": True,
                "kind": "solved",
                "token": "ordinary-token",
            },
        )
        self.assertFalse(is_human_check_shell(unsolved))
        self.assertTrue(unresolved_procaptcha(unsolved))
        self.assertFalse(unresolved_procaptcha(solved))


class ProcaptchaClearanceTest(unittest.TestCase):
    def test_click_procaptcha_uses_cdp_mouse_events(self) -> None:
        calls: list[tuple[str, dict | None]] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append((method, params))
                return {}

            browser.call = call  # type: ignore[method-assign]
            clicked = await browser._click_procaptcha(
                "session",
                {"x": 54, "y": 69, "width": 28, "height": 28},
            )
            self.assertTrue(clicked)

        asyncio.run(run())
        methods = [name for name, _params in calls]
        self.assertEqual(methods, ["Input.dispatchMouseEvent"] * 3)
        self.assertEqual(calls[0][1]["type"], "mouseMoved")
        self.assertEqual(calls[1][1]["type"], "mousePressed")
        self.assertEqual(calls[2][1]["type"], "mouseReleased")
        self.assertEqual(calls[1][1]["x"], 54)
        self.assertEqual(calls[1][1]["y"], 69)

    def test_clearance_clicks_checkbox_then_returns_when_solved(self) -> None:
        clicks = {"n": 0}
        calls: list[str] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append(method)
                if method == "Input.dispatchMouseEvent" and (params or {}).get("type") == "mousePressed":
                    clicks["n"] += 1
                return {}

            async def evaluate(expression: str, session_id: str):
                if clicks["n"]:
                    return _snapshot(
                        procaptcha={
                            "present": True,
                            "solved": True,
                            "kind": "solved",
                            "token": "procaptcha-test-token",
                        }
                    )
                return _snapshot(
                    procaptcha={
                        "present": True,
                        "solved": False,
                        "kind": "checkbox",
                        "click": {"x": 50, "y": 55},
                    }
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=3.0
            )
            self.assertFalse(timed_out)
            self.assertEqual(procaptcha_token(snapshot), "procaptcha-test-token")

        asyncio.run(run())
        self.assertGreaterEqual(clicks["n"], 1)
        self.assertNotIn("Page.reload", calls)

    def test_clearance_waits_on_unsolved_widget(self) -> None:
        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                return {}

            async def evaluate(expression: str, session_id: str):
                return _snapshot(
                    procaptcha={"present": True, "solved": False, "kind": "pending"}
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            _snapshot_out, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=0.2
            )
            self.assertTrue(timed_out)

        asyncio.run(run())

    def test_visual_challenge_is_not_clicked(self) -> None:
        calls: list[str] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append(method)
                return {}

            async def evaluate(expression: str, session_id: str):
                return _snapshot(
                    procaptcha={
                        "present": True,
                        "solved": False,
                        "kind": "visual",
                        "click": {"x": 10, "y": 10},
                    }
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            _snapshot_out, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=0.2
            )
            self.assertTrue(timed_out)

        asyncio.run(run())
        self.assertNotIn("Input.dispatchMouseEvent", calls)
        self.assertNotIn("Page.reload", calls)

    def test_invisible_widget_is_executed_once(self) -> None:
        evaluates: list[str] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)
            executed = {"n": 0}

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                return {}

            async def evaluate(expression: str, session_id: str):
                evaluates.append(expression)
                if "procaptcha:execute" in expression:
                    executed["n"] += 1
                    return {"executed": True, "via": "api"}
                if executed["n"]:
                    return _snapshot(
                        procaptcha={
                            "present": True,
                            "solved": True,
                            "kind": "solved",
                            "token": "inv-token",
                        }
                    )
                return _snapshot(
                    procaptcha={"present": True, "solved": False, "kind": "invisible"}
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=3.0
            )
            self.assertFalse(timed_out)
            self.assertEqual(procaptcha_token(snapshot), "inv-token")
            self.assertEqual(executed["n"], 1)

        asyncio.run(run())

    def test_pending_spinner_is_not_clicked(self) -> None:
        calls: list[str] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append(method)
                return {}

            async def evaluate(expression: str, session_id: str):
                return _snapshot(
                    procaptcha={
                        "present": True,
                        "solved": False,
                        "kind": "pending",
                        "loading": True,
                    }
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            _snapshot_out, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=0.2
            )
            self.assertTrue(timed_out)

        asyncio.run(run())
        self.assertNotIn("Input.dispatchMouseEvent", calls)
        self.assertNotIn("Page.reload", calls)

    def test_plain_page_still_returns_immediately(self) -> None:
        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                return {}

            async def evaluate(expression: str, session_id: str):
                return _snapshot(
                    html="<html><body>ok</body></html>",
                    procaptcha={"present": False, "solved": False},
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=1.0
            )
            self.assertFalse(timed_out)
            self.assertEqual(snapshot["href"], "https://example.com/")

        asyncio.run(run())

    def test_human_check_shell_is_not_clearance_while_unmounted(self) -> None:
        evaluates = {"n": 0}

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                return {}

            async def evaluate(expression: str, session_id: str):
                evaluates["n"] += 1
                return _shell_snapshot()

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=0.3
            )
            self.assertTrue(timed_out)
            self.assertTrue(unresolved_procaptcha(snapshot))
            self.assertEqual(snapshot["title"], "Confirm you are human")

        asyncio.run(run())
        self.assertGreater(evaluates["n"], 1)

    def test_human_check_shell_clears_after_navigation(self) -> None:
        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)
            n = {"i": 0}

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                return {}

            async def wait_for_event(method: str, session_id=None, timeout: float = 60.0):
                return {}

            async def evaluate(expression: str, session_id: str):
                n["i"] += 1
                if n["i"] < 3:
                    return _shell_snapshot()
                return _snapshot(
                    href=DEST_HREF,
                    html="<html><body>ok</body></html>",
                    procaptcha={"present": False, "solved": False},
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            browser.wait_for_event = wait_for_event  # type: ignore[method-assign]
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=3.0
            )
            self.assertFalse(timed_out)
            self.assertEqual(snapshot["href"], DEST_HREF)
            self.assertFalse(unresolved_procaptcha(snapshot))

        asyncio.run(run())

    def test_human_check_shell_solved_token_without_navigation_is_not_clearance(
        self,
    ) -> None:
        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                return {}

            async def evaluate(expression: str, session_id: str):
                return _shell_snapshot(
                    procaptcha={
                        "present": True,
                        "solved": True,
                        "kind": "solved",
                        "token": "shell-token",
                    }
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=0.3
            )
            self.assertTrue(timed_out)
            self.assertTrue(unresolved_procaptcha(snapshot))
            self.assertEqual(snapshot["href"], SHELL_HREF)

        asyncio.run(run())

    def test_human_check_shell_clears_when_solved_and_location_left(self) -> None:
        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)
            n = {"i": 0}

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                return {}

            async def wait_for_event(method: str, session_id=None, timeout: float = 60.0):
                return {}

            async def evaluate(expression: str, session_id: str):
                n["i"] += 1
                if n["i"] < 3:
                    return _shell_snapshot()
                return _snapshot(
                    href=DEST_HREF,
                    html="<html><body>ok</body></html>",
                    procaptcha={
                        "present": True,
                        "solved": True,
                        "kind": "solved",
                        "token": "nav-token",
                    },
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            browser.wait_for_event = wait_for_event  # type: ignore[method-assign]
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=3.0
            )
            self.assertFalse(timed_out)
            self.assertEqual(snapshot["href"], DEST_HREF)
            self.assertEqual(procaptcha_token(snapshot), "nav-token")
            self.assertFalse(unresolved_procaptcha(snapshot))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
