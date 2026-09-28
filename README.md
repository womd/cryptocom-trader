# cryptocom-trader

Intraday trading on the [Crypto.com Exchange API v1](https://exchange-docs.crypto.com/exchange/v1/rest-ws/index.html).
The strategy buys near the rolling-24h low and sells near the high, filtered by trend.
It currently runs as **backtests and live paper trading**, with no real orders. See
[PLAN.md](PLAN.md) for the design, the decisions so far, and the roadmap.

## Setup

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config.example.toml config.toml   # optional; defaults are used without it
cp .env.example .env                 # optional; only for `trader account`
```

## Usage

```bash
# 0. What can we trade? (checks for tokenized stocks too)
trader instruments "TSLA|NVDA|AAPL"
trader instruments --type CCY_PAIR BTC

# 1. Download history (5m candles) into data/trader.db
trader backfill --days 90 -s BTC_USD,ETH_USD,SOL_USD

# 2. Backtest (fetches any missing history automatically, incl. indicator warm-up)
trader backtest --days 90 --compare                  # --compare: also run without trend filter
trader backtest --days 30 --set strategy.entry_zone=0.1 --set fees.maker=0.001
trader backtest --days 60 --end 2026-08-01 --trades-csv trades.csv

# 3. Live paper trading (real-time WebSocket data, simulated fills). State survives restarts.
trader paper                  # Ctrl+C to stop
trader status                 # balances, positions, working orders, recent events
trader paper --reset          # start the paper account from scratch

# Record live tickers and trades for later analysis
trader record -s BTC_USD,ETH_USD

# Check API credentials (read-only balance call)
trader account
```

Run `pytest` for the tests and `ruff check src tests` to lint.

## How it works

```
WS ticker/candles ─┐
                   ├─> MarketState ─> Strategy ─> RiskManager ─> PaperBroker
historical candles ┘   (24h range,    (range_trend) (sizing, caps,  (fills, fees,
                        EMA trend, RSI)               daily halt)    stops, P&L)
```

- **One engine for backtests and live paper trading** (`trader/core/engine.py`). All
  indicators update incrementally, so a backtest sees exactly what live trading would.
- **The fill model errs on the pessimistic side** (`trader/execution/paper.py`):
  - Nothing fills in the bar that created it.
  - A limit order fills only if the price trades *through* it.
  - If a stop and a take-profit could both fill in the same bar, the stop fills first.
  - Price gaps fill stops at the open.
  - Maker/taker fees and slippage are charged on every fill.
- **Risk** (`trader/risk/manager.py`):
  - Each position is capped as a percentage of equity, with a maximum number of open positions.
  - A 1.5% stop-loss order is attached to every entry.
  - After a 3% loss in a UTC day, no new positions are opened until the next day.
- **Live paper trading** (`trader/live/paper.py`):
  - Warms up the indicators from REST history before trading.
  - Ticker updates drive order matching.
  - Missed candles after a reconnect are fetched but not traded on.
  - State is saved to SQLite.

## Notes

- The default fees (0.25% maker, 0.5% taker) are pessimistic. Set your actual tier in
  `config.toml` or with `--set fees.maker=...`, because fees decide whether this strategy is profitable.
- When you create API keys, give them **read + trade permission only, no withdrawals**, and
  add an IP allowlist.
