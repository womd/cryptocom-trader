"""SQLite storage: candles, recorded ticks/trades, instruments, paper-trading state."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from pathlib import Path

from trader.exchange.models import Candle, Instrument, Ticker, Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, interval, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ticks (
    symbol TEXT NOT NULL, ts INTEGER NOT NULL,
    last REAL, bid REAL, ask REAL, high_24h REAL, low_24h REAL, volume_24h REAL
);
CREATE INDEX IF NOT EXISTS ticks_symbol_ts ON ticks(symbol, ts);

CREATE TABLE IF NOT EXISTS trades (
    symbol TEXT NOT NULL, ts INTEGER NOT NULL, trade_id TEXT,
    price REAL, qty REAL, side TEXT,
    PRIMARY KEY (symbol, trade_id)
);

CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY, data TEXT NOT NULL, updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv_state (
    name TEXT PRIMARY KEY, data TEXT NOT NULL, updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run TEXT NOT NULL, ts INTEGER NOT NULL, kind TEXT NOT NULL, symbol TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS journal_run_ts ON journal(run, ts);

CREATE TABLE IF NOT EXISTS equity (
    run TEXT NOT NULL, ts INTEGER NOT NULL, equity REAL, cash REAL,
    PRIMARY KEY (run, ts)
);
"""


class Database:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- candles -----------------------------------------------------------

    def upsert_candles(self, symbol: str, interval: str, candles: Iterable[Candle]) -> int:
        rows = [(symbol, interval, c.ts, c.open, c.high, c.low, c.close, c.volume) for c in candles]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def delete_candles(self, symbol: str, interval: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM candles WHERE symbol=? AND interval=?", (symbol, interval)
            )

    def candle_bounds(self, symbol: str, interval: str) -> tuple[int | None, int | None]:
        row = self.conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM candles WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchone()
        return row[0], row[1]

    def iter_candles(
        self, symbol: str, interval: str, start_ts: int | None = None, end_ts: int | None = None
    ) -> Iterator[Candle]:
        cur = self.conn.execute(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND interval=? AND ts>=? AND ts<? ORDER BY ts",
            (symbol, interval, start_ts or 0, end_ts or 2**62),
        )
        for row in cur:
            yield Candle(*row)

    # ---- recording ---------------------------------------------------------

    def insert_ticks(self, ticks: Iterable[Ticker]) -> None:
        rows = [
            (t.symbol, t.ts, t.last, t.bid, t.ask, t.high_24h, t.low_24h, t.volume_24h)
            for t in ticks
        ]
        with self.conn:
            self.conn.executemany("INSERT INTO ticks VALUES (?,?,?,?,?,?,?,?)", rows)

    def insert_trades(self, trades: Iterable[Trade]) -> None:
        rows = [(t.symbol, t.ts, t.trade_id, t.price, t.qty, t.side) for t in trades]
        with self.conn:
            self.conn.executemany("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?)", rows)

    # ---- instruments -------------------------------------------------------

    def save_instruments(self, instruments: Iterable[Instrument]) -> None:
        now = int(time.time() * 1000)
        rows = [(i.symbol, json.dumps(asdict(i)), now) for i in instruments]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO instruments VALUES (?,?,?)", rows)

    def load_instrument(self, symbol: str) -> Instrument | None:
        row = self.conn.execute("SELECT data FROM instruments WHERE symbol=?", (symbol,)).fetchone()
        return Instrument(**json.loads(row[0])) if row else None

    # ---- state / journal ---------------------------------------------------

    def save_state(self, name: str, data: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO kv_state VALUES (?,?,?)",
                (name, json.dumps(data), int(time.time() * 1000)),
            )

    def load_state(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT data FROM kv_state WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def journal(self, run: str, ts: int, kind: str, symbol: str | None, data: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO journal (run, ts, kind, symbol, data) VALUES (?,?,?,?,?)",
                (run, ts, kind, symbol, json.dumps(data)),
            )

    def recent_journal(self, run: str, limit: int = 20) -> list[tuple]:
        return self.conn.execute(
            "SELECT ts, kind, symbol, data FROM journal WHERE run=? ORDER BY id DESC LIMIT ?",
            (run, limit),
        ).fetchall()

    def record_equity(self, run: str, ts: int, equity: float, cash: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO equity VALUES (?,?,?,?)", (run, ts, equity, cash)
            )
