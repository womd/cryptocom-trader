import pytest
from conftest import STEP, T0, candles_from_closes, sine_closes

from trader.backtest.engine import run_backtest
from trader.backtest.metrics import format_report, max_drawdown, summarize
from trader.config import Settings, StrategyParams
from trader.execution.base import OrderType, Position
from trader.market.state import MarketState, Trend, warmup_ms
from trader.strategy.base import Action, SymbolContext
from trader.strategy.range_trend import RangeTrend

DAY_BARS = 288


def small_params(**kw) -> StrategyParams:
    base = dict(ema_fast=8, ema_slow=32, slope_lookback=2, window_hours=12)
    base.update(kw)
    return StrategyParams(**base)


def ready_state(params, closes) -> MarketState:
    state = MarketState("AAA_USD", params)
    for c in candles_from_closes(closes):
        state.update(c)
    assert state.ready
    return state


def test_entries_only_near_range_low_and_never_in_downtrend():
    params = small_params()
    strat = RangeTrend(params)
    state = MarketState("AAA_USD", params)
    entries = 0
    for c in candles_from_closes(sine_closes(6 * DAY_BARS, period_bars=144)):
        state.update(c)
        ctx = SymbolContext(c.ts + STEP, None, False, None)
        sig = strat.evaluate(state, ctx)
        if sig:
            assert sig.action is Action.ENTER and sig.order_type is OrderType.LIMIT
            assert state.range_position(c.close) <= params.entry_zone
            assert state.trend() is not Trend.DOWN
            assert sig.price == c.close
            entries += 1
    assert entries > 0


def test_exit_rules():
    params = small_params()
    state = ready_state(params, sine_closes(3 * DAY_BARS, period_bars=144))
    last = state.last
    pos = Position("AAA_USD", 1, 100, entry_ts=last.ts)
    ts = last.ts + STEP
    strat = RangeTrend(params)

    old = Position("AAA_USD", 1, 100, entry_ts=ts - int(params.max_hold_hours * 3_600_000))
    sig = strat.evaluate(state, SymbolContext(ts, old, False, None))
    assert sig.reason == "time_stop" and sig.order_type is OrderType.MARKET

    state.trend = lambda: Trend.DOWN
    sig = strat.evaluate(state, SymbolContext(ts, pos, False, None))
    assert sig.reason == "trend_down"

    state.range_position = lambda price: 0.95
    state.trend = lambda: Trend.FLAT
    sig = strat.evaluate(state, SymbolContext(ts, pos, False, None))
    assert sig.reason == "range_high" and sig.order_type is OrderType.LIMIT
    assert strat.evaluate(state, SymbolContext(ts, pos, False, OrderType.LIMIT)) is None

    state.trend = lambda: Trend.UP  # at the high in an uptrend: trail instead of selling
    assert strat.evaluate(state, SymbolContext(ts, pos, False, None)) is None
    assert strat.to_dict()["trail_peak"]["AAA_USD"] == last.close


def test_cooldown_after_exit():
    params = small_params(cooldown_bars=3)
    strat = RangeTrend(params)
    strat.on_flat("AAA_USD", 1_000)
    assert strat.to_dict()["cooldown_until"]["AAA_USD"] == 1_000 + 3 * STEP


def _settings(tmp_path, symbols=("AAA_USD",), **strategy_kw) -> Settings:
    return Settings(
        _env_file=None,
        db_path=tmp_path / "t.db",
        symbols=list(symbols),
        strategy=small_params(**strategy_kw),
    )


def _run(settings, series: dict, days: float):
    end = max(s[-1].ts for s in series.values()) + STEP
    start = end - int(days * 86_400_000)
    return run_backtest(settings, series, start, end)


def test_backtest_profits_from_a_ranging_market(tmp_path):
    settings = _settings(tmp_path)
    series = {"AAA_USD": candles_from_closes(sine_closes(14 * DAY_BARS, period_bars=144))}
    result = _run(settings, series, days=12)
    s = summarize(result)
    assert s["trades"] >= 5
    assert s["total_return"] > 0.01
    assert s["total_return"] > s["benchmark_return"]
    assert s["max_drawdown"] < 0.05
    assert "range_trend" not in format_report(result)  # report renders without error


def test_trend_filter_avoids_trading_a_downtrend(tmp_path):
    closes = sine_closes(14 * DAY_BARS, period_bars=144, amp=0.02, drift_per_bar=-0.0002)
    series = {"AAA_USD": candles_from_closes(closes)}
    filtered = summarize(_run(_settings(tmp_path), series, days=12))
    unfiltered = summarize(_run(_settings(tmp_path, trend_filter=False), series, days=12))
    # without the filter every bounce entry gets stopped out; with it, positions are cut
    # early when the trend turns down again
    assert unfiltered["exit_reasons"]["stop"] > 0
    assert filtered["exit_reasons"]["stop"] < unfiltered["exit_reasons"]["stop"]
    assert filtered["total_return"] > unfiltered["total_return"]
    assert filtered["total_return"] > filtered["benchmark_return"]


def test_multi_symbol_round_trips_keep_their_symbol(tmp_path):
    settings = _settings(tmp_path, symbols=("AAA_USD", "BBB_USD"))
    series = {
        "AAA_USD": candles_from_closes(sine_closes(10 * DAY_BARS, 144, base=100)),
        "BBB_USD": candles_from_closes(sine_closes(10 * DAY_BARS, 100, base=1000)),
    }
    result = _run(settings, series, days=8)
    symbols = {t.symbol for t in result.round_trips}
    assert symbols == {"AAA_USD", "BBB_USD"}
    for t in result.round_trips:
        level = 100 if t.symbol == "AAA_USD" else 1000
        assert level * 0.9 < t.entry_price < level * 1.1


def test_position_sizing_and_caps_respected(tmp_path):
    settings = _settings(tmp_path, symbols=("AAA_USD", "BBB_USD"))
    settings.risk.max_open_positions = 1
    series = {
        "AAA_USD": candles_from_closes(sine_closes(10 * DAY_BARS, 144, base=100)),
        "BBB_USD": candles_from_closes(sine_closes(10 * DAY_BARS, 144, base=1000)),
    }
    result = _run(settings, series, days=8)
    trips = sorted(result.round_trips, key=lambda t: t.entry_ts)
    for a, b in zip(trips, trips[1:], strict=False):
        assert b.entry_ts >= a.exit_ts  # never two positions at once
    for t in trips:
        assert t.entry_price * t.qty <= settings.starting_cash * 0.10 * 1.2
    assert result.rejects["max_open_positions"] > 0


def test_max_drawdown():
    assert max_drawdown([(0, 100), (1, 120), (2, 90), (3, 130)]) == pytest.approx(0.25)


def test_warmup_covers_default_indicators():
    p = StrategyParams()
    assert warmup_ms(p) >= 200 * 15 * 60_000
    assert T0 % (15 * 60_000) == 0
