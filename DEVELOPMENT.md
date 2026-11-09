# Development guide

Detailed documentation for the Turnstile solver stack: architecture, components, token harvesting, Docker internals, and backend integration.

For a short usage guide, see [README.md](README.md).

---

## Overview

A proof-of-concept Cloudflare Turnstile bypass system built with Rust and JavaScript. It runs real GUI browsers (never `--headless`, which changes the fingerprint) and can either:

1. **Cookie mode** (default, `PAGE_OVERRIDE=0`) — load arbitrary URLs on demand and return cookies.
2. **Token harvesting** (`PAGE_OVERRIDE=1`) — replace pages with a harvester that solves Turnstile widgets and streams tokens over a WebSocket.

---

## Pros and caveats

### Pros

| Major |
| :--- |
| No third-party solver API required. |
| Fast token generation when harvesting. |
| Harder to patch than many bypasses — uses real browsers and page overrides. |
| High success rate; Cloudflare sees a legitimate browser session. |
| After a token is solved, downstream work can be fully headless. |

| Minor |
| :--- |
| Token server centralizes solver management. |
| Effective when you know the target site in advance. |
| Modular pipeline — components can be changed independently. |

### Caveats

| Major |
| :--- |
| **Not headless** — a real GUI browser is required. Docker uses Xvfb. |
| Token harvesting is site-specific; cookie mode loads any URL but still runs a full browser. |
| No TLS/JA4/canvas fingerprint spoofing out of the box. |
| Browsers must be started by the stack (Docker entrypoint handles this). |

| Minor |
| :--- |
| Harvesting mode relies on a browser extension with page overrides. |
| Designed for smaller-scale harvesting; architecture supports scaling. |
| Per-iframe proxy tunneling is not supported; per-window proxying is. |
| Extension must be loaded once per profile (persisted in Docker volume). |

---

## Why this method?

A free alternative to commercial solver APIs and WebDriver-based approaches. Uses a real browser instance instead of stealth-patched automation, which reduces maintenance when browser fingerprints change.

Best suited for **repeated Turnstile solves on a known site**. Cookie mode extends the stack for **session bootstrap** — load a site in Chrome, export cookies, continue with `curl_cffi`.

---

## Supported browsers

Any Chromium-based browser with CDP support: Chrome, Edge, Brave, Opera, etc. The Docker image ships Google Chrome stable.

---

## Latency and throughput

### Latency

Single-site harvesting avoids full page reloads per solve. The override harvester strips the page down to the widget iframe. Real fingerprints yield short challenges (often under five seconds). Solver ↔ token-server round-trips are sub-second; the bottleneck is Cloudflare itself.

### Throughput

`BROWSER_COUNT` is the cookie API's parallelism knob: that many headed Chrome processes run at once, each with its own profile and `--remote-debugging-port` (`CDP_PORT`, `CDP_PORT+1`, …). The cookie server checks a CDP connection out of that pool for one `/v1/get` or `/v1/cookies` call at a time. There is no global navigation lock. When every live slot is busy, the extra call waits up to `QUEUE_TIMEOUT` seconds (default 10) and then returns HTTP 503 with a JSON `error` and a `Retry-After` header. The page `timeout` clock starts after a slot is acquired, so queue wait stays inside the caller's 20 second slack.

Each Chrome needs on the order of 1–2 GB of `/dev/shm`. Compose defaults `shm_size` to `2gb` for `BROWSER_COUNT=1`. Raise `shm_size` with the count (`BROWSER_COUNT=4` → about `8gb`). Leave the default count at 1 so a small box does not OOM. The existing `--disable-dev-shm-usage` fallback still applies when `/dev/shm` is under ~500 MB.

Token harvesting can still use multiple windows as independent solvers. Parallel cookie-API challenges are clicked with CDP `Input.dispatchMouseEvent` on the tab that owns the request so two windows do not steal OS focus from each other.

---

## Proxy formats

```
protocol://host:port
protocol://user:pass@host:port
```

HTTP proxies are recommended. SOCKS support varies by browser.

---

## Components

1. **Token Harvester** — iframe-based Turnstile widget loader (`cf-turnstile-bypass/token-harvester/`)
2. **Turnstile Clicker** — OS-level checkbox clicker (`cf-turnstile-bypass/turnstile-clicker/`)
3. **Token Server** — WebSocket router (`cf-turnstile-bypass/token-server/`)
4. **Proxy Extensions** — per-tab proxy, spoofing, page override (`cf-turnstile-bypass/proxy-extensions/`)
5. **Cookie Server** — HTTP API + CDP tab automation (`cookie_server/`) — *added in this fork*

---

### 1. Token Harvester

Spawns iframe solvers that connect to the token server. On a solve request, loads the Turnstile widget and returns the token. Config is injected via the extension into `localStorage` (see `config/inject_config.txt`).

**Why overrides?** Sidesteps CORS and keeps a minimal page focused on the widget.

---

### 2. Turnstile Clicker

Rust binary that finds Turnstile checkboxes via pixel analysis (grey ring DFS) and clicks at the OS level. **Disabled by default** on bare metal — press **F8** to toggle. In Docker, `CLICKER_ENABLED=1` starts it automatically.

---

### 3. Token Server

WebSocket server on port **8080**. Binary little-endian protocol.

#### Serverbound (client → server)

| Header | From | Description |
|--------|------|-------------|
| `0` | Solver | Token result → routed to requester |
| `1` | Receiver | On-demand solve request |
| `2` | Solver | Register as solver (with user-agent) |
| `3` | Receiver | Count available solvers |

#### Clientbound (server → client)

| Length | Meaning |
|--------|---------|
| `1` | No solvers available |
| `4` | Solve failed (solver idx only) |
| `5` | Available solver count |
| `>4` | Token (solver idx + token bytes) |

See `examples/request_token.py` for a Python client. JS packet builders are in the [Backend helpers](#backend-helpers) section below.

**Field spoofing in solve requests:**

- `navigator.language` / `window.innerWidth` — JS API spoofs (`API.key` format)
- `action` / `cData` — Turnstile `render()` fields (`key` format, no prefix)

---

### 4. Proxy Extensions

Bridge for proxy routing and fingerprint spoofing via `window.postMessage`.

**Setup (manual / non-Docker):**

1. Set `PROXIES_LIST_PATH`, `OVERRIDE_FILE_PATH`, `INJECT_CONFIG_FILE_PATH` in `background.js`.
2. Set `SITEKEY`, `PROXY_CONNECT_TIMEOUT`, `USE_PROXY_SOLVING`, `TOKEN_SERVER_HOST` in inject config.

In Docker, the entrypoint rewrites extension paths to a loopback file server and packs the CRX at build time.

**Flow:** `SET_TAB_PROXY` → proxy connect → JS spoof → `PROXY_READY`. Also handles WebRTC leak blocking and `matchMedia` spoofing when window dimensions are provided.

---

### 5. Cookie Server (fork)

Python service started by the Docker entrypoint.

- Listens on `0.0.0.0:8081` (configurable via `COOKIE_SERVER_PORT`)
- Connects to a pool of Chrome processes over CDP. `--cdp` / `CDP_URL` is the endpoint for index 0 (`http://127.0.0.1:9222`); later indexes use `CDP_PORT + i`
- Pool size is the number of Chromes that actually came up, logged at startup. A dead Chrome fails only the request that held it; the entrypoint relaunches that process and later requests reconnect
- Per request: check out one slot → navigate that Chrome's tab → wait for load / challenge → optional `min_wait` → `Network.getCookies` → release the slot
- Cloudflare Turnstile interstitials are clicked with CDP `Input.dispatchMouseEvent`. Visible Prosopo Procaptcha widgets (frictionless PoW and dedicated PoW checkbox) are clicked the same way and the tab is held until a `procaptcha-response` token appears. Puzzle dialogs are matched in image space (overlay onto the darkened cut, including inner overlap lines) and dragged with a trusted CDP mouse path so the widget records `puzzleEvents` itself. Image dialogs are detected and the wait ends unsolved.
- Parallel slots never share a cookie jar (separate `--user-data-dir`). A fifth caller waits up to `QUEUE_TIMEOUT` then gets HTTP 503; it does not block on another caller's page timeout

Callers use this by POSTing the usual `{"url", "timeout", "min_wait"}` JSON from several workers at once to the same `http://turnstile:8081/v1/get` (or `/v1/cookies`). The server assigns a Chrome per in-flight POST. Set `BROWSER_COUNT` to that worker count and raise compose `shm_size` (~2 GB × count). See README [Concurrent workers](README.md#concurrent-workers) for the `.env` / compose snippet, the client HTTP timeout `(2 * timeout) + min_wait + 20`, and a threaded example.

Source: `cookie_server/server.py`, `cookie_server/cdp_browser.py`, `cookie_server/pool.py`.

Python client: `solver/client.py`.

Cookie API tests (fake browsers, no display, no Cloudflare):

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_unfocused_cdp_click.py` additionally launches headed Chrome on Xvfb when `google-chrome-stable` and `Xvfb` are installed, and checks that `Input.dispatchMouseEvent` on one slot's session clicks that slot's widget while another window has OS focus.

`tests/test_procaptcha.py` covers the Procaptcha wait loop with a fake CDP session. `tests/test_procaptcha_puzzle.py` covers overlay matching and the puzzle drag path. `tests/test_procaptcha_cdp_click.py` launches headed Chrome and confirms a trusted CDP click fills `procaptcha-response` on a widget that ignores scripted clicks. Live demo solves (`tests/test_procaptcha_live.py`) run only when `PROSOPO_LIVE=1`.

---

## Docker

### What starts

| Process | Port | Role |
|---------|------|------|
| Xvfb | — | Virtual display `:99` |
| File server | `9377` (loopback) | Extension config + harvester HTML |
| `token_server` | `8080` | Turnstile token WebSocket |
| `turnstile-clicker` | — | OS-level checkbox clicks |
| Chrome × `BROWSER_COUNT` | `CDP_PORT + i` (loopback, default 9222, 9223, …) | Headed browser on `about:blank`; one process per concurrent cookie-API navigation |
| `cdp_proxy` | `CDP_PROXY_PORT` → `CDP_PORT` | Publishes Chrome 0 on host `127.0.0.1:9222`. Default proxy port is `9222 + BROWSER_COUNT` (9223 when the count is 1) |
| `cookie_server` | `8081` | Cookie HTTP API; pools the Chromes above |

### Configure

```bash
cp .env.example .env
```

Edit `config/inject_config.txt` for harvesting mode:

```
SITEKEY: <your turnstile sitekey>
PROXY_CONNECT_TIMEOUT: 5000
USE_PROXY_SOLVING: false
TOKEN_SERVER_HOST: ws://127.0.0.1:8080
```

Optional proxies: `config/proxies.txt` (one per line), set `USE_PROXY_SOLVING: true`.

### Run

```bash
docker compose up --build
```

To serve four scraper workers at once, set `BROWSER_COUNT=4` in `.env` and `shm_size: "8gb"` in `docker-compose.yml`, then compose up. Workers POST concurrently at `http://127.0.0.1:8081` with the same JSON as a single call. Details: README [Concurrent workers](README.md#concurrent-workers).

### Environment variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `COOKIE_SERVER_PORT` | `8081` | Cookie API host port |
| `TOKEN_SERVER_PORT` | `8080` | Token server host port |
| `PAGE_OVERRIDE` | `0` | `1` = harvester replaces navigations |
| `CLICKER_ENABLED` | `1` | Auto-start clicker |
| `BROWSER_COUNT` | `1` | Parallel Chrome processes (cookie API concurrency). Each extra browser needs 1–2 GB of `/dev/shm` |
| `QUEUE_TIMEOUT` | `10` | Seconds to wait for a free Chrome slot before HTTP 503 |
| `SCREEN_WIDTH` / `SCREEN_HEIGHT` | `1920` / `1080` | Xvfb size |
| `ENABLE_VNC` | `0` | VNC on port `5900` |
| `CHROME_NO_SANDBOX` | `1` | `--no-sandbox` for Docker |
| `CHROME_REMOTE_DEBUGGING` | `1` | Enable CDP |

Chrome profiles persist in the `chrome-profile` volume.

### Chrome `--no-sandbox`

Default `CHROME_NO_SANDBOX=1` because Chrome's sandbox often cannot start in Docker.

Trade-offs:

- A compromised renderer runs with browser-level privileges inside the container.
- CDP on port `9222` equals full profile control — compose binds it to `127.0.0.1` only.
- Do not point the cookie API at untrusted pages in production.

To try Chrome's real sandbox:

```yaml
environment:
  CHROME_NO_SANDBOX: "0"
cap_add:
  - SYS_ADMIN
security_opt:
  - seccomp=unconfined
```

### VNC debugging

```bash
ENABLE_VNC=1 docker compose up
# connect VNC client to localhost:5900
```

---

## Token harvesting

Requires `PAGE_OVERRIDE=1` in `.env` / compose. The extension replaces the main document with `token-harvester/index.html`.

**1. Start with override enabled**

```bash
PAGE_OVERRIDE=1 docker compose up --build
```

Wait for a solver registration line:

```
[+] Solver 0 added to queue. Total available for UA '...': 1.
```

**2. Request a token**

```bash
python3 examples/request_token.py --count
python3 examples/request_token.py
python3 examples/request_token.py \
  --field action=login \
  --field cData=abc123 \
  --field window.innerWidth=1920 \
  --field window.innerHeight=1080
```

`examples/request_token.py` is stdlib-only.

---

## Manual setup (desktop)

1. Start the **token server** (`cf-turnstile-bypass/token-server/`).
2. Start the **turnstile-clicker**.
3. Load the **proxy extension** in your browser.
4. Open target pages (or use cookie API / CDP in Docker).
5. Press **F8** to enable the clicker (if not auto-started).
6. Connect your backend to the token server WebSocket.

---

## Backend helpers

JavaScript snippets for building token-server packets from a backend.

**Construct solve request:**

```javascript
function construct_solver_request_packet(proxy_idx, user_agent = "", fields = {}) {
   let encoder = new TextEncoder();
   let packet = Array(5);
   packet[0] = 1;
   packet[1] = proxy_idx & 255;
   packet[2] = (proxy_idx >> 8) & 255;
   packet[3] = (proxy_idx >> 16) & 255;
   packet[4] = (proxy_idx >> 24) & 255;
   let user_agent_bytes = encoder.encode(user_agent);
   packet[5] = user_agent_bytes.length;
   packet.push(...user_agent_bytes);
   for (let field_name in fields) {
         let field_value = fields[field_name];
         let field_name_bytes = encoder.encode(field_name);
         let field_value_bytes = encoder.encode(field_value);
         packet.push(field_name_bytes.length);
         packet.push(...field_name_bytes);
         packet.push(field_value_bytes.length);
         packet.push(...field_value_bytes);
   }
   return new Uint8Array(packet);
}
```

**Parse token response:**

```javascript
function parse_token_response_packet(packet) {
    let view = new DataView(packet);
    let solver_idx = view.getUint32(0, true);
    let token = undefined;
    if (packet.length > 4) {
      token = new TextDecoder().decode(new Uint8Array(packet).subarray(4));
    }
    return [solver_idx, token];
}
```

**Parse available solvers count:**

```javascript
function parse_available_solvers_count_packet(packet) {
    return [new DataView(packet).getUint32(0, true)];
}
```

**Discriminate by length:**

```javascript
if (packet.byteLength > 5) {
   // Token packet
} else if (packet.byteLength == 5) {
   // Available solvers count
} else if (packet.byteLength == 4) {
   // Failed solve
} else {
   // No solvers available
}
```

---

## Project layout

```
cookie_server/          # HTTP cookie API + CDP automation + browser pool
solver/                 # Python client package (pip install -e solver)
tests/                  # Cookie API pool tests (fake browsers) + unfocused CDP click
cf-turnstile-bypass/
  token-server/         # Rust WebSocket router
  turnstile-clicker/    # Rust OS clicker
  token-harvester/      # Harvester HTML
  proxy-extensions/cdp/ # Chrome MV3 extension
docker/                 # entrypoint, extension pack script
examples/               # CLI clients and tests
config/                 # inject_config.txt, proxies.txt
```

---

## Future plans

- Fully automated browser solver startup across all binaries.
- Headless/CDP-level overriding without extension page replacement.
- WebGL vendor spoofing, canvas fingerprint spoofing.
- Per-iframe proxy tunneling if a feasible approach is found.

---

## Contributing

Issues and pull requests are welcome.
