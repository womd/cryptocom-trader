"""Incremental (streaming) indicators.

Everything updates in O(1) per bar, so the identical code path serves live trading and
backtests. There's no vectorized re-computation that could drift from what live saw.
"""

from __future__ import annotations

from collections import deque

from trader.exchange.models import Candle


class EMA:
    """Exponential moving average, seeded with the SMA of the first `period` values."""

    def __init__(self, period: int):
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self._seed: list[float] = []

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
                self._seed = []
        else:
            self.value += self.alpha * (x - self.value)
        return self.value


class RSI:
    """Wilder's RSI."""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self._prev: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self._avg_gain = 0.0
        self._avg_loss = 0.0

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self.value is None and len(self._gains) < self.period:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._avg_gain = sum(self._gains) / self.period
            self._avg_loss = sum(self._losses) / self.period
        else:
            n = self.period
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n
        if self._avg_loss == 0:
            self.value = 100.0 if self._avg_gain > 0 else 50.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class RollingExtremes:
    """Max(high)/min(low) over a trailing time window, via monotonic deques."""

    def __init__(self, window_ms: int):
        self.window_ms = window_ms
        self._highs: deque[tuple[int, float]] = deque()
        self._lows: deque[tuple[int, float]] = deque()

    def update(self, ts: int, high: float, low: float) -> None:
        while self._highs and self._highs[-1][1] <= high:
            self._highs.pop()
        self._highs.append((ts, high))
        while self._lows and self._lows[-1][1] >= low:
            self._lows.pop()
        self._lows.append((ts, low))
        cutoff = ts - self.window_ms
        while self._highs[0][0] <= cutoff:
            self._highs.popleft()
        while self._lows[0][0] <= cutoff:
            self._lows.popleft()

    @property
    def high(self) -> float | None:
        return self._highs[0][1] if self._highs else None

    @property
    def low(self) -> float | None:
        return self._lows[0][1] if self._lows else None


class Resampler:
    """Aggregates base-interval candles into a larger interval (e.g. 5m -> 15m).

    Emits a bucket when its last base candle arrives, or when a later bucket starts
    (gap in data), so output never waits on a candle that doesn't exist.
    """

    def __init__(self, base_ms: int, target_ms: int):
        if target_ms % base_ms:
            raise ValueError("target interval must be a multiple of the base interval")
        self.base_ms = base_ms
        self.target_ms = target_ms
        self._bucket: int | None = None
        self._o = self._h = self._l = self._c = self._v = 0.0

    def update(self, c: Candle) -> list[Candle]:
        out: list[Candle] = []
        bucket = c.ts - c.ts % self.target_ms
        if self._bucket is not None and bucket != self._bucket:
            out.append(self._emit())
        if self._bucket is None:
            self._bucket = bucket
            self._o, self._h, self._l, self._c, self._v = c.open, c.high, c.low, c.close, c.volume
        else:
            self._h = max(self._h, c.high)
            self._l = min(self._l, c.low)
            self._c = c.close
            self._v += c.volume
        if c.ts + self.base_ms == bucket + self.target_ms:
            out.append(self._emit())
        return out

    def _emit(self) -> Candle:
        candle = Candle(self._bucket, self._o, self._h, self._l, self._c, self._v)
        self._bucket = None
        return candle
