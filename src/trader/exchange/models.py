"""Exchange data models and parsers for Crypto.com Exchange API v1 payloads.

All timestamps are epoch milliseconds (UTC). Prices/quantities are floats: fine for
signal logic and paper accounting; live order submission formats them via the
instrument's tick sizes.
"""

from __future__ import annotations

from dataclasses import dataclass

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1D": 24 * 60 * 60_000,
}


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(
            f"unsupported interval {interval!r}; use one of {list(INTERVAL_MS)}"
        ) from None


def _f(value, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


@dataclass(frozen=True, slots=True)
class Candle:
    """OHLCV bar. `ts` is the bar's open time; it closes at `ts + interval`."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_api(cls, d: dict) -> Candle:
        return cls(
            ts=int(d["t"]),
            open=_f(d["o"]),
            high=_f(d["h"]),
            low=_f(d["l"]),
            close=_f(d["c"]),
            volume=_f(d.get("v")),
        )


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    ts: int
    last: float
    bid: float
    ask: float
    high_24h: float
    low_24h: float
    volume_24h: float

    @classmethod
    def from_api(cls, d: dict, symbol: str | None = None) -> Ticker:
        last = _f(d.get("a"))
        return cls(
            symbol=symbol or d["i"],
            ts=int(d["t"]),
            last=last,
            bid=_f(d.get("b"), last),
            ask=_f(d.get("k"), last),
            high_24h=_f(d.get("h")),
            low_24h=_f(d.get("l")),
            volume_24h=_f(d.get("v")),
        )


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    ts: int
    price: float
    qty: float
    side: str  # taker side: "BUY" | "SELL"
    trade_id: str

    @classmethod
    def from_api(cls, d: dict, symbol: str | None = None) -> Trade:
        return cls(
            symbol=symbol or d["i"],
            ts=int(d["t"]),
            price=_f(d["p"]),
            qty=_f(d["q"]),
            side=str(d.get("s", "")).upper(),
            trade_id=str(d.get("d", "")),
        )


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    inst_type: str
    base: str
    quote: str
    price_tick: float
    qty_tick: float
    tradable: bool
    display_name: str = ""

    @classmethod
    def from_api(cls, d: dict) -> Instrument:
        return cls(
            symbol=d["symbol"],
            inst_type=d.get("inst_type", ""),
            base=d.get("base_ccy", ""),
            quote=d.get("quote_ccy", ""),
            price_tick=_f(d.get("price_tick_size"), 0.0),
            qty_tick=_f(d.get("qty_tick_size"), 0.0),
            tradable=bool(d.get("tradable", True)),
            display_name=d.get("display_name", ""),
        )

    @classmethod
    def fallback(cls, symbol: str) -> Instrument:
        """Used when instrument metadata is unavailable (e.g. offline backtests)."""
        base, _, quote = symbol.partition("_")
        return cls(symbol, "CCY_PAIR", base, quote, 0.0, 0.0, True, symbol)
