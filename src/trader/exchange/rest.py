"""Async REST client for Crypto.com Exchange API v1."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time

import httpx

from .auth import sign_request
from .models import Candle, Instrument, interval_ms

log = logging.getLogger(__name__)

MAX_CANDLES_PER_REQUEST = 300


class ExchangeError(RuntimeError):
    def __init__(self, method: str, code, message: str):
        super().__init__(f"{method}: code={code} {message}")
        self.code = code


class RestClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        timeout: float = 15.0,
        min_request_interval: float = 0.05,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._api_key = api_key
        self._api_secret = api_secret
        self._ids = itertools.count(1)
        self._min_interval = min_request_interval
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> RestClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    @staticmethod
    def _unwrap(method: str, body: dict) -> dict:
        code = body.get("code", 0)
        if code != 0:
            raise ExchangeError(method, code, body.get("message", ""))
        return body.get("result") or {}

    async def public(self, method: str, params: dict | None = None) -> dict:
        await self._throttle()
        query = {k: v for k, v in (params or {}).items() if v is not None}
        resp = await self._client.get(method, params=query)
        resp.raise_for_status()
        return self._unwrap(method, resp.json())

    async def private(self, method: str, params: dict | None = None) -> dict:
        if not (self._api_key and self._api_secret):
            raise RuntimeError("API key/secret not configured (set TRADER_API_KEY/SECRET in .env)")
        await self._throttle()
        req = {
            "id": next(self._ids),
            "method": method,
            "params": params or {},
            "nonce": int(time.time() * 1000),
        }
        signed = sign_request(req, self._api_key, self._api_secret)
        resp = await self._client.post(method, json=signed)
        resp.raise_for_status()
        return self._unwrap(method, resp.json())

    # ---- public market data -------------------------------------------------

    async def get_instruments(self) -> list[Instrument]:
        result = await self.public("public/get-instruments")
        return [Instrument.from_api(d) for d in result.get("data", [])]

    async def get_candles(
        self,
        symbol: str,
        interval: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        count: int = MAX_CANDLES_PER_REQUEST,
    ) -> list[Candle]:
        result = await self.public(
            "public/get-candlestick",
            {
                "instrument_name": symbol,
                "timeframe": interval,
                "count": count,
                "start_ts": start_ts,
                "end_ts": end_ts,
            },
        )
        candles = [Candle.from_api(d) for d in result.get("data", [])]
        candles.sort(key=lambda c: c.ts)
        return candles

    async def iter_candle_windows(self, symbol: str, interval: str, start_ts: int, end_ts: int):
        """Yield candle batches covering [start_ts, end_ts), walking forward in fixed windows.

        Explicit windows make paging independent of the API's result ordering.
        """
        step = interval_ms(interval)
        window = step * MAX_CANDLES_PER_REQUEST
        cursor = start_ts - start_ts % step
        while cursor < end_ts:
            window_end = min(cursor + window, end_ts)
            batch = await self.get_candles(symbol, interval, cursor, window_end - 1)
            yield [c for c in batch if cursor <= c.ts < window_end]
            cursor = window_end

    # ---- private -----------------------------------------------------------

    async def get_user_balance(self) -> dict:
        return await self.private("private/user-balance")
