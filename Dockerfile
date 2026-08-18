# syntax=docker/dockerfile:1.7
#
# Multi-stage image for the Turnstile solver.
# Chrome runs headed against Xvfb — never --headless — so the browser
# fingerprint stays that of a real desktop session.

# ---------------------------------------------------------------------------
# Build the two Linux-capable Rust binaries.
# z-index-orderer is Windows-only and is not built here.
# ---------------------------------------------------------------------------
FROM rust:bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config \
        libx11-dev \
        libxtst-dev \
        libxi-dev \
        libxcb1-dev \
        libxrandr-dev \
        libdbus-1-dev \
        libxdo-dev \
        libxkbcommon-dev \
        libwayland-dev \
        libudev-dev \
        libinput-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY cf-turnstile-bypass/token-server /src/token-server
COPY cf-turnstile-bypass/turnstile-clicker /src/turnstile-clicker

WORKDIR /src/token-server
RUN cargo build --release

WORKDIR /src/turnstile-clicker
RUN cargo build --release


# ---------------------------------------------------------------------------
# Runtime: official Chrome + Xvfb + the compiled binaries.
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    DISPLAY=:99 \
    LIBGL_ALWAYS_SOFTWARE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
        gnupg \
        locales \
        python3 \
        openssl \
        xvfb \
        xauth \
        x11-utils \
        x11-xserver-utils \
        xdotool \
        openbox \
        dbus-x11 \
        x11vnc \
        fonts-liberation \
        fonts-dejavu-core \
        fonts-noto-core \
        fonts-freefont-ttf \
        fonts-unifont \
        libgl1 \
        libgl1-mesa-dri \
        mesa-utils \
        libx11-6 \
        libxtst6 \
        libxi6 \
        libxcb1 \
        libxrandr2 \
        libdbus-1-3 \
        libxdo3 \
        libxkbcommon0 \
        libwayland-client0 \
        libudev1 \
        libinput10 \
        procps \
        iproute2 \
        gosu \
    && sed -i 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-linux-signing-key.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux-signing-key.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && command -v google-chrome-stable \
    && (dbus-uuidgen --ensure=/etc/machine-id || openssl rand -hex 16 > /etc/machine-id) \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Chrome still needs --no-sandbox in typical Docker setups;
# set CHROME_NO_SANDBOX=0 and add SYS_ADMIN if you want the real sandbox.
RUN useradd --create-home --shell /bin/bash --uid 1000 solver \
    && mkdir -p /app /app/bin /app/config /data /opt/solver \
    && chown -R solver:solver /app /data /opt/solver

COPY --from=builder /src/token-server/target/release/token_server /app/bin/token_server
COPY --from=builder /src/turnstile-clicker/target/release/turnstile-clicker /app/bin/turnstile-clicker

COPY cf-turnstile-bypass /app/cf-turnstile-bypass
COPY docker /opt/solver/docker
COPY docker /app/docker
COPY config /app/config
COPY examples /app/examples

RUN chmod +x /opt/solver/docker/entrypoint.sh \
              /opt/solver/docker/pack_extension.sh \
              /opt/solver/docker/extension_id.py \
    && chmod +x /app/bin/token_server /app/bin/turnstile-clicker \
    && /opt/solver/docker/pack_extension.sh \
        /app/cf-turnstile-bypass/proxy-extensions/cdp \
        /opt/solver/extension.pem \
        /opt/solver/extension.crx \
        /opt/solver/docker/extension_id.py \
        || echo "[build] CRX pack failed; runtime will fall back to --load-extension" \
    && chown -R solver:solver /app /data /opt/solver

USER solver
WORKDIR /app

EXPOSE 8080 5900 9222

VOLUME ["/data", "/app/config"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD pgrep -x token_server >/dev/null

ENTRYPOINT ["/opt/solver/docker/entrypoint.sh"]
