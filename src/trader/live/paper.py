"""Live paper trading: real-time market data over WebSocket, simulated execution.

- Warm-up: backfills enough history via REST to make indicators ready immediately.
- Tickers drive order matching (as degenerate bars, with live bid/ask for market fills).
- Candlestick updates are folded into closed candles: a candle is closed when a newer one
  starts, or when exchange time (from tickers) passes its end plus a small grace period.
- On every (re)connect, missed candles are fetched via REST and fed as warm-up only
  (no decisions on stale bars).
- Engine/broker/strategy state is persisted to SQLite, so restarts resume positions.
"""

from __future__ import annotations

import logging
import time

from trader.backtest.engine import build_engine
from trader.config import Settings
from trader.exchange.models import Candle, Instrument, Ticker, interval_ms
from trader.exchange.rest import RestClient
from trader.exchange.ws import MarketStream
from trader.execution.base import Quote
from trader.market.backfill import backfill
from trader.market.state import warmup_ms
from trader.storage.db import Database

log = logging.getLogger(__name__)

CLOSE_GRACE_MS = 5_000
SAVE_EVERY_S = 30.0


def _ms() -> int:
    return int(time.time() * 1000)


class PaperRunner:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        rest: RestClient,
        instruments: dict[str, Instrument],
        run: str = "paper",
        reset: bool = False,
    ):
        self.settings = settings
        self.db = db
        self.rest = rest
        self.run = run
        self.interval = settings.strategy.interval
        self.step = interval_ms(self.interval)
        self.engine = build_engine(settings, instruments, self._on_event)
        self.last_closed: dict[str, int] = {}
        self._pending: dict[str, Candle] = {}
        self._last_save = 0.0
        self.stream: MarketStream | None = None

        saved = None if reset else db.load_state(self.state_key)
        if saved:
            self.engine.load_dict(saved["engine"])
            log.info("restored paper state %s: equity=%.2f", run, self.engine.broker.equity())

    @property
    def state_key(self) -> str:
        return f"paper:{self.run}"

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        now = _ms()
        self.engine.trading_start_ts = now
        start = now - warmup_ms(self.settings.strategy)
        for sym in self.settings.symbols:
            await backfill(self.rest, self.db, sym, self.interval, start, now)
            for c in self.db.iter_candles(sym, self.interval, start, now):
                self._feed_closed(sym, c)
            state = self.engine.states[sym]
            log.info("warm-up %s: ready=%s %s", sym, state.ready, state.snapshot())

    async def run_forever(self) -> None:
        await self.start()
        channels = [f"ticker.{s}" for s in self.settings.symbols] + [
            f"candlestick.{self.interval}.{s}" for s in self.settings.symbols
        ]
        self.stream = MarketStream(self.settings.market_ws_url, channels)
        try:
            async for ev in self.stream.events():
                if ev.kind == "connected":
                    await self._gap_fill()
                elif ev.kind == "data":
                    self.handle(ev.channel, ev.symbol, ev.data)
                if time.monotonic() - self._last_save > SAVE_EVERY_S:
                    self.save()
        finally:
            self.save()

    def stop(self) -> None:
        if self.stream:
            self.stream.stop()

    # ---- message handling (sync, unit-testable) ----------------------------

    def handle(self, channel: str, symbol: str, data: list[dict]) -> None:
        if symbol not in self.engine.states:
            return
        if channel == "ticker":
            for d in data:
                t = Ticker.from_api(d, symbol)
                self._close_stale(symbol, t.ts)
                if t.last > 0:
                    bar = Candle(t.ts, t.last, t.last, t.last, t.last)
                    self.engine.on_bar(symbol, bar, Quote(t.bid or None, t.ask or None))
        elif channel == "candlestick":
            for d in sorted(data, key=lambda x: int(x["t"])):
                self._on_candle_update(symbol, Candle.from_api(d))

    def _on_candle_update(self, symbol: str, c: Candle) -> None:
        if c.ts <= self.last_closed.get(symbol, -1):
            return
        pending = self._pending.get(symbol)
        if pending is not None and c.ts > pending.ts:
            self._close(symbol, pending)
        self._pending[symbol] = c

    def _close_stale(self, symbol: str, exchange_ts: int) -> None:
        pending = self._pending.get(symbol)
        if pending is not None and exchange_ts >= pending.ts + self.step + CLOSE_GRACE_MS:
            self._close(symbol, pending)

    def _close(self, symbol: str, c: Candle) -> None:
        self._pending.pop(symbol, None)
        if c.ts <= self.last_closed.get(symbol, -1):
            return
        self.db.upsert_candles(symbol, self.interval, [c])
        self._feed_closed(symbol, c)
        broker = self.engine.broker
        self.db.record_equity(self.run, c.ts + self.step, broker.equity(), broker.cash)

    def _feed_closed(self, symbol: str, c: Candle) -> None:
        if c.ts <= self.last_closed.get(symbol, -1):
            return
        self.last_closed[symbol] = c.ts
        self.engine.on_candle(symbol, c)

    async def _gap_fill(self) -> None:
        now = _ms()
        saved_start = self.engine.trading_start_ts
        self.engine.trading_start_ts = now  # missed bars update state only
        try:
            for sym in self.settings.symbols:
                since = self.last_closed.get(sym)
                if since is None:
                    continue
                await backfill(self.rest, self.db, sym, self.interval, since + self.step, now)
                for c in self.db.iter_candles(sym, self.interval, since + self.step, now):
                    self._feed_closed(sym, c)
        finally:
            self.engine.trading_start_ts = max(saved_start, now)

    # ---- persistence / events ---------------------------------------------

    def save(self) -> None:
        self.db.save_state(self.state_key, {"engine": self.engine.to_dict(), "saved_ms": _ms()})
        self._last_save = time.monotonic()

    def _on_event(self, kind: str, ts: int, symbol: str | None, data: dict) -> None:
        self.db.journal(self.run, ts, kind, symbol, data)
        log.info("%s %s %s", kind, symbol or "-", data)
        if kind in ("fill", "halt"):
            self.save()
