import pytest

from trader.config import FeeParams
from trader.exchange.models import Candle, Instrument
from trader.execution.base import Order, OrderStatus, OrderType, Quote, Side
from trader.execution.paper import PaperBroker

FEES = FeeParams(maker=0.001, taker=0.002, slippage=0.0, half_spread=0.0)


def bar(ts, o, h, low, c):
    return Candle(ts, o, h, low, c, 1.0)


def buy_limit(price, qty=1.0, ts=0, **kw):
    return Order("X_USD", Side.BUY, OrderType.LIMIT, qty, ts, price=price, tag="entry", **kw)


def test_limit_needs_trade_through_and_no_same_bar_lookahead():
    b = PaperBroker(FEES, 1000)
    b.place(buy_limit(100, ts=10))
    assert b.on_bar("X_USD", bar(5, 99, 99, 90, 95)) == []  # before created_ts
    assert b.on_bar("X_USD", bar(10, 101, 102, 100, 101)) == []  # touched, not through
    fills = b.on_bar("X_USD", bar(15, 101, 101, 99.9, 100))
    assert len(fills) == 1 and fills[0].price == 100 and fills[0].liquidity == "maker"
    assert b.cash == pytest.approx(1000 - 100 - 0.1)
    assert b.positions["X_USD"].qty == 1


def test_entry_attaches_stop_that_can_trigger_in_same_bar():
    b = PaperBroker(FEES, 1000)
    b.place(buy_limit(100, stop_loss_pct=0.02))
    fills = b.on_bar("X_USD", bar(0, 100.5, 101, 97, 98))  # dips through entry and stop
    assert [f.tag for f in fills] == ["entry", "stop"]
    assert fills[1].price == pytest.approx(98.0)  # min(open, stop) = stop
    assert "X_USD" not in b.positions
    assert b.round_trips[0].exit_reason == "stop"
    assert b.round_trips[0].pnl == pytest.approx(-2 - 0.1 - 98 * 0.002)


def test_stop_gap_fills_at_open_and_beats_take_profit():
    b = PaperBroker(FEES, 1000)
    b.place(buy_limit(100, stop_loss_pct=0.02))
    b.on_bar("X_USD", bar(0, 101, 101, 99.5, 100.5))
    b.place(Order("X_USD", Side.SELL, OrderType.LIMIT, 1, 5, price=103, tag="exit:tp"))
    fills = b.on_bar("X_USD", bar(5, 95, 104, 94, 103))  # gaps below stop, then rallies
    assert [f.tag for f in fills] == ["stop"]
    assert fills[0].price == 95
    assert b.open_orders() == []  # take-profit canceled (OCO)


def test_market_orders_use_quotes_and_taker_fee():
    b = PaperBroker(FeeParams(maker=0.001, taker=0.002, slippage=0.001, half_spread=0.0), 1000)
    b.place(Order("X_USD", Side.BUY, OrderType.MARKET, 2, 0))
    (f,) = b.on_bar("X_USD", bar(0, 100, 100, 100, 100), Quote(bid=99.9, ask=100.1))
    assert f.price == pytest.approx(100.1 * 1.001)
    assert f.fee == pytest.approx(2 * f.price * 0.002)
    assert f.liquidity == "taker"


def test_buy_is_clipped_to_cash_and_rounded_to_tick():
    inst = {"X_USD": Instrument("X_USD", "CCY_PAIR", "X", "USD", 0.01, 0.1, True)}
    b = PaperBroker(FEES, 250, inst)
    b.place(buy_limit(100, qty=5.0))
    (f,) = b.on_bar("X_USD", bar(0, 100, 100, 99, 99))
    assert f.qty == pytest.approx(2.4)  # 250 / (100 * 1.001) = 2.497 -> 2.4
    assert b.cash >= 0


def test_expired_orders_are_removed():
    b = PaperBroker(FEES, 1000)
    o = b.place(buy_limit(100, expires_ts=10))
    assert b.on_bar("X_USD", bar(10, 90, 90, 80, 85)) == []
    assert o.status is OrderStatus.EXPIRED and b.open_orders() == []


def test_state_roundtrip():
    b = PaperBroker(FEES, 1000)
    b.place(buy_limit(100, stop_loss_pct=0.02))
    b.on_bar("X_USD", bar(0, 100.5, 101, 99, 100))
    b2 = PaperBroker(FEES, 0)
    b2.load_dict(b.to_dict())
    assert b2.cash == b.cash
    assert b2.positions["X_USD"].qty == 1
    assert [o.type for o in b2.open_orders()] == [OrderType.STOP]
    assert b2.place(buy_limit(1)).id not in {o.id for o in b.open_orders()}
