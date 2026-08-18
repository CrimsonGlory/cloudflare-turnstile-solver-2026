#!/usr/bin/env python3
"""Background helper: watch Chrome's title while Cloudflare (if any) settles.

Does not click anything. Logs every title change so `docker compose logs`
shows whether the real site loaded.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cdp_client import default_cdp_http, pick_page  # noqa: E402
from test_site import snapshot  # noqa: E402


async def loop() -> None:
    cdp = default_cdp_http()
    expect = os.environ.get("EXPECT_TITLE", "Upwork")
    last_title = None
    while True:
        try:
            if not pick_page(cdp):
                await asyncio.sleep(1)
                continue
            snap = await snapshot(cdp, expect)
        except Exception as exc:
            print(f"[auto-browse] {exc}", flush=True)
            await asyncio.sleep(2)
            continue

        if snap["title"] != last_title:
            print(
                f"[auto-browse] kind={snap['kind']} title={snap['title']!r} url={snap['url']}",
                flush=True,
            )
            last_title = snap["title"]

        await asyncio.sleep(2)


def main() -> int:
    print(f"[auto-browse] watching {default_cdp_http()}", flush=True)
    try:
        asyncio.run(loop())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
