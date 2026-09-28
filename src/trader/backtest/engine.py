"""Event-driven backtester: replays candles through the live Engine with a PaperBroker."""

from __future__ import annotations

import heapq
from collections import Counter
from dataclasses import dataclass, field

from trader.config import Settings
from trader.core.engine import Engine
from trader.exchange.models import Candle, Instrument
from trader.execution.base import Position, RoundTrip
from trader.execution.paper import PaperBroker
from trader.risk.manager import RiskManager
from trader.strategy.range_trend import RangeTrend


@dataclass
class BacktestResult:
    start_ts: int
    end_ts: int
    initial_equity: float
    final_equity: float
    equity_curve: list[tuple[int, float]]
    round_trips: list[RoundTrip]
    open_positions: list[Position]
    fees_paid: float
    exposure: float  # fraction of time steps with at least one open position
    benchmark_return: float  # equal-weight buy-and-hold over the same period, after fees
    events: Counter = field(default_factory=Counter)
    rejects: Counter = field(default_factory=Counter)


def build_engine(
    settings: Settings,
    instruments: dict[str, Instrument] | None = None,
    on_event=None,
) -> Engine:
    instruments = instruments or {}
    broker = PaperBroker(settings.fees, settings.starting_cash, instruments)
    risk = RiskManager(
        settings.risk, settings.fees, {s: i.qty_tick for s, i in instruments.items()}
    )
    return Engine(settings, RangeTrend(settings.strategy), risk, broker, on_event)


def run_backtest(
    settings: Settings,
    candles: dict[str, list[Candle]],
    start_ts: int,
    end_ts: int,
    instruments: dict[str, Instrument] | None = None,
) -> BacktestResult:
    """`candles` should include warm-up history before `start_ts` (see market.state.warmup_ms)."""
    events: Counter = Counter()
    rejects: Counter = Counter()

    def sink(kind, ts, symbol, data):
        events[kind] += 1
        if kind == "reject":
            rejects[data["reason"]] += 1

    engine = build_engine(settings, instruments, sink)
    engine.trading_start_ts = start_ts
    broker = engine.broker
    bar_ms = engine.bar_ms

    def stream(sym: str, series: list[Candle]):
        return ((c.ts, sym, c) for c in series if c.ts < end_ts)

    streams = [stream(sym, series) for sym, series in candles.items() if sym in engine.states]
    curve: list[tuple[int, float]] = []
    in_market = steps = 0
    current_ts: int | None = None

    def close_step(ts: int) -> None:
        nonlocal in_market, steps
        if ts + bar_ms > start_ts:
            curve.append((ts + bar_ms, broker.equity()))
            steps += 1
            in_market += bool(broker.positions)

    for ts, sym, candle in heapq.merge(*streams, key=lambda x: (x[0], x[1])):
        if current_ts is not None and ts != current_ts:
            close_step(current_ts)
        current_ts = ts
        engine.on_bar(sym, candle)
        engine.on_candle(sym, candle)
    if current_ts is not None:
        close_step(current_ts)

    return BacktestResult(
        start_ts=start_ts,
        end_ts=end_ts,
        initial_equity=settings.starting_cash,
        final_equity=broker.equity(),
        equity_curve=curve,
        round_trips=list(broker.round_trips),
        open_positions=list(broker.positions.values()),
        fees_paid=broker.fees_paid,
        exposure=in_market / steps if steps else 0.0,
        benchmark_return=buy_and_hold(candles, start_ts, end_ts, settings.fees.taker),
        events=events,
        rejects=rejects,
    )


def buy_and_hold(
    candles: dict[str, list[Candle]], start_ts: int, end_ts: int, taker_fee: float
) -> float:
    rets = []
    for series in candles.values():
        window = [c for c in series if start_ts <= c.ts < end_ts]
        if len(window) >= 2:
            gross = window[-1].close / window[0].open
            rets.append(gross * (1 - taker_fee) ** 2 - 1)
    return sum(rets) / len(rets) if rets else 0.0
