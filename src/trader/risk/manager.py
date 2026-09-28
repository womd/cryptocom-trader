"""Risk manager: position sizing, exposure caps, and the daily drawdown halt."""

from __future__ import annotations

import logging

from trader.config import FeeParams, RiskParams
from trader.execution.base import Side
from trader.execution.paper import round_down

log = logging.getLogger(__name__)

DAY_MS = 86_400_000


class RiskManager:
    def __init__(
        self, params: RiskParams, fees: FeeParams, qty_ticks: dict[str, float] | None = None
    ):
        self.p = params
        self.fees = fees
        self.qty_ticks = qty_ticks or {}
        self.day: int | None = None
        self.day_start_equity: float | None = None
        self.halted = False

    def on_equity(self, ts: int, equity: float) -> bool:
        """Update the daily drawdown tracker. Returns True when a halt was just triggered."""
        day = ts // DAY_MS
        if day != self.day:
            self.day, self.day_start_equity, self.halted = day, equity, False
        assert self.day_start_equity is not None
        if not self.halted and equity <= self.day_start_equity * (1 - self.p.daily_loss_halt_pct):
            self.halted = True
            log.warning(
                "daily loss halt: equity %.2f <= %.2f (day start) * (1 - %.3f)",
                equity,
                self.day_start_equity,
                self.p.daily_loss_halt_pct,
            )
            return True
        return False

    def size_entry(self, symbol: str, price: float, broker) -> tuple[float, str]:
        """Return (qty, "") for an allowed entry, or (0, reason) when rejected."""
        if self.halted:
            return 0.0, "daily_loss_halt"
        if symbol in broker.positions:
            return 0.0, "already_in_position"
        busy = set(broker.positions) | {
            o.symbol for o in broker.open_orders() if o.side is Side.BUY
        }
        if symbol in busy:
            return 0.0, "entry_pending"
        if len(busy) >= self.p.max_open_positions:
            return 0.0, "max_open_positions"

        budget = broker.equity() * self.p.max_position_pct
        budget = min(budget, broker.available_cash() / (1 + self.fees.taker))
        qty = round_down(budget / price, self.qty_ticks.get(symbol, 0.0))
        if qty * price < self.p.min_notional:
            return 0.0, "below_min_notional"
        return qty, ""

    def to_dict(self) -> dict:
        return {"day": self.day, "day_start_equity": self.day_start_equity, "halted": self.halted}

    def load_dict(self, d: dict) -> None:
        self.day = d.get("day")
        self.day_start_equity = d.get("day_start_equity")
        self.halted = d.get("halted", False)
