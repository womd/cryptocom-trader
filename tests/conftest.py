from __future__ import annotations

import math

from trader.exchange.models import Candle

STEP = 5 * 60_000
T0 = 1_700_006_400_000  # 2023-11-15 00:00 UTC (a multiple of 15m)


def candles_from_closes(closes: list[float], start: int = T0, step: int = STEP, wick=0.0005):
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(
            Candle(start + i * step, o, max(o, c) * (1 + wick), min(o, c) * (1 - wick), c, 1.0)
        )
        prev = c
    return out


def sine_closes(n: int, period_bars: int, base=100.0, amp=0.03, drift_per_bar=0.0):
    return [
        base * (1 + drift_per_bar) ** i * (1 + amp * math.sin(2 * math.pi * i / period_bars))
        for i in range(n)
    ]
