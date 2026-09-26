#!/usr/bin/env python3
"""Load a URL in the solver browser, then fetch it with curl_cffi using those cookies.

    docker compose up -d
    pip install curl_cffi
    pip install -e solver
    python3 examples/fetch_with_cookies.py https://example.com
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import curl_cffi
except ImportError:
    print("install curl_cffi: pip install curl_cffi", file=sys.stderr)
    raise SystemExit(1)

from solver import setup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="site to open in the browser")
    parser.add_argument("--host", default=os.environ.get("SOLVER_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("COOKIE_SERVER_PORT", "8081")),
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    browser = setup(args.host, args.port, timeout=args.timeout)
    result = browser.fetch_cookies(args.url, timeout=args.timeout)
    cookies = {
        str(item["name"]): str(item["value"])
        for item in (result.get("cookies") or [])
        if "name" in item
    }
    headers = dict(result.get("headers") or {})
    print(f"got {len(cookies)} cookies from the browser")
    response = curl_cffi.get(
        args.url,
        impersonate="chrome",
        cookies=cookies,
        headers=headers,
    )
    print(f"curl_cffi status={response.status_code}")
    print(response.text[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
