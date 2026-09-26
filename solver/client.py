"""HTTP client for loading URLs in the remote browser and reading cookies."""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from http import HTTPStatus
from typing import Any, Iterator, Mapping, MutableMapping

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
PERMANENT_REDIRECT_STATUSES = frozenset({301, 308})


class HTTPError(Exception):
    """Raised by :meth:`BrowserResponse.raise_for_status` on HTTP error responses."""

    def __init__(self, message: str, *, response: BrowserResponse) -> None:
        self.response = response
        super().__init__(message)


class CaseInsensitiveDict(MutableMapping[str, str]):
    """Minimal case-insensitive header mapping like requests uses."""

    def __init__(self, data: Mapping[str, str] | None = None) -> None:
        self._store: dict[str, str] = {}
        if data:
            self.update(data)

    def __getitem__(self, key: str) -> str:
        return self._store[key.lower()]

    def __setitem__(self, key: str, value: str) -> None:
        self._store[key.lower()] = str(value)

    def __delitem__(self, key: str) -> None:
        del self._store[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._store)

    def __len__(self) -> int:
        return len(self._store)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self.items())!r})"

    def items(self) -> Iterator[tuple[str, str]]:
        for key, value in self._store.items():
            yield key, value

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._store.get(key.lower(), default)


def _parse_charset(content_type: str | None) -> str | None:
    if not content_type:
        return None
    match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    if not match:
        return None
    return match.group(1).strip("\"'")


class BrowserResponse:
    """Response object with a requests/curl_cffi-like interface."""

    def __init__(
        self,
        *,
        url: str,
        status_code: int,
        headers: Mapping[str, str] | None = None,
        content: bytes = b"",
        final_url: str | None = None,
        cookies: list[dict[str, Any]] | None = None,
        request_headers: Mapping[str, str] | None = None,
        history: list[BrowserResponse] | None = None,
    ) -> None:
        self.url = url
        self.status_code = int(status_code)
        self.headers = CaseInsensitiveDict(headers or {})
        self._content = bytes(content)
        self._final_url = final_url or url
        self._cookies = list(cookies or [])
        self.request_headers = CaseInsensitiveDict(request_headers or {})
        self.history = list(history or [])
        self.encoding: str | None = _parse_charset(self.headers.get("content-type"))

    @property
    def final_url(self) -> str:
        return self._final_url

    @property
    def ok(self) -> bool:
        return 400 > self.status_code >= 200

    @property
    def reason(self) -> str:
        try:
            return HTTPStatus(self.status_code).phrase
        except ValueError:
            return ""

    @property
    def is_redirect(self) -> bool:
        return self.status_code in REDIRECT_STATUSES

    @property
    def is_permanent_redirect(self) -> bool:
        return self.status_code in PERMANENT_REDIRECT_STATUSES

    @property
    def charset(self) -> str | None:
        return self.encoding

    @property
    def charset_encoding(self) -> str | None:
        return self.encoding

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        encoding = self.encoding or "utf-8"
        return self._content.decode(encoding, errors="replace")

    def json(self, **kwargs: Any) -> Any:
        return json.loads(self.text, **kwargs)

    def raise_for_status(self) -> None:
        if self.ok:
            return
        if 400 <= self.status_code < 500:
            message = f"{self.status_code} Client Error: {self.reason} for url: {self.url}"
        elif 500 <= self.status_code < 600:
            message = f"{self.status_code} Server Error: {self.reason} for url: {self.url}"
        else:
            message = f"{self.status_code} Unexpected status for url: {self.url}"
        raise HTTPError(message, response=self)

    @property
    def cookies_dict(self) -> dict[str, str]:
        return {
            str(item["name"]): str(item["value"])
            for item in self._cookies
            if "name" in item
        }


class Browser:
    def __init__(self, host: str, port: int, default_timeout: float = 60.0) -> None:
        self.host = host
        self.port = port
        self.default_timeout = default_timeout
        self._base = f"http://{host}:{port}"

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
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

    def _fetch_cookies_payload(
        self,
        url: str,
        timeout: float | None = None,
        min_wait: float = 0.0,
    ) -> dict[str, Any]:
        wait = timeout if timeout is not None else self.default_timeout
        return self._post_json(
            "/v1/cookies",
            {"url": url, "timeout": wait, "min_wait": min_wait},
            wait,
        )

    def fetch_cookies(
        self,
        url: str,
        timeout: float | None = None,
        min_wait: float = 0.0,
    ) -> dict[str, Any]:
        """Load ``url`` and return the full API response (cookies, headers, user_agent, url)."""
        return self._fetch_cookies_payload(url, timeout=timeout, min_wait=min_wait)

    def get(
        self,
        url: str,
        timeout: float | None = None,
        min_wait: float = 0.0,
    ) -> BrowserResponse:
        """Load ``url`` in the remote browser and return a requests-like response."""
        wait = timeout if timeout is not None else self.default_timeout
        payload = self._post_json(
            "/v1/get",
            {"url": url, "timeout": wait, "min_wait": min_wait},
            wait + min_wait,
        )

        content_b64 = payload.get("content_b64") or ""
        content = base64.b64decode(content_b64) if content_b64 else b""

        history: list[BrowserResponse] = []
        for item in payload.get("redirects") or []:
            history.append(
                BrowserResponse(
                    url=str(item.get("url") or url),
                    status_code=int(item.get("status") or 302),
                    headers=item.get("headers") or {},
                    content=b"",
                    final_url=str(item.get("url") or url),
                )
            )

        return BrowserResponse(
            url=str(payload.get("url") or url),
            status_code=int(payload.get("status_code") or 200),
            headers=payload.get("headers") or {},
            content=content,
            final_url=str(payload.get("final_url") or url),
            cookies=list(payload.get("cookies") or []),
            request_headers=payload.get("request_headers") or {},
            history=history,
        )

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
