from conftest import STEP, candles_from_closes, sine_closes

from trader.config import Settings, StrategyParams
from trader.execution.base import Order, OrderType, Side
from trader.live.paper import CLOSE_GRACE_MS, PaperRunner
from trader.storage.db import Database

SYM = "AAA_USD"


def make_runner(tmp_path, reset=False) -> PaperRunner:
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "t.db",
        symbols=[SYM],
        strategy=StrategyParams(ema_fast=8, ema_slow=32, slope_lookback=2, window_hours=12),
    )
    db = Database(settings.db_path)
    return PaperRunner(settings, db, rest=None, instruments={}, run="t", reset=reset)


def api_candle(ts, px):
    return {"t": ts, "o": px, "h": px + 1, "l": px - 1, "c": px, "v": 1}


def api_ticker(ts, last, bid=None, ask=None):
    return {
        "i": SYM,
        "t": ts,
        "a": last,
        "b": bid or last - 0.01,
        "k": ask or last + 0.01,
        "h": last,
        "l": last,
        "v": 1,
    }


def test_candle_updates_close_on_next_candle_or_by_time(tmp_path):
    r = make_runner(tmp_path)
    history = candles_from_closes(sine_closes(300, 144))
    for c in history:
        r._feed_closed(SYM, c)
    t = history[-1].ts + STEP
    r.engine.trading_start_ts = t

    r.handle("candlestick", SYM, [api_candle(t, 100)])
    r.handle("candlestick", SYM, [api_candle(t, 101)])  # update of the same bar
    assert r.last_closed[SYM] == history[-1].ts
    r.handle("candlestick", SYM, [api_candle(t + STEP, 102)])
    assert r.last_closed[SYM] == t
    stored = list(r.db.iter_candles(SYM, "5m", t, t + 1))
    assert stored[0].close == 101

    # no new candle arrives (quiet market): ticker time closes the pending bar
    r.handle("ticker", SYM, [api_ticker(t + 2 * STEP + CLOSE_GRACE_MS - 1, 102)])
    assert r.last_closed[SYM] == t
    r.handle("ticker", SYM, [api_ticker(t + 2 * STEP + CLOSE_GRACE_MS, 102)])
    assert r.last_closed[SYM] == t + STEP

    # late/duplicate candles are ignored
    r.handle("candlestick", SYM, [api_candle(t, 50)])
    assert SYM not in r._pending


def test_ticker_fills_orders_journals_and_state_survives_restart(tmp_path):
    r = make_runner(tmp_path)
    r.engine.broker.place(
        Order(SYM, Side.BUY, OrderType.LIMIT, 1.0, 0, price=100, tag="entry", stop_loss_pct=0.02)
    )
    r.handle("ticker", SYM, [api_ticker(1_000, 100.5)])
    assert SYM not in r.engine.broker.positions
    r.handle("ticker", SYM, [api_ticker(2_000, 99.8)])
    assert r.engine.broker.positions[SYM].qty == 1.0
    kinds = [row[1] for row in r.db.recent_journal("t")]
    assert "fill" in kinds

    restored = make_runner(tmp_path)
    assert restored.engine.broker.positions[SYM].avg_price == 100
    assert [o.type for o in restored.engine.broker.open_orders()] == [OrderType.STOP]

    fresh = make_runner(tmp_path, reset=True)
    assert fresh.engine.broker.positions == {}
