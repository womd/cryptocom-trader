"""Broker-agnostic order/position types and the Broker interface."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from trader.exchange.models import Candle


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"  # post-only, pays maker fee
    MARKET = "MARKET"  # pays taker fee
    STOP = "STOP"  # stop-market sell (stop-loss), pays taker fee


class OrderStatus(StrEnum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class Order:
    symbol: str
    side: Side
    type: OrderType
    qty: float
    created_ts: int  # eligible to fill from bars with ts >= created_ts
    price: float | None = None  # limit price, or trigger price for STOP
    expires_ts: int | None = None
    tag: str = ""  # "entry" | "take_profit" | "stop" | "exit:<reason>"
    stop_loss_pct: float | None = None  # entries: attach a stop at fill*(1-pct)
    id: str = ""
    status: OrderStatus = OrderStatus.OPEN

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Order:
        d = dict(d)
        d["side"], d["type"], d["status"] = (
            Side(d["side"]),
            OrderType(d["type"]),
            OrderStatus(d["status"]),
        )
        return cls(**d)


@dataclass(slots=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    ts: int
    tag: str
    liquidity: str  # "maker" | "taker"


@dataclass(slots=True)
class Position:
    symbol: str
    qty: float
    avg_price: float
    entry_ts: int
    fees: float = 0.0  # entry fees, folded into the round-trip P&L on exit

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class RoundTrip:
    symbol: str
    entry_ts: int
    exit_ts: int
    qty: float
    entry_price: float
    exit_price: float
    fees: float
    pnl: float  # net of all fees
    exit_reason: str

    @property
    def return_pct(self) -> float:
        cost = self.entry_price * self.qty
        return self.pnl / cost if cost else 0.0


@dataclass(slots=True)
class Quote:
    bid: float | None = None
    ask: float | None = None


class Broker(Protocol):
    positions: dict[str, Position]
    cash: float
    round_trips: list[RoundTrip]

    def place(self, order: Order) -> Order: ...
    def cancel(self, order_id: str) -> None: ...
    def open_orders(self, symbol: str | None = None) -> list[Order]: ...
    def on_bar(self, symbol: str, bar: Candle, quote: Quote | None = None) -> list[Fill]: ...
    def mark(self, symbol: str, price: float) -> None: ...
    def equity(self) -> float: ...
    def available_cash(self) -> float: ...
