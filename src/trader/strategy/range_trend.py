"""`range_trend`: buy near the rolling-24h low, sell near the high, filtered by trend.

Entry (long, when flat):
  - state ready, no cooldown, no pending entry
  - range is wide enough: (H - L) / price >= min_range_pct (so fees can be covered)
  - position in range p <= entry_zone
  - trend is not DOWN (when trend_filter)
  - reversal confirmation (when require_confirmation):
      close > previous bar's high, or RSI crosses back above rsi_oversold
  -> post-only LIMIT buy at the close

Exit (when long), first match wins:
  - held >= max_hold_hours                 -> MARKET  (time stop)
  - trend turned DOWN (when trend_filter)  -> MARKET
  - trailing stop active and close <= peak * (1 - trail_pct) -> MARKET
  - p >= exit_zone:
      trend UP and trail_in_uptrend        -> activate trailing stop, keep holding
      otherwise                            -> LIMIT sell at the close (take profit)
  The hard stop-loss is a resting STOP order owned by the risk layer, not a signal.
"""

from __future__ import annotations

from trader.config import StrategyParams
from trader.exchange.models import interval_ms
from trader.execution.base import OrderType
from trader.market.state import MarketState, Trend

from .base import Action, Signal, SymbolContext


class RangeTrend:
    name = "range_trend"

    def __init__(self, params: StrategyParams):
        self.p = params
        self._cooldown_until: dict[str, int] = {}
        self._trail_peak: dict[str, float] = {}

    def on_flat(self, symbol: str, ts: int) -> None:
        self._trail_peak.pop(symbol, None)
        self._cooldown_until[symbol] = ts + self.p.cooldown_bars * interval_ms(self.p.interval)

    def evaluate(self, state: MarketState, ctx: SymbolContext) -> Signal | None:
        if not state.ready or state.last is None:
            return None
        close = state.last.close
        pos_in_range = state.range_position(close)
        if pos_in_range is None:
            return None
        trend = state.trend() if self.p.trend_filter else None
        info = {
            "p": round(pos_in_range, 4),
            "range_pct": round(state.range_pct or 0.0, 5),
            "trend": trend,
            "rsi": round(state.rsi.value or 0.0, 2),
        }
        if ctx.position is None:
            return self._entry(state, ctx, close, pos_in_range, trend, info)
        return self._exit(state, ctx, close, pos_in_range, trend, info)

    def _entry(self, state, ctx, close, pos_in_range, trend, info) -> Signal | None:
        sym = state.symbol
        if ctx.pending_entry or ctx.ts < self._cooldown_until.get(sym, 0):
            return None
        if (state.range_pct or 0.0) < self.p.min_range_pct:
            return None
        if pos_in_range > self.p.entry_zone:
            return None
        if trend is Trend.DOWN:
            return None
        if self.p.require_confirmation:
            prev = state.prev
            bounced = prev is not None and close > prev.high
            rsi_cross = (
                state.rsi_prev is not None
                and state.rsi.value is not None
                and state.rsi_prev < self.p.rsi_oversold <= state.rsi.value
            )
            if not (bounced or rsi_cross):
                return None
            info["confirm"] = "bounce" if bounced else "rsi_cross"
        return Signal(sym, Action.ENTER, OrderType.LIMIT, "range_low", price=close, info=info)

    def _exit(self, state, ctx, close, pos_in_range, trend, info) -> Signal | None:
        sym = state.symbol
        if ctx.pending_exit is OrderType.MARKET:
            return None

        def market(reason: str) -> Signal:
            return Signal(sym, Action.EXIT, OrderType.MARKET, reason, info=info)

        held_ms = ctx.ts - ctx.position.entry_ts
        if held_ms >= self.p.max_hold_hours * 3_600_000:
            return market("time_stop")
        if trend is Trend.DOWN:
            return market("trend_down")

        peak = self._trail_peak.get(sym)
        if peak is not None:
            peak = max(peak, close)
            self._trail_peak[sym] = peak
            info["trail_peak"] = peak
            if close <= peak * (1 - self.p.trail_pct):
                return market("trailing_stop")
            return None

        if pos_in_range >= self.p.exit_zone:
            if self.p.trail_in_uptrend and trend is Trend.UP:
                self._trail_peak[sym] = close
                return None
            if ctx.pending_exit is OrderType.LIMIT:
                return None
            return Signal(sym, Action.EXIT, OrderType.LIMIT, "range_high", price=close, info=info)
        return None

    # ---- persistence (live paper restarts) --------------------------------

    def to_dict(self) -> dict:
        return {"cooldown_until": self._cooldown_until, "trail_peak": self._trail_peak}

    def load_dict(self, d: dict) -> None:
        self._cooldown_until = dict(d.get("cooldown_until", {}))
        self._trail_peak = dict(d.get("trail_peak", {}))
