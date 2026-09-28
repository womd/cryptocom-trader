"""Per-symbol market state derived from closed candles."""

from __future__ import annotations

from collections import deque
from enum import StrEnum

from trader.config import StrategyParams
from trader.exchange.models import Candle, interval_ms

from .indicators import EMA, RSI, Resampler, RollingExtremes


class Trend(StrEnum):
    UP = "up"
    FLAT = "flat"
    DOWN = "down"


def warmup_ms(p: StrategyParams) -> int:
    """History needed before the state is `ready`, plus a margin for data gaps."""
    trend_ms = interval_ms(p.trend_interval)
    need = max(
        int(p.window_hours * 3_600_000),
        (p.ema_slow + p.slope_lookback + 1) * trend_ms,
        (p.rsi_period + 2) * interval_ms(p.interval),
    )
    return int(need * 1.1)


class MarketState:
    def __init__(self, symbol: str, params: StrategyParams):
        self.symbol = symbol
        self.params = params
        self.interval_ms = interval_ms(params.interval)
        self.window_ms = int(params.window_hours * 3_600_000)

        self.range = RollingExtremes(self.window_ms)
        self.rsi = RSI(params.rsi_period)
        self.rsi_prev: float | None = None
        self._resampler = Resampler(self.interval_ms, interval_ms(params.trend_interval))
        self.ema_fast = EMA(params.ema_fast)
        self.ema_slow = EMA(params.ema_slow)
        self._fast_hist: deque[float] = deque(maxlen=params.slope_lookback + 1)

        self.last: Candle | None = None
        self.prev: Candle | None = None
        self.first_ts: int | None = None

    def update(self, c: Candle) -> None:
        if self.last is not None and c.ts <= self.last.ts:
            return  # duplicate / out-of-order candle
        if self.first_ts is None:
            self.first_ts = c.ts
        self.prev, self.last = self.last, c
        self.range.update(c.ts, c.high, c.low)
        self.rsi_prev = self.rsi.value
        self.rsi.update(c.close)
        for tc in self._resampler.update(c):
            self.ema_slow.update(tc.close)
            fast = self.ema_fast.update(tc.close)
            if fast is not None:
                self._fast_hist.append(fast)

    # ---- derived values ----------------------------------------------------

    @property
    def window_full(self) -> bool:
        return (
            self.last is not None
            and self.first_ts is not None
            and self.last.ts - self.first_ts >= self.window_ms - self.interval_ms
        )

    @property
    def ready(self) -> bool:
        return (
            self.window_full
            and self.prev is not None
            and self.rsi.value is not None
            and self.rsi_prev is not None
            and self.ema_slow.value is not None
            and len(self._fast_hist) == self._fast_hist.maxlen
        )

    @property
    def range_pct(self) -> float | None:
        h, low = self.range.high, self.range.low
        if h is None or low is None or not self.last:
            return None
        return (h - low) / self.last.close

    def range_position(self, price: float) -> float | None:
        """0 at the rolling low, 1 at the rolling high."""
        h, low = self.range.high, self.range.low
        if h is None or low is None or h <= low:
            return None
        return (price - low) / (h - low)

    def trend(self) -> Trend | None:
        fast, slow = self.ema_fast.value, self.ema_slow.value
        if fast is None or slow is None or len(self._fast_hist) < self._fast_hist.maxlen:
            return None
        diff = (fast - slow) / slow
        slope = (self._fast_hist[-1] - self._fast_hist[0]) / self._fast_hist[0]
        band = self.params.trend_band
        if diff > band and slope > 0:
            return Trend.UP
        if diff < -band and slope < 0:
            return Trend.DOWN
        return Trend.FLAT

    def snapshot(self) -> dict:
        return {
            "close": self.last.close if self.last else None,
            "range_high": self.range.high,
            "range_low": self.range.low,
            "range_pct": self.range_pct,
            "position": self.range_position(self.last.close) if self.last else None,
            "rsi": self.rsi.value,
            "ema_fast": self.ema_fast.value,
            "ema_slow": self.ema_slow.value,
            "trend": self.trend(),
            "ready": self.ready,
        }
