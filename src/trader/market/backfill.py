"""Download historical candles into SQLite, fetching only what's missing."""

from __future__ import annotations

import logging
import time

from trader.exchange.models import interval_ms
from trader.exchange.rest import RestClient
from trader.storage.db import Database

log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


async def backfill(
    rest: RestClient,
    db: Database,
    symbol: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    now: int | None = None,
) -> int:
    """Ensure closed candles covering [start_ts, end_ts) are stored. Returns rows written.

    Only extends the stored range at either end; it doesn't hunt for holes in the middle
    (the exchange has none for liquid pairs; `trader backfill --force` refetches everything).
    """
    step = interval_ms(interval)
    now = now or now_ms()
    end_ts = min(end_ts, now - now % step)  # never store the still-open candle
    lo, hi = db.candle_bounds(symbol, interval)
    ranges: list[tuple[int, int]] = []
    if lo is None or hi is None:
        ranges.append((start_ts, end_ts))
    else:
        if start_ts < lo:
            ranges.append((start_ts, lo))
        if end_ts > hi + step:
            ranges.append((hi + step, end_ts))

    written = 0
    for a, b in ranges:
        if a >= b:
            continue
        log.info("backfill %s %s: %d candles to fetch", symbol, interval, (b - a) // step)
        async for batch in rest.iter_candle_windows(symbol, interval, a, b):
            closed = [c for c in batch if c.ts + step <= now]
            written += db.upsert_candles(symbol, interval, closed)
    return written
