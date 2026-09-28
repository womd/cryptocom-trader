"""Trading engine: MarketState -> Strategy -> RiskManager -> Broker.

The same engine runs backtests and live paper trading; only the event source differs:
  on_bar(symbol, bar, quote)  -- price update; lets the broker match working orders
  on_candle(symbol, candle)   -- a *closed* candle; updates state and asks the strategy
Backtests call on_bar(c) then on_candle(c) for each historical candle. Live paper feeds
ticker updates as degenerate bars into on_bar and closed WS candles into on_candle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from trader.config import Settings
from trader.exchange.models import Candle, interval_ms
from trader.execution.base import Fill, Order, OrderType, Quote, Side
from trader.market.state import MarketState
from trader.risk.manager import RiskManager
from trader.strategy.base import Action, Signal, Strategy, SymbolContext

log = logging.getLogger(__name__)

EventSink = Callable[[str, int, str | None, dict], None]  # (kind, ts, symbol, data)


class Engine:
    def __init__(
        self,
        settings: Settings,
        strategy: Strategy,
        risk: RiskManager,
        broker,
        on_event: EventSink | None = None,
    ):
        self.settings = settings
        self.params = settings.strategy
        self.strategy = strategy
        self.risk = risk
        self.broker = broker
        self.bar_ms = interval_ms(self.params.interval)
        self.states = {s: MarketState(s, self.params) for s in settings.symbols}
        self.trading_start_ts = 0  # candles closing before this only warm up state
        self._emit = on_event or (lambda *a: None)

    # ---- events ------------------------------------------------------------

    def on_bar(self, symbol: str, bar: Candle, quote: Quote | None = None) -> list[Fill]:
        fills = self.broker.on_bar(symbol, bar, quote)
        for f in fills:
            self._emit(
                "fill",
                f.ts,
                f.symbol,
                {
                    "side": f.side,
                    "qty": f.qty,
                    "price": f.price,
                    "fee": f.fee,
                    "tag": f.tag,
                    "liquidity": f.liquidity,
                },
            )
            if f.side is Side.SELL and f.symbol not in self.broker.positions:
                self.strategy.on_flat(f.symbol, f.ts)
                rt = self.broker.round_trips[-1]
                self._emit(
                    "round_trip",
                    f.ts,
                    f.symbol,
                    {
                        "pnl": rt.pnl,
                        "return_pct": rt.return_pct,
                        "reason": rt.exit_reason,
                        "entry_price": rt.entry_price,
                        "exit_price": rt.exit_price,
                    },
                )
        return fills

    def on_candle(self, symbol: str, candle: Candle) -> Signal | None:
        state = self.states[symbol]
        state.update(candle)
        self.broker.mark(symbol, candle.close)
        decision_ts = candle.ts + self.bar_ms
        if decision_ts <= self.trading_start_ts:
            return None

        if self.risk.on_equity(decision_ts, self.broker.equity()):
            self._emit("halt", decision_ts, None, {"equity": self.broker.equity()})

        ctx = self._context(symbol, decision_ts)
        signal = self.strategy.evaluate(state, ctx)
        if signal is not None:
            self._act(signal, ctx, decision_ts)
        return signal

    # ---- internals ---------------------------------------------------------

    def _context(self, symbol: str, ts: int) -> SymbolContext:
        orders = self.broker.open_orders(symbol)
        exits = {o.type for o in orders if o.side is Side.SELL and o.type is not OrderType.STOP}
        pending_exit = OrderType.MARKET if OrderType.MARKET in exits else next(iter(exits), None)
        return SymbolContext(
            ts=ts,
            position=self.broker.positions.get(symbol),
            pending_entry=any(o.side is Side.BUY for o in orders),
            pending_exit=pending_exit,
        )

    def _act(self, sig: Signal, ctx: SymbolContext, ts: int) -> None:
        sym = sig.symbol
        timeout = self.params.entry_timeout_bars * self.bar_ms
        if sig.action is Action.ENTER:
            assert sig.price is not None
            qty, reason = self.risk.size_entry(sym, sig.price, self.broker)
            if not qty:
                self._emit("reject", ts, sym, {"reason": reason, "signal": sig.reason})
                return
            order = self.broker.place(
                Order(
                    symbol=sym,
                    side=Side.BUY,
                    type=sig.order_type,
                    qty=qty,
                    created_ts=ts,
                    price=sig.price,
                    expires_ts=ts + timeout,
                    tag="entry",
                    stop_loss_pct=self.settings.risk.stop_loss_pct,
                )
            )
        else:
            pos = ctx.position
            assert pos is not None
            # a new exit supersedes any working take-profit; the stop stays until flat
            for o in self.broker.open_orders(sym):
                if o.side is Side.SELL and o.type is not OrderType.STOP:
                    self.broker.cancel(o.id)
            order = self.broker.place(
                Order(
                    symbol=sym,
                    side=Side.SELL,
                    type=sig.order_type,
                    qty=pos.qty,
                    created_ts=ts,
                    price=sig.price,
                    expires_ts=ts + timeout if sig.order_type is OrderType.LIMIT else None,
                    tag=f"exit:{sig.reason}",
                )
            )
        self._emit(
            "signal",
            ts,
            sym,
            {
                "action": sig.action,
                "type": sig.order_type,
                "reason": sig.reason,
                "price": sig.price,
                "qty": order.qty,
                "order_id": order.id,
                "status": order.status,
                **sig.info,
            },
        )

    # ---- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "broker": self.broker.to_dict(),
            "risk": self.risk.to_dict(),
            "strategy": self.strategy.to_dict(),
        }

    def load_dict(self, d: dict) -> None:
        self.broker.load_dict(d["broker"])
        self.risk.load_dict(d.get("risk", {}))
        self.strategy.load_dict(d.get("strategy", {}))
