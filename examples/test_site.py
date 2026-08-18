#!/usr/bin/env python3
"""Acceptance test: Chrome must reach the real site, not Cloudflare.

Success: <title> contains EXPECT (default: Upwork)
Failure: still stuck on "Just a moment..."

No GUI. Talks to headed Chrome over CDP (Xvfb inside the container).

    python3 examples/test_site.py
    python3 examples/test_site.py --expect Upwork --timeout 120
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cdp_client import CdpSession, default_cdp_http, pick_page  # noqa: E402


CF_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "enable javascript and cookies",
)


def classify(title: str, href: str, body: str, expect: str) -> str:
    blob = f"{title}\n{href}\n{body}".lower()
    if any(m in blob for m in CF_MARKERS):
        return "cloudflare"
    if expect and expect.lower() in blob:
        return "real"
    if "token harvester" in blob:
        return "harvester_override"
    return "unknown"


async def snapshot(cdp_http: str, expect: str) -> dict:
    session = await CdpSession.connect(cdp_http)
    try:
        await session.call("Runtime.enable")
        title = await session.evaluate("document.title") or ""
        href = await session.evaluate("location.href") or ""
        body = await session.evaluate(
            "document.documentElement ? document.documentElement.outerHTML.slice(0, 8000) : ''"
        ) or ""
        return {
            "title": title,
            "url": href,
            "kind": classify(title, href, body, expect),
            "body_preview": body[:500],
        }
    finally:
        await session.close()


async def wait_for_cdp(cdp_http: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            if pick_page(cdp_http):
                return
        except Exception as exc:
            last = exc
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Chrome CDP not reachable at {cdp_http}: {last}")


async def main_async(args: argparse.Namespace) -> int:
    cdp = args.cdp
    await wait_for_cdp(cdp, min(args.timeout, 30))

    deadline = time.time() + args.timeout
    last = None
    stuck_cf_since = None

    while time.time() < deadline:
        try:
            last = await snapshot(cdp, args.expect)
        except Exception as exc:
            print(f"[test] snapshot failed: {exc}", file=sys.stderr)
            await asyncio.sleep(1)
            continue

        print(
            json.dumps(
                {
                    "kind": last["kind"],
                    "title": last["title"],
                    "url": last["url"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        if last["kind"] == "real" or args.expect.lower() in (last["title"] or "").lower():
            print(f"PASS title={last['title']!r}", flush=True)
            return 0

        if last["kind"] == "cloudflare":
            if stuck_cf_since is None:
                stuck_cf_since = time.time()
            elif time.time() - stuck_cf_since > args.cf_fail_after:
                print(
                    f"FAIL stuck on Cloudflare challenge: title={last['title']!r}",
                    file=sys.stderr,
                )
                return 2
        else:
            stuck_cf_since = None

        if last["kind"] == "harvester_override":
            print(
                "FAIL page was replaced by the token-harvester override; "
                "set PAGE_OVERRIDE=0 to load the real site",
                file=sys.stderr,
            )
            return 3

        await asyncio.sleep(args.poll)

    print(
        f"FAIL timeout after {args.timeout}s last={last}",
        file=sys.stderr,
    )
    if last and last["kind"] == "cloudflare":
        return 2
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cdp", default=default_cdp_http())
    p.add_argument("--expect", default=os.environ.get("EXPECT_TITLE", "Upwork"))
    p.add_argument("--timeout", type=float, default=float(os.environ.get("TEST_TIMEOUT", "120")))
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument(
        "--cf-fail-after",
        type=float,
        default=45.0,
        help="fail if the title stays on Just a moment… this long",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
