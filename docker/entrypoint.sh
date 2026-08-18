#!/usr/bin/env bash
# Start the solver stack on a virtual X display.
# Chrome is launched as a normal GUI browser (not --headless) so its
# fingerprint stays that of a real desktop session.
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export SCREEN_WIDTH="${SCREEN_WIDTH:-1920}"
export SCREEN_HEIGHT="${SCREEN_HEIGHT:-1080}"
export SCREEN_DEPTH="${SCREEN_DEPTH:-24}"
export CLICKER_ENABLED="${CLICKER_ENABLED:-1}"
export ENABLE_VNC="${ENABLE_VNC:-0}"
export VNC_PORT="${VNC_PORT:-5900}"
export BROWSER_COUNT="${BROWSER_COUNT:-1}"
export CHROME_NO_SANDBOX="${CHROME_NO_SANDBOX:-1}"
export TARGET_URL="${TARGET_URL:-}"
export PAGE_OVERRIDE="${PAGE_OVERRIDE:-0}"
export CHROME_REMOTE_DEBUGGING="${CHROME_REMOTE_DEBUGGING:-1}"
export CDP_PORT="${CDP_PORT:-9222}"
export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

APP_DIR="${APP_DIR:-/app}"
CONFIG_DIR="${CONFIG_DIR:-/app/config}"
DATA_DIR="${DATA_DIR:-/data}"
EXT_DIR="${APP_DIR}/cf-turnstile-bypass/proxy-extensions/cdp"
STATIC_DIR="${STATIC_DIR:-/tmp/solver-static}"
FILE_SERVER_PORT="${FILE_SERVER_PORT:-9377}"
CHROME_BIN="${CHROME_BIN:-google-chrome-stable}"

mkdir -p "${DATA_DIR}" "${STATIC_DIR}"
mkdir -p /tmp/.X11-unix 2>/dev/null || true

# Config may be a read-only host mount. Prefer it when present, otherwise
# materialize defaults in the writable static dir.
ensure_config_file() {
    local name="$1"
    local dest="${CONFIG_DIR}/${name}"
    if [[ -f "${dest}" ]]; then
        return 0
    fi
    if [[ -f "${CONFIG_DIR}/${name}.example" ]] && cp "${CONFIG_DIR}/${name}.example" "${dest}" 2>/dev/null; then
        echo "[entrypoint] Created ${dest} from the example file."
        return 0
    fi
    if [[ -n "${2:-}" && -f "$2" ]] && cp "$2" "${dest}" 2>/dev/null; then
        echo "[entrypoint] Copied $2 -> ${dest}"
        return 0
    fi
    dest="${STATIC_DIR}/${name}"
    if [[ -n "${2:-}" && -f "$2" ]]; then
        cp "$2" "${dest}"
    elif [[ -n "${3:-}" ]]; then
        printf '%s\n' "$3" > "${dest}"
    else
        : > "${dest}"
    fi
    echo "[entrypoint] Using ${dest} (config mount is not writable)."
}

ensure_config_file "proxies.txt" "${CONFIG_DIR}/proxies.txt.example" ""
ensure_config_file "inject_config.txt" \
    "${APP_DIR}/cf-turnstile-bypass/proxy-extensions/inject_config.txt" \
    "SITEKEY: sitekey
PROXY_CONNECT_TIMEOUT: 5000
USE_PROXY_SOLVING: false
TOKEN_SERVER_HOST: ws://127.0.0.1:8080"

# Point the extension at the in-container file server. Chrome MV3 service
# workers cannot read host files, and branded Chrome no longer accepts
# --load-extension, so config is served over loopback HTTP instead.
export _EXT_DIR="${EXT_DIR}"
export _FILE_SERVER_PORT="${FILE_SERVER_PORT}"
python3 - <<'PY'
from pathlib import Path
import os
import re

path = Path(os.environ["_EXT_DIR"]) / "background.js"
port = os.environ["_FILE_SERVER_PORT"]
text = path.read_text()
replacements = {
    "PROXIES_LIST_PATH": f"http://127.0.0.1:{port}/proxies.txt",
    "OVERRIDE_FILE_PATH": f"http://127.0.0.1:{port}/index.html",
    "INJECT_CONFIG_FILE_PATH": f"http://127.0.0.1:{port}/inject_config.txt",
}
for name, url in replacements.items():
    text, n = re.subn(
        r"const %s = String\.raw`[^`]*`;" % name,
        f'const {name} = "{url}";',
        text,
        count=1,
    )
    if n == 0:
        text, n = re.subn(
            r'const %s = ["\'].*["\'];' % name,
            f'const {name} = "{url}";',
            text,
            count=1,
        )
    if n == 0:
        raise SystemExit(f"failed to rewrite {name} in background.js")
path.write_text(text)
print("[entrypoint] Rewrote extension config paths to the loopback file server.")
PY

if [[ -f "${CONFIG_DIR}/proxies.txt" ]]; then
    ln -sfn "${CONFIG_DIR}/proxies.txt" "${STATIC_DIR}/proxies.txt"
fi
if [[ -f "${CONFIG_DIR}/inject_config.txt" ]]; then
    ln -sfn "${CONFIG_DIR}/inject_config.txt" "${STATIC_DIR}/inject_config.txt"
fi
ln -sfn "${APP_DIR}/cf-turnstile-bypass/token-harvester/index.html" "${STATIC_DIR}/index.html"

if [[ -f /opt/solver/extension.crx ]]; then
    ln -sfn /opt/solver/extension.crx "${STATIC_DIR}/extension.crx"
fi
if [[ -f /opt/solver/updates.xml ]]; then
    ln -sfn /opt/solver/updates.xml "${STATIC_DIR}/updates.xml"
fi

cleanup() {
    echo "[entrypoint] Stopping..."
    jobs -pr | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- virtual display -------------------------------------------------------
# Xvfb gives Chrome a real X11 screen. That is intentional: --headless
# changes the fingerprint (WebGL, GPU, window chrome, HeadlessChrome UA).
echo "[entrypoint] Starting Xvfb on ${DISPLAY} (${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH})"
Xvfb "${DISPLAY}" \
    -screen 0 "${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}" \
    -ac \
    +extension RANDR \
    +extension GLX \
    +extension XTEST \
    +extension MIT-SHM \
    -nolisten tcp \
    -dpi 96 \
    &

for _ in $(seq 1 50); do
    if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
if ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    echo "[entrypoint] Xvfb failed to come up on ${DISPLAY}" >&2
    exit 1
fi

if command -v dbus-launch >/dev/null 2>&1; then
    # shellcheck disable=SC2046
    eval "$(dbus-launch --sh-syntax)"
    export DBUS_SESSION_BUS_ADDRESS DBUS_SESSION_BUS_PID
fi

openbox >/tmp/openbox.log 2>&1 &

if [[ "${ENABLE_VNC}" == "1" ]]; then
    echo "[entrypoint] Starting x11vnc on port ${VNC_PORT} (debug only)"
    x11vnc \
        -display "${DISPLAY}" \
        -rfbport "${VNC_PORT}" \
        -shared \
        -forever \
        -nopw \
        -xkb \
        -quiet \
        >/tmp/x11vnc.log 2>&1 &
fi

# --- file server + rust services ------------------------------------------
echo "[entrypoint] Serving extension files on 127.0.0.1:${FILE_SERVER_PORT}"
python3 -m http.server "${FILE_SERVER_PORT}" \
    --bind 127.0.0.1 \
    --directory "${STATIC_DIR}" \
    >/tmp/file-server.log 2>&1 &

echo "[entrypoint] Starting token server"
"${APP_DIR}/bin/token_server" >/proc/1/fd/1 2>/proc/1/fd/2 &
TOKEN_SERVER_PID=$!

echo "[entrypoint] Starting turnstile clicker (CLICKER_ENABLED=${CLICKER_ENABLED})"
"${APP_DIR}/bin/turnstile-clicker" >/proc/1/fd/1 2>/proc/1/fd/2 &
CLICKER_PID=$!

sleep 1
if ! kill -0 "${TOKEN_SERVER_PID}" 2>/dev/null; then
    echo "[entrypoint] token_server exited immediately" >&2
    exit 1
fi
if ! kill -0 "${CLICKER_PID}" 2>/dev/null; then
    echo "[entrypoint] turnstile-clicker exited immediately" >&2
    exit 1
fi

# --- chrome ----------------------------------------------------------------
chrome_base_flags=(
    --no-first-run
    --no-default-browser-check
    --disable-session-crashed-bubble
    --hide-crash-restore-bubble
    --disable-search-engine-choice-screen
    --password-store=basic
    --use-mock-keychain
    --disable-sync
    --noerrdialogs
    --disable-popup-blocking
    --disable-component-update
    --disable-backgrounding-occluded-windows
    --disable-renderer-backgrounding
    --disable-ipc-flooding-protection
    --autoplay-policy=no-user-gesture-required
)

if [[ "${CHROME_NO_SANDBOX}" == "1" ]]; then
    chrome_base_flags+=(--no-sandbox --disable-setuid-sandbox)
fi

# /dev/shm is 64MB in default Docker; Chrome tabs crash without a larger
# shm or this flag. docker-compose sets shm_size, in which case we skip it.
shm_kb="$(df -k /dev/shm 2>/dev/null | awk 'NR==2 { print $2 }' || echo 0)"
if [[ "${shm_kb:-0}" -lt 500000 ]]; then
    echo "[entrypoint] /dev/shm is ${shm_kb}k — adding --disable-dev-shm-usage"
    chrome_base_flags+=(--disable-dev-shm-usage)
fi

# Loading the harvester extension replaces the real page with Token Harvester
# and attaches a debugger — both trip Cloudflare. PAGE_OVERRIDE=1 is only for
# token harvesting; the default is a real headed browse of TARGET_URL.
chrome_features="TranslateUI,ChromeWhatsNewUI,PrivacySandboxSettings4"
if [[ "${PAGE_OVERRIDE}" == "1" ]]; then
    chrome_features+=",DisableLoadExtensionCommandLineSwitch"
    chrome_base_flags+=(--load-extension="${EXT_DIR}")
    echo "[entrypoint] PAGE_OVERRIDE=1 — extension will replace TARGET_URL with the harvester"
else
    echo "[entrypoint] PAGE_OVERRIDE=0 — Chrome loads TARGET_URL for real"
fi
chrome_base_flags+=(--disable-features="${chrome_features}")

if [[ "${CHROME_REMOTE_DEBUGGING}" == "1" ]]; then
    # Chrome only accepts DevTools on loopback. A small proxy publishes it
    # on 0.0.0.0 so `python3 examples/test_site.py` works from the host.
    chrome_base_flags+=(
        --remote-debugging-port="${CDP_PORT}"
        --remote-debugging-address=127.0.0.1
        --remote-allow-origins=*
    )
fi

if [[ -z "${TARGET_URL}" ]]; then
    echo "[entrypoint] WARNING: TARGET_URL is not set. Chrome will open about:blank."
    echo "[entrypoint] Set TARGET_URL to the site to open."
fi

# /usr/bin/google-chrome-stable is a wrapper that starts the real binary
# and then exits. Supervising that wrapper PID looks like a crash loop:
# we relaunch, the new wrapper sees the live profile and exits immediately.
chrome_bin="${CHROME_BIN}"
if [[ -x /opt/google/chrome/chrome ]]; then
    chrome_bin=/opt/google/chrome/chrome
fi

chrome_profile() {
    if [[ "${PAGE_OVERRIDE}" == "1" ]]; then
        echo "${DATA_DIR}/chrome-$1"
    else
        # Separate profile so a previous harvest run's installed extension
        # cannot keep overriding the real page.
        echo "${DATA_DIR}/chrome-browse-$1"
    fi
}

chrome_alive() {
    local profile
    profile="$(chrome_profile "$1")"
    # [c]hrome so this pgrep command line cannot match itself.
    pgrep -f -- "[c]hrome.*--user-data-dir=${profile}" >/dev/null 2>&1
}

launch_chrome() {
    local index="$1"
    local profile
    profile="$(chrome_profile "${index}")"
    local pos_x=$((index * 48))
    local pos_y=$((index * 48))
    local url="${TARGET_URL:-about:blank}"
    mkdir -p "${profile}"

    # A previous SIGKILL leaves these behind; Chrome then exits at once.
    rm -f "${profile}/SingletonLock" \
          "${profile}/SingletonSocket" \
          "${profile}/SingletonCookie"

    local flags=("${chrome_base_flags[@]}"
        --user-data-dir="${profile}"
        --window-position="${pos_x},${pos_y}"
        --ozone-platform=x11
    )

    if [[ "${BROWSER_COUNT}" -le 1 ]]; then
        flags+=(--start-maximized)
    else
        flags+=(--window-size="$((SCREEN_WIDTH - pos_x)),$((SCREEN_HEIGHT - pos_y))")
    fi

    echo "[entrypoint] Launching Chrome #${index} -> ${url}"
    "${chrome_bin}" "${flags[@]}" "${url}" >/tmp/chrome-"${index}".log 2>&1 &

    local i
    for i in $(seq 1 25); do
        if chrome_alive "${index}"; then
            return 0
        fi
        sleep 0.2
    done
    echo "[entrypoint] Chrome #${index} did not stay up. Last log:" >&2
    tail -n 40 /tmp/chrome-"${index}".log >&2 || true
    return 1
}

for i in $(seq 0 $((BROWSER_COUNT - 1))); do
    launch_chrome "${i}" || true
    sleep 1
done

# The extension attaches its debugger on navigation, but the first load of a
# target page often races that attach. A reload after the profile is up makes
# the document override fire reliably (see background.js comments).
if [[ "${PAGE_OVERRIDE}" == "1" ]]; then
    echo "[entrypoint] Waiting for Chrome to settle, then reloading for the override..."
    sleep 6
    if command -v xdotool >/dev/null 2>&1; then
        for win in $(xdotool search --onlyvisible --class 'Google-chrome' 2>/dev/null || true); do
            xdotool windowactivate --sync "${win}" key --clearmodifiers F5 || true
            sleep 0.3
        done
    fi
else
    echo "[entrypoint] Waiting for Chrome to settle..."
    sleep 3
fi

if [[ "${CHROME_REMOTE_DEBUGGING}" == "1" ]]; then
    export CDP_PROXY_PORT="${CDP_PROXY_PORT:-9223}"
    python3 /app/examples/cdp_proxy.py >/tmp/cdp-proxy.log 2>&1 &
    echo "[entrypoint] Starting auto-browse helper (title watch)"
    python3 /app/examples/auto_browse.py >/proc/1/fd/1 2>/proc/1/fd/2 &
fi

echo "[entrypoint] Stack is up."
echo "[entrypoint]   display      ${DISPLAY} (${SCREEN_WIDTH}x${SCREEN_HEIGHT})"
echo "[entrypoint]   token server ws://0.0.0.0:8080"
echo "[entrypoint]   browsers     ${BROWSER_COUNT}"
echo "[entrypoint]   target       ${TARGET_URL:-about:blank}"
echo "[entrypoint]   override     ${PAGE_OVERRIDE}"
echo "[entrypoint]   cdp          $([[ "${CHROME_REMOTE_DEBUGGING}" == "1" ]] && echo "0.0.0.0:${CDP_PORT}" || echo "disabled")"
echo "[entrypoint]   vnc          $([[ "${ENABLE_VNC}" == "1" ]] && echo "port ${VNC_PORT}" || echo "disabled")"
echo "[entrypoint] z-index-orderer is Windows-only and is not started in this image."

# Keep the container alive while the core processes run. Restart Chrome if
# a window dies; exit if the token server or clicker dies.
while true; do
    if ! kill -0 "${TOKEN_SERVER_PID}" 2>/dev/null; then
        echo "[entrypoint] token_server exited" >&2
        exit 1
    fi
    if ! kill -0 "${CLICKER_PID}" 2>/dev/null; then
        echo "[entrypoint] turnstile-clicker exited" >&2
        exit 1
    fi
    for i in $(seq 0 $((BROWSER_COUNT - 1))); do
        if chrome_alive "${i}"; then
            continue
        fi
        echo "[entrypoint] Chrome #${i} is not running — restarting"
        tail -n 20 /tmp/chrome-"${i}".log 2>/dev/null || true
        launch_chrome "${i}" || true
    done
    sleep 2
done
