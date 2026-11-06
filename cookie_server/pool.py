"""Pool of headed Chrome processes: one CDP connection per live slot."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse, urlunparse

ConnectFn = Callable[[str], Awaitable[Any]]
WaitCdpFn = Callable[..., Awaitable[None]]


class SlotBusyError(Exception):
    """Raised when every browser slot is busy past ``QUEUE_TIMEOUT``."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def parse_browser_count(value: str | None = None) -> int:
    raw = os.environ.get("BROWSER_COUNT", "1") if value is None else value
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def parse_queue_timeout(value: str | None = None) -> float:
    raw = os.environ.get("QUEUE_TIMEOUT", "10") if value is None else value
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 10.0


def discover_cdp_urls(base_cdp: str, browser_count: int | None = None) -> list[str]:
    """Return ``BROWSER_COUNT`` HTTP CDP endpoints starting at ``base_cdp``.

    Chrome ``i`` listens on ``CDP_PORT + i`` (9222, 9223, …). ``base_cdp`` is
    the URL for index 0; later indexes reuse its host and scheme.
    """
    count = parse_browser_count() if browser_count is None else max(1, int(browser_count))
    parsed = urlparse(base_cdp)
    host = parsed.hostname or "127.0.0.1"
    scheme = parsed.scheme or "http"
    if parsed.port is not None:
        base_port = parsed.port
    else:
        try:
            base_port = int(os.environ.get("CDP_PORT", "9222"))
        except (TypeError, ValueError):
            base_port = 9222
    path = parsed.path or ""
    urls: list[str] = []
    for index in range(count):
        netloc = f"{host}:{base_port + index}"
        urls.append(urlunparse((scheme, netloc, path, "", "", "")))
    return urls


class BrowserSlot:
    def __init__(self, index: int, cdp_http: str) -> None:
        self.index = index
        self.cdp_http = cdp_http
        self.browser: Any = None
        self.alive = False
        self.busy = False
        self._reconnect_task: asyncio.Task | None = None

    async def close_browser(self) -> None:
        browser = self.browser
        self.browser = None
        self.alive = False
        if browser is None:
            return
        close = getattr(browser, "close", None)
        if close is None:
            return
        try:
            await close()
        except Exception:
            pass

    async def connect(
        self,
        factory: ConnectFn,
        wait_cdp: WaitCdpFn | None,
        timeout: float,
    ) -> None:
        if wait_cdp is not None:
            await wait_cdp(self.cdp_http, timeout=timeout)
        self.browser = await factory(self.cdp_http)
        self.alive = True


class BrowserPool:
    """Checkout one Chrome per in-flight cookie-API request."""

    def __init__(
        self,
        cdp_urls: list[str],
        *,
        queue_timeout: float = 10.0,
        connect: ConnectFn | None = None,
        wait_cdp: WaitCdpFn | None = None,
        connect_timeout: float = 10.0,
        reconnect_wait: float = 2.0,
    ) -> None:
        if not cdp_urls:
            raise ValueError("cdp_urls must not be empty")
        self.cdp_urls = list(cdp_urls)
        self.queue_timeout = float(queue_timeout)
        self._factory = connect
        self._wait_cdp = wait_cdp
        self._connect_timeout = float(connect_timeout)
        self._reconnect_wait = float(reconnect_wait)
        self._slots = [BrowserSlot(index, url) for index, url in enumerate(self.cdp_urls)]
        self._idle: asyncio.Queue[BrowserSlot] | None = None
        self._started = False
        self._start_gate: asyncio.Future | None = None
        self._stopped = False
        self._focus_windows = len(self._slots) <= 1

    @property
    def started(self) -> bool:
        return self._started

    @property
    def focus_windows(self) -> bool:
        return self._focus_windows

    @property
    def slot_count(self) -> int:
        return len(self._slots)

    def stats(self) -> dict[str, Any]:
        return {
            "browsers": len(self._slots),
            "ready": sum(1 for slot in self._slots if slot.alive),
            "busy": sum(1 for slot in self._slots if slot.busy),
            "queue_timeout": self.queue_timeout,
        }

    def _require_factory(self) -> ConnectFn:
        if self._factory is None:
            raise RuntimeError("BrowserPool has no connect factory")
        return self._factory

    async def start(self) -> None:
        """Probe every configured Chrome; only live processes become slots."""
        if self._started:
            return
        if self._start_gate is not None:
            await self._start_gate
            return
        loop = asyncio.get_running_loop()
        self._start_gate = loop.create_future()
        try:
            await self._do_start()
        except Exception as exc:
            if not self._start_gate.done():
                self._start_gate.set_exception(exc)
            self._start_gate = None
            raise
        else:
            self._started = True
            if not self._start_gate.done():
                self._start_gate.set_result(True)

    async def _do_start(self) -> None:
        self._idle = asyncio.Queue()
        factory = self._require_factory()
        results = await asyncio.gather(
            *[
                slot.connect(factory, self._wait_cdp, self._connect_timeout)
                for slot in self._slots
            ],
            return_exceptions=True,
        )
        ready: list[BrowserSlot] = []
        for slot, result in zip(self._slots, results):
            if isinstance(result, Exception):
                sys.stderr.write(
                    f"[cookie-server] Chrome slot {slot.index} "
                    f"({slot.cdp_http}) not ready: {result}\n"
                )
                self._spawn_reconnect(slot)
                continue
            ready.append(slot)
            await self._idle.put(slot)
        ports = ", ".join(slot.cdp_http for slot in ready) if ready else "none"
        sys.stderr.write(
            f"[cookie-server] pool: {len(ready)}/{len(self._slots)} "
            f"Chrome slots ready ({ports})\n"
        )

    async def close(self) -> None:
        self._stopped = True
        for slot in self._slots:
            task = slot._reconnect_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await slot.close_browser()

    async def acquire(self, timeout: float | None = None) -> BrowserSlot:
        if not self._started:
            await self.start()
        assert self._idle is not None
        wait = self.queue_timeout if timeout is None else float(timeout)
        deadline = asyncio.get_event_loop().time() + wait
        retry_after = max(1, int(self.queue_timeout) if self.queue_timeout >= 1 else 1)
        factory = self._require_factory()

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                slot = self._idle.get_nowait()
            except asyncio.QueueEmpty:
                if remaining <= 0:
                    raise SlotBusyError(
                        "all browser slots are busy",
                        retry_after=retry_after,
                    )
                try:
                    slot = await asyncio.wait_for(self._idle.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise SlotBusyError(
                        "all browser slots are busy",
                        retry_after=retry_after,
                    ) from None

            if getattr(slot.browser, "_closed", False):
                await slot.close_browser()
                self._spawn_reconnect(slot)
                continue

            if slot.browser is None or not slot.alive:
                try:
                    await slot.connect(factory, self._wait_cdp, min(5.0, max(remaining, 0.1)))
                except Exception as exc:
                    sys.stderr.write(
                        f"[cookie-server] Chrome slot {slot.index} "
                        f"({slot.cdp_http}) connect failed: {exc}\n"
                    )
                    await slot.close_browser()
                    self._spawn_reconnect(slot)
                    continue

            slot.busy = True
            return slot

    async def release(self, slot: BrowserSlot, *, dead: bool = False) -> None:
        slot.busy = False
        if self._stopped:
            await slot.close_browser()
            return
        if dead or getattr(slot.browser, "_closed", False):
            sys.stderr.write(
                f"[cookie-server] Chrome slot {slot.index} "
                f"({slot.cdp_http}) dropped; reconnecting\n"
            )
            await slot.close_browser()
            self._spawn_reconnect(slot)
            return
        if self._idle is None:
            return
        await self._idle.put(slot)

    @asynccontextmanager
    async def hold(self, timeout: float | None = None):
        slot = await self.acquire(timeout=timeout)
        dead = False
        try:
            yield slot.browser
        except ConnectionError:
            dead = True
            raise
        finally:
            if getattr(slot.browser, "_closed", False):
                dead = True
            await self.release(slot, dead=dead)

    def _spawn_reconnect(self, slot: BrowserSlot) -> None:
        if self._stopped:
            return
        task = slot._reconnect_task
        if task is not None and not task.done():
            return
        slot._reconnect_task = asyncio.create_task(self._reconnect(slot))

    async def _reconnect(self, slot: BrowserSlot) -> None:
        factory = self._require_factory()
        while not self._stopped:
            try:
                await slot.connect(factory, self._wait_cdp, timeout=5.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                sys.stderr.write(
                    f"[cookie-server] Chrome slot {slot.index} "
                    f"({slot.cdp_http}) reconnect failed: {exc}\n"
                )
                await asyncio.sleep(self._reconnect_wait)
                continue
            if self._stopped:
                await slot.close_browser()
                return
            if self._idle is not None:
                await self._idle.put(slot)
            sys.stderr.write(
                f"[cookie-server] Chrome slot {slot.index} "
                f"reconnected ({slot.cdp_http})\n"
            )
            return
