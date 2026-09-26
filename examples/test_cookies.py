#!/usr/bin/env python3
"""Smoke test for the cookie API.

    docker compose up -d
    python3 examples/test_cookies.py https://example.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from solver import setup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--host", default=os.environ.get("SOLVER_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("COOKIE_SERVER_PORT", "8081")),
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    browser = setup(args.host, args.port, timeout=args.timeout)
    payload = browser.fetch_cookies(args.url, timeout=args.timeout)
    cookies = {
        str(item["name"]): str(item["value"])
        for item in (payload.get("cookies") or [])
        if "name" in item
    }
    print(
        json.dumps(
            {
                "url": payload.get("url", args.url),
                "user_agent": payload.get("user_agent", ""),
                "cookies": cookies,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
