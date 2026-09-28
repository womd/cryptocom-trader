"""Strategy interface. Strategies are pure decision logic: no I/O, no order handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from trader.execution.base import OrderType, Position
from trader.market.state import MarketState


class Action(StrEnum):
    ENTER = "enter"
    EXIT = "exit"


@dataclass(slots=True)
class Signal:
    symbol: str
    action: Action
    order_type: OrderType
    reason: str
    price: float | None = None  # limit price (LIMIT orders)
    info: dict = field(default_factory=dict)


@dataclass(slots=True)
class SymbolContext:
    """What the engine tells the strategy about our own book for one symbol."""

    ts: int  # decision time = close time of the bar just completed
    position: Position | None
    pending_entry: bool
    pending_exit: OrderType | None  # type of a working (non-stop) exit order, if any


class Strategy(Protocol):
    name: str

    def evaluate(self, state: MarketState, ctx: SymbolContext) -> Signal | None: ...

    def on_flat(self, symbol: str, ts: int) -> None: ...

    def to_dict(self) -> dict: ...

    def load_dict(self, d: dict) -> None: ...
