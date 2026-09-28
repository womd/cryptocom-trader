"""Paper broker: simulated fills against bars (backtests) or ticker updates (live paper).

Fill model, deliberately on the pessimistic side:
- An order can only fill on bars with ts >= its created_ts (no same-bar look-ahead).
- Within a bar the phases run in this order: market orders (at the open), buy limits,
  stops, then sell limits. So a stop beats a take-profit in the same bar, and a
  fresh entry can be stopped out in the bar it filled in.
- LIMIT (post-only) fills only if price trades *through* it (low < buy / high > sell), at
  the limit price, maker fee.
- MARKET fills at ask/bid (or open ± half_spread without quotes) ± slippage, taker fee.
- STOP triggers at low <= stop, fills at min(open, stop) (gaps hurt) - slippage, taker fee.
- Long-only spot: sells are clipped to the position; when a position goes flat, its
  remaining orders are canceled (OCO).
Partial fills are not modeled.
"""

from __future__ import annotations

import itertools
import math

from trader.config import FeeParams
from trader.exchange.models import Candle, Instrument

from .base import (
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    RoundTrip,
    Side,
)


def round_down(qty: float, tick: float) -> float:
    if tick <= 0:
        return qty
    return math.floor(qty / tick + 1e-9) * tick


class PaperBroker:
    def __init__(
        self,
        fees: FeeParams,
        cash: float,
        instruments: dict[str, Instrument] | None = None,
    ):
        self.fees = fees
        self.cash = cash
        self.instruments = instruments or {}
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self.round_trips: list[RoundTrip] = []
        self.marks: dict[str, float] = {}
        self.fees_paid = 0.0
        self._ids = itertools.count(1)

    # ---- order management --------------------------------------------------

    def place(self, order: Order) -> Order:
        if not order.id:
            order.id = f"P{next(self._ids)}"
        if order.side is Side.BUY:
            order.qty = round_down(order.qty, self._qty_tick(order.symbol))
        if order.qty <= 0:
            order.status = OrderStatus.REJECTED
            return order
        self.orders[order.id] = order
        return order

    def cancel(self, order_id: str) -> None:
        order = self.orders.pop(order_id, None)
        if order:
            order.status = OrderStatus.CANCELED

    def cancel_symbol(self, symbol: str) -> None:
        for o in self.open_orders(symbol):
            self.cancel(o.id)

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self.orders.values() if symbol is None or o.symbol == symbol]

    # ---- accounting --------------------------------------------------------

    def mark(self, symbol: str, price: float) -> None:
        self.marks[symbol] = price

    def equity(self) -> float:
        value = self.cash
        for sym, pos in self.positions.items():
            value += pos.qty * self.marks.get(sym, pos.avg_price)
        return value

    def available_cash(self) -> float:
        """Cash not reserved by open buy orders."""
        reserved = sum(
            o.qty * (o.price or self.marks.get(o.symbol, 0.0)) * (1 + self.fees.taker)
            for o in self.orders.values()
            if o.side is Side.BUY
        )
        return self.cash - reserved

    def _qty_tick(self, symbol: str) -> float:
        inst = self.instruments.get(symbol)
        return inst.qty_tick if inst else 0.0

    # ---- matching ----------------------------------------------------------

    def on_bar(self, symbol: str, bar: Candle, quote: Quote | None = None) -> list[Fill]:
        self.mark(symbol, bar.close)
        fills: list[Fill] = []
        eligible = [o for o in self.open_orders(symbol) if o.created_ts <= bar.ts]
        for o in eligible:
            if o.expires_ts is not None and bar.ts >= o.expires_ts:
                self.orders.pop(o.id, None)
                o.status = OrderStatus.EXPIRED

        def phase(pred):
            for o in [o for o in self.open_orders(symbol) if o.created_ts <= bar.ts and pred(o)]:
                if o.id in self.orders:  # may have been OCO-canceled by an earlier fill
                    fill = self._try_fill(o, bar, quote)
                    if fill:
                        fills.append(fill)

        phase(lambda o: o.type is OrderType.MARKET)
        phase(lambda o: o.type is OrderType.LIMIT and o.side is Side.BUY)
        phase(lambda o: o.type is OrderType.STOP)
        phase(lambda o: o.type is OrderType.LIMIT and o.side is Side.SELL)
        return fills

    def _try_fill(self, o: Order, bar: Candle, quote: Quote | None) -> Fill | None:
        f = self.fees
        if o.type is OrderType.MARKET:
            if o.side is Side.BUY:
                base = quote.ask if quote and quote.ask else bar.open * (1 + f.half_spread)
                price = base * (1 + f.slippage)
            else:
                base = quote.bid if quote and quote.bid else bar.open * (1 - f.half_spread)
                price = base * (1 - f.slippage)
            return self._execute(o, price, bar.ts, f.taker, "taker")

        if o.type is OrderType.STOP:
            assert o.price is not None
            if bar.low > o.price:
                return None
            base = min(bar.open, o.price)
            if quote and quote.bid:
                base = min(base, quote.bid)
            return self._execute(o, base * (1 - f.slippage), bar.ts, f.taker, "taker")

        assert o.price is not None
        if o.side is Side.BUY and bar.low < o.price:
            return self._execute(o, o.price, bar.ts, f.maker, "maker")
        if o.side is Side.SELL and bar.high > o.price:
            return self._execute(o, o.price, bar.ts, f.maker, "maker")
        return None

    def _execute(self, o: Order, price: float, ts: int, fee_rate: float, liq: str) -> Fill | None:
        qty = o.qty
        if o.side is Side.BUY:
            affordable = round_down(self.cash / (price * (1 + fee_rate)), self._qty_tick(o.symbol))
            qty = min(qty, affordable)
        else:
            pos = self.positions.get(o.symbol)
            qty = min(qty, pos.qty if pos else 0.0)
        self.orders.pop(o.id, None)
        if qty <= 0:
            o.status = OrderStatus.REJECTED
            return None

        o.status = OrderStatus.FILLED
        notional = qty * price
        fee = notional * fee_rate
        self.fees_paid += fee
        fill = Fill(o.id, o.symbol, o.side, qty, price, fee, ts, o.tag, liq)

        if o.side is Side.BUY:
            self.cash -= notional + fee
            pos = self.positions.get(o.symbol)
            if pos:
                total = pos.qty + qty
                pos.avg_price = (pos.avg_price * pos.qty + price * qty) / total
                pos.qty = total
                pos.fees += fee
            else:
                self.positions[o.symbol] = Position(o.symbol, qty, price, ts, fee)
            if o.stop_loss_pct:
                self.place(
                    Order(
                        symbol=o.symbol,
                        side=Side.SELL,
                        type=OrderType.STOP,
                        qty=self.positions[o.symbol].qty,
                        created_ts=ts,
                        price=price * (1 - o.stop_loss_pct),
                        tag="stop",
                    )
                )
        else:
            self.cash += notional - fee
            pos = self.positions[o.symbol]
            share = qty / pos.qty
            entry_fees = pos.fees * share
            pnl = (price - pos.avg_price) * qty - fee - entry_fees
            self.round_trips.append(
                RoundTrip(
                    o.symbol,
                    pos.entry_ts,
                    ts,
                    qty,
                    pos.avg_price,
                    price,
                    fee + entry_fees,
                    pnl,
                    o.tag,
                )
            )
            pos.qty -= qty
            pos.fees -= entry_fees
            if pos.qty <= 1e-12:
                del self.positions[o.symbol]
                self.cancel_symbol(o.symbol)
            else:
                for other in self.open_orders(o.symbol):
                    if other.side is Side.SELL:
                        other.qty = min(other.qty, pos.qty)
        return fill

    # ---- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "fees_paid": self.fees_paid,
            "next_id": next(self._ids),
            "positions": [p.to_dict() for p in self.positions.values()],
            "orders": [o.to_dict() for o in self.orders.values()],
            "marks": self.marks,
        }

    def load_dict(self, d: dict) -> None:
        self.cash = d["cash"]
        self.fees_paid = d.get("fees_paid", 0.0)
        self._ids = itertools.count(d.get("next_id", 1))
        self.positions = {p["symbol"]: Position(**p) for p in d.get("positions", [])}
        self.orders = {o["id"]: Order.from_dict(o) for o in d.get("orders", [])}
        self.marks = dict(d.get("marks", {}))
