import pytest

from trader.exchange.models import Candle
from trader.market.indicators import EMA, RSI, Resampler, RollingExtremes


def test_ema_seeds_with_sma_then_smooths():
    ema = EMA(3)
    assert ema.update(1) is None
    assert ema.update(2) is None
    assert ema.update(3) == pytest.approx(2.0)
    assert ema.update(6) == pytest.approx(2.0 + 0.5 * (6 - 2.0))


def test_rsi_extremes_and_known_value():
    up = RSI(3)
    for x in [1, 2, 3, 4, 5]:
        up.update(x)
    assert up.value == 100.0

    rsi = RSI(2)
    for x in [10, 11, 10]:  # gains [1], losses [1] -> avg 0.5/0.5 -> 50
        rsi.update(x)
    assert rsi.value == pytest.approx(50.0)
    rsi.update(12)  # gain 2: avg_gain=(0.5+2)/2=1.25, avg_loss=0.25 -> rs=5
    assert rsi.value == pytest.approx(100 - 100 / 6)


def test_rolling_extremes_evicts_by_time():
    r = RollingExtremes(window_ms=3)
    r.update(0, high=10, low=5)
    r.update(1, high=8, low=6)
    r.update(2, high=9, low=4)
    assert (r.high, r.low) == (10, 4)
    r.update(3, high=7, low=7)  # ts 0 falls out of (0, 3]
    assert (r.high, r.low) == (9, 4)
    r.update(5, high=1, low=1)  # ts 1,2 fall out of (2, 5]
    assert (r.high, r.low) == (7, 1)


def test_resampler_aggregates_and_handles_gaps():
    rs = Resampler(5, 15)
    assert rs.update(Candle(0, 1, 2, 0.5, 1.5, 1)) == []
    assert rs.update(Candle(5, 1.5, 3, 1, 2, 1)) == []
    out = rs.update(Candle(10, 2, 2.5, 0.2, 2.2, 1))
    assert out == [Candle(0, 1, 3, 0.2, 2.2, 3)]
    # gap: bucket 15 gets only its first bar, then bucket 30 starts
    assert rs.update(Candle(15, 2.2, 2.3, 2.1, 2.25, 1)) == []
    out = rs.update(Candle(30, 3, 3, 3, 3, 1))
    assert out == [Candle(15, 2.2, 2.3, 2.1, 2.25, 1)]
