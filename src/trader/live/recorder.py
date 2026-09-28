"""Records live ticker and trade streams to SQLite for later replay/analysis."""

from __future__ import annotations

import logging
import time

from trader.exchange.models import Ticker, Trade
from trader.exchange.ws import MarketStream
from trader.storage.db import Database

log = logging.getLogger(__name__)

FLUSH_EVERY_S = 2.0
FLUSH_AT = 1000


async def record(url: str, symbols: list[str], db: Database, trades: bool = True) -> None:
    channels = [f"ticker.{s}" for s in symbols]
    if trades:
        channels += [f"trade.{s}" for s in symbols]
    stream = MarketStream(url, channels)
    ticks: list[Ticker] = []
    fills: list[Trade] = []
    last_flush = time.monotonic()
    total = 0

    def flush() -> None:
        nonlocal last_flush, total
        if ticks:
            db.insert_ticks(ticks)
        if fills:
            db.insert_trades(fills)
        total += len(ticks) + len(fills)
        ticks.clear()
        fills.clear()
        last_flush = time.monotonic()

    try:
        async for ev in stream.events():
            if ev.kind != "data":
                continue
            if ev.channel == "ticker":
                ticks.extend(Ticker.from_api(d, ev.symbol) for d in ev.data)
            elif ev.channel == "trade":
                fills.extend(Trade.from_api(d, ev.symbol) for d in ev.data)
            if len(ticks) + len(fills) >= FLUSH_AT or time.monotonic() - last_flush > FLUSH_EVERY_S:
                flush()
                log.debug("recorded %d rows", total)
    finally:
        flush()
        log.info("recorder stopped; %d rows written", total)
