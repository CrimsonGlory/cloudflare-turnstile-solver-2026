# cloudflare-turnstile-solver-2026

Runs a real headed Chrome instance (on Xvfb in Docker) and exposes it to your scripts. You send a URL, the browser loads it, waits for the page to finish, returns the cookies, and closes the tab. Use those cookies with `curl_cffi` or any HTTP client that needs a browser-grade session.

The upstream project focused on Turnstile token harvesting. This fork adds an on-demand **cookie API** so you can test any site without hardcoding a target URL.

See [DEVELOPMENT.md](DEVELOPMENT.md) for architecture, component details, token-server protocol, and advanced setup.

---

## Fork changes

Changes made in this fork relative to the original project:

| Area | Change |
| :--- | :--- |
| **Cookie API** | New HTTP server on port `8081` (`POST /v1/cookies`). Loads any URL via CDP, collects cookies, closes the tab. |
| **Browser headers** | Captured once at container startup from a real Chrome navigation (CDP) and returned as `headers` in every cookie response (`User-Agent`, `Accept-Language`, `sec-ch-ua*`). |
| **Python client** | New `solver` package with `setup()`, `get_all_cookies()`, and `fetch_cookies()`. Stdlib-only client; no extra deps for the API itself. |
| **On-demand loading** | Removed hardcoded `TARGET_URL`. Chrome starts on `about:blank`; URLs are passed per request. |
| **`min_wait`** | Optional minimum wait after page load so redirect chains and late cookie writes are captured. |
| **Docker** | Cookie server started in `entrypoint.sh`; port `8081` exposed in compose. |
| **Examples** | Added `examples/test_cookies.py` and `examples/fetch_with_cookies.py`. |
| **Removed** | Windows-only `z-index-orderer` (not used in the Linux/Docker path). |
| **Docs split** | README simplified for usage; detailed upstream docs moved to `DEVELOPMENT.md`. |

Token harvesting (`PAGE_OVERRIDE=1`, token server, harvester, clicker) is still available. See [DEVELOPMENT.md](DEVELOPMENT.md#token-harvesting).

---

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

Wait until the logs show the cookie API is listening:

```
[entrypoint]   cookie API   http://0.0.0.0:8081/v1/cookies
```

---

## Use from Python

Install the client package (from the repo root):

```bash
pip install -e solver
pip install curl_cffi   # optional, for the example below
```

```python
import solver
import curl_cffi

browser = solver.setup("127.0.0.1", 8081)

URL = "https://example.com"
result = browser.fetch_cookies(URL)
cookies = {c["name"]: c["value"] for c in result["cookies"]}
headers = result["headers"]

response = curl_cffi.get(
    URL,
    impersonate="chrome",
    cookies=cookies,
    headers=headers,
)
print(response.status_code)
```

### `get_all_cookies(url, timeout=60, min_wait=0)`

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `url` | — | Site to open in the browser. |
| `timeout` | `60` | Max seconds to wait for the page to load. |
| `min_wait` | `0` | Minimum seconds to keep the tab open after load. Useful when the site redirects and cookies are set on a later hop. Resets if the URL changes during the wait. |

Returns a `dict[str, str]` mapping cookie name to value, ready for `curl_cffi` or `requests`.

```python
# Wait up to 60s for load, then stay on the page at least 5s more
cookies = browser.get_all_cookies("https://example.com", timeout=60, min_wait=5.0)
```

Full cookie metadata (domain, path, `secure`, etc.) is available via `browser.get_cookie_details(url, ...)`.

### `fetch_cookies(url, timeout=60, min_wait=0)`

Returns the full API response as a `dict` with `url`, `cookies` (CDP objects), `headers`, and `user_agent` (alias for `headers["User-Agent"]`). Use `headers` when replaying the browser session with `curl_cffi` or `requests`.

Headers are read from `/data/browser_headers.json` inside the container. They are captured once at startup by opening a local page in Chrome and recording the real outbound request headers via CDP, including client hints (`sec-ch-ua*`) and `Accept-Language`.

### Shell smoke test

```bash
python3 examples/test_cookies.py https://example.com
python3 examples/fetch_with_cookies.py https://example.com
```

### HTTP API (any language)

```http
POST http://localhost:8081/v1/cookies
Content-Type: application/json

{"url": "https://example.com", "timeout": 60, "min_wait": 5.0}
```

Response:

```json
{
  "url": "https://example.com",
  "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ...",
  "headers": {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ...",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": "\"Chromium\";v=\"131\", \"Google Chrome\";v=\"131\", \"Not_A Brand\";v=\"24\"",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "\"Linux\"",
    "sec-ch-ua-platform-version": "\"6.12.0\"",
    "sec-ch-ua-full-version-list": "\"Chromium\";v=\"131.0.6778.85\", ..."
  },
  "cookies": [
    {"name": "session", "value": "...", "domain": ".example.com", "path": "/", ...}
  ]
}
```

Pass the returned `headers` dict to your HTTP client so follow-up requests match the browser session that produced the cookies.

---

## Configuration

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `COOKIE_SERVER_PORT` | `8081` | Host port for the cookie API. |
| `BROWSER_HEADERS_FILE` | `/data/browser_headers.json` | Path inside the container where startup-captured browser headers are stored. |
| `TOKEN_SERVER_PORT` | `8080` | Host port for the token WebSocket (harvesting mode). |
| `PAGE_OVERRIDE` | `0` | `1` enables token-harvester page replacement. |
| `BROWSER_COUNT` | `1` | Number of Chrome windows. |
| `ENABLE_VNC` | `0` | Set `1` to watch the virtual display on port `5900`. |

See `.env.example` and [DEVELOPMENT.md](DEVELOPMENT.md#docker) for the full list.

---

## License

See [LICENSE](LICENSE).
