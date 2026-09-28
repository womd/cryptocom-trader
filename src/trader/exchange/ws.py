"""Market-data WebSocket client for Crypto.com Exchange API v1.

Handles the protocol chores so callers only see data:
- waits ~1s after connecting before sending (the exchange rate-limits early requests),
- answers `public/heartbeat` with `public/respond-heartbeat` (same id) or the server drops us,
- reconnects with exponential backoff and re-subscribes,
- emits a `connected` event on every (re)connect so callers can gap-fill via REST.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

MAX_CHANNELS_PER_CONNECTION = 400


@dataclass(slots=True)
class StreamEvent:
    kind: str  # "connected" | "data" | "error"
    channel: str = ""  # e.g. "ticker", "candlestick", "trade"
    subscription: str = ""  # e.g. "ticker.BTC_USD"
    symbol: str = ""
    data: list = field(default_factory=list)
    raw: dict | None = None


class MarketStream:
    def __init__(
        self,
        url: str,
        channels: Iterable[str],
        connect_delay: float = 1.0,
        max_backoff: float = 60.0,
        idle_timeout: float = 90.0,
        open_timeout: float = 15.0,
    ):
        self.url = url
        self.channels = list(dict.fromkeys(channels))
        if len(self.channels) > MAX_CHANNELS_PER_CONNECTION:
            raise ValueError(f"max {MAX_CHANNELS_PER_CONNECTION} channels per connection")
        self.connect_delay = connect_delay
        self.max_backoff = max_backoff
        self.idle_timeout = idle_timeout
        self.open_timeout = open_timeout
        self._ids = itertools.count(1)
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def _request(self, method: str, params: dict | None = None, req_id: int | None = None) -> str:
        msg = {"id": req_id if req_id is not None else next(self._ids), "method": method}
        if params is not None:
            msg["params"] = params
        msg["nonce"] = int(time.time() * 1000)
        return json.dumps(msg)

    async def events(self) -> AsyncIterator[StreamEvent]:
        backoff = min(1.0, self.max_backoff)
        while not self._stopped:
            try:
                async with connect(
                    self.url, open_timeout=self.open_timeout, ping_interval=None, max_size=2**22
                ) as ws:
                    await asyncio.sleep(self.connect_delay)
                    await ws.send(self._request("subscribe", {"channels": self.channels}))
                    log.info("ws connected url=%s channels=%d", self.url, len(self.channels))
                    yield StreamEvent(kind="connected")
                    backoff = min(1.0, self.max_backoff)
                    while not self._stopped:
                        # the server heartbeats every ~30s; silence means a dead connection
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout)
                        event = await self._handle(ws, raw)
                        if event is not None:
                            yield event
                    return
            except (TimeoutError, ConnectionClosed, OSError) as exc:
                reason = str(exc) or type(exc).__name__
            if self._stopped:
                return
            log.warning("ws disconnected (%s); reconnecting in %.1fs", reason, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def _handle(self, ws, raw: str | bytes) -> StreamEvent | None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("ws non-json message: %r", raw[:200])
            return None

        method = msg.get("method")
        if method == "public/heartbeat":
            await ws.send(self._request("public/respond-heartbeat", req_id=msg.get("id")))
            return None

        code = msg.get("code", 0)
        if code != 0:
            log.error("ws error code=%s message=%s", code, msg.get("message"))
            return StreamEvent(kind="error", raw=msg)

        result = msg.get("result")
        if method == "subscribe" and result and "data" in result:
            return StreamEvent(
                kind="data",
                channel=result.get("channel", ""),
                subscription=result.get("subscription", ""),
                symbol=result.get("instrument_name", ""),
                data=result.get("data") or [],
                raw=msg,
            )
        return None  # subscription acks etc.
