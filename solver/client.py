"""HTTP client for loading URLs in the remote browser and reading cookies."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class Browser:
    def __init__(self, host: str, port: int, default_timeout: float = 60.0) -> None:
        self.host = host
        self.port = port
        self.default_timeout = default_timeout
        self._base = f"http://{host}:{port}"

    def _fetch_cookies_payload(
        self,
        url: str,
        timeout: float | None = None,
        min_wait: float = 0.0,
    ) -> dict[str, Any]:
        wait = timeout if timeout is not None else self.default_timeout
        body = json.dumps({"url": url, "timeout": wait, "min_wait": min_wait}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base}/v1/cookies",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=wait + min_wait + 10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(detail).get("error", detail)
            except json.JSONDecodeError:
                message = detail or exc.reason
            raise RuntimeError(f"cookie server error ({exc.code}): {message}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"could not reach cookie server at {self._base}: {exc}") from exc

    def fetch_cookies(
        self,
        url: str,
        timeout: float | None = None,
        min_wait: float = 0.0,
    ) -> dict[str, Any]:
        """Load ``url`` and return the full API response (cookies, headers, user_agent, url)."""
        return self._fetch_cookies_payload(url, timeout=timeout, min_wait=min_wait)

    def get_all_cookies(
        self,
        url: str,
        timeout: float | None = None,
        min_wait: float = 0.0,
    ) -> dict[str, str]:
        """Load ``url`` in the remote browser and return name -> value cookies.

        ``min_wait`` keeps the tab open for at least that many seconds after the
        page finishes loading, so redirects and late cookie writes are captured.
        """
        payload = self._fetch_cookies_payload(url, timeout=timeout, min_wait=min_wait)
        cookies = payload.get("cookies") or []
        return {str(item["name"]): str(item["value"]) for item in cookies if "name" in item}

    def get_cookie_details(
        self,
        url: str,
        timeout: float | None = None,
        min_wait: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Return the full CDP cookie objects (domain, path, secure, etc.)."""
        payload = self._fetch_cookies_payload(url, timeout=timeout, min_wait=min_wait)
        return list(payload.get("cookies") or [])


def setup(host: str, port: int, timeout: float = 60.0) -> Browser:
    """Connect to a running solver container's cookie server."""
    return Browser(host, port, default_timeout=timeout)
