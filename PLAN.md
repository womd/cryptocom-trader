# cryptocom-trader — Plan

Automated intraday trading on the Crypto.com Exchange API. The strategy buys near the
bottom of the rolling 24h range and sells near the top, filtered by trend. News is
collected alongside prices. Phase 1 is a service we run and iterate on ourselves;
offering it to other users comes later.

## Decisions so far

| Topic | Decision |
|---|---|
| Stack | Python 3.11+, asyncio, `websockets`, `httpx`, pandas/numpy, pydantic, typer |
| Execution | **Paper trading first**: live market data, simulated fills with fees and spread. Live orders come later behind a kill switch |
| Universe | Liquid crypto pairs now. Instruments are abstracted so tokenized stocks (TSLA, NVDA, …) plug in once they're reachable via the API |
| "Day" window | Rolling 24h high/low (matches the ticker's `h`/`l` fields) |
| News | **Log only** in v1: collect, tag by symbol, store. Test later whether it would have helped |
| Evaluation | Backtester that replays historical candles through the *same* strategy code as live |
| Interface | CLI + structured logs + SQLite |
| Risk defaults | ≤10% equity per position, ≤3 open positions, 1.5% stop, halt the day at −3% drawdown, long-only |

## Open risk: tokenized stocks and the API

Crypto.com launched tokenized stocks on 2026-08-12. They are derivatives issued by
Foris Capital, sold in the **App**, for the EEA and approved jurisdictions. It is
unconfirmed whether they are listed on the Exchange REST/WS API. To check:

```
curl -s https://api.crypto.com/exchange/v1/public/get-instruments | grep -iE 'TSLA|NVDA|AAPL'
```

If they are missing, we keep trading crypto and look for an API venue for equities
later. The `Instrument` abstraction plus the `Broker` interface keep that change
contained.

## Architecture

```
src/trader/
  config.py            # pydantic-settings; secrets from .env (never committed)
  exchange/
    auth.py            # HMAC-SHA256 request signing (public/auth, private/*)
    rest.py            # get-instruments, get-candlestick (history backfill)
    ws.py              # market + user streams: heartbeat reply, reconnect, resubscribe
    models.py          # Instrument, Ticker, Candle, Trade, OrderBook
  market/
    state.py           # per-symbol rolling 24h H/L, candles, indicators
  strategy/
    base.py            # Strategy interface: on_candle / on_ticker -> Signal
    range_trend.py     # v1 strategy (below)
  risk/manager.py      # sizing, exposure caps, stops, daily drawdown halt
  execution/
    base.py            # Broker interface: place / cancel / positions / balances
    paper.py           # simulated fills (maker/taker fees, spread, partial fills)
    live.py            # later: private/create-order + user WS order updates
  news/
    sources/           # RSS feeds, Finnhub/NewsAPI (optional keys)
    collector.py       # poll, dedupe, symbol-tag, store
  storage/db.py        # SQLite: candles, ticks, signals, orders, fills, news
  backtest/
    engine.py          # replays candles through strategy -> risk -> paper broker
    metrics.py         # return, max DD, Sharpe, win rate, exposure, vs buy-and-hold
  cli.py               # trader backfill | backtest | paper | record | news | status
tests/
```

Data flow (live and backtest share everything right of the source):

```
WS ticker/candles ─┐
                   ├─> MarketState ─> Strategy ─> RiskManager ─> Broker (paper|live)
historical candles ┘                                                  │
                                                                SQLite + logs
News sources ─> NewsCollector ─> SQLite (not wired into decisions in v1)
```

### Exchange API notes (Exchange API v1, verify against docs while implementing)

- Market WS `wss://stream.crypto.com/exchange/v1/market`, user WS `…/v1/user`.
  UAT sandbox hosts exist for testing the plumbing.
- Wait about 1s after connecting before sending requests. Answer every
  `public/heartbeat` with `public/respond-heartbeat` (same `id`), or the server disconnects.
- Channels: `ticker.{inst}`, `candlestick.{interval}.{inst}`, `trade.{inst}`, `book.{inst}.{depth}`.
- History: REST `public/get-candlestick`, paginated by `start_ts`/`end_ts`.
- Auth: HMAC-SHA256 over `method + id + api_key + sorted_params + nonce`.
- API key: **trade + read only, no withdrawal permission, IP-whitelisted**.

## Strategy v1: `range_trend`

Per symbol, on each closed 5m candle (the ticker is used for live price and spread):

- `H, L` = rolling 24h high and low. `R = H − L`. `p = (price − L) / R` (position in range).
- **Tradeable range filter**: `R / price ≥ min_range` (default 1.5%). The range must
  clearly cover round-trip fees plus spread, or we skip.
- **Trend** from 15m EMA(50) vs EMA(200), plus the slope of EMA(50): `up | flat | down`.
- **Entry (long)**: `p ≤ 0.15`, trend is not `down`, and a reversal is confirmed
  (the 5m close is above the previous candle's high, or RSI(14) crosses back above 30).
  Uses a post-only limit at the bid to pay maker fees.
- **Exit**, whichever comes first: `p ≥ 0.85` (in an `up` trend a trailing stop can let it run),
  the 1.5% stop-loss, trend flips to `down`, or a 24h time stop.
- Every threshold goes in config so the backtester can sweep them later.

Baselines to beat in backtests: buy-and-hold, and the same rules without the trend filter.

## Phases

0. **Verify** *(you, with the tools from phase 1)*: `trader instruments "TSLA|NVDA"` for
   stocks, your fee tier (put it in `config.toml`), `trader account` for API key permissions.
   Pick starter pairs.
1. **Foundation** ✅: config/.env, REST client + candle backfill into SQLite, market WS
   client (heartbeat, reconnect, resubscribe, idle timeout), `trader record` for tick recording.
2. **Strategy + backtester** ✅: `MarketState`, `range_trend`, fee/slippage model, metrics
   report. `trader backtest --symbols BTC_USD,ETH_USD --days 90 --compare`.
   Still to do: parameter sweeps with walk-forward splits.
3. **Paper trading** ✅: live loop over WS → strategy → risk → paper broker. Positions and P&L
   persist across restarts. `trader paper`, `trader status`.
   Not yet exercised against the real exchange: the build sandbox can't reach api.crypto.com.
4. **News logger**: RSS + optional Finnhub, symbol tagging, dedupe, stored next to prices.
   Later: event studies to see whether news predicts moves in our window.
5. **Live execution**: signed private API, user WS order and balance updates, order
   reconciliation on restart, kill switch, hard caps. Tiny size first.
6. **Multi-user (later)**: encrypted per-user key storage, per-user config and isolation,
   web UI. **Check the legal side before offering this to others**: automated trading
   for third parties can fall under MiCA / investment-service licensing.

## Still to decide (not blocking phase 1)

- Starter pairs (proposal: `BTC_USD`, `ETH_USD`, `SOL_USD`, plus 1–2 liquid alts).
- News sources and whether to get a free Finnhub key.
- Where it runs long-term (local, Docker on a VPS, …).
