"""Command-line interface: `trader --help`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from trader.config import Settings, load_settings
from trader.exchange.models import Instrument
from trader.exchange.rest import RestClient
from trader.market.backfill import backfill, now_ms
from trader.market.state import warmup_ms
from trader.storage.db import Database

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
log = logging.getLogger("trader")

DAY_MS = 86_400_000

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="TOML config file")]
SymbolsOpt = Annotated[
    str | None, typer.Option("--symbols", "-s", help="Comma-separated, e.g. BTC_USD,ETH_USD")
]
SetOpt = Annotated[
    list[str] | None,
    typer.Option("--set", help="Override a setting, e.g. --set strategy.entry_zone=0.1"),
]


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _settings(config: Path | None, symbols: str | None, sets: list[str] | None) -> Settings:
    syms = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    return load_settings(config, sets=sets, symbols=syms)


def _rest(settings: Settings) -> RestClient:
    return RestClient(
        settings.rest_url,
        settings.api_key.get_secret_value() if settings.api_key else None,
        settings.api_secret.get_secret_value() if settings.api_secret else None,
    )


async def _instruments(rest: RestClient | None, db: Database, symbols: list[str]):
    """Instrument metadata from the API (cached in the DB); fallback when offline."""
    if rest is not None:
        try:
            all_inst = await rest.get_instruments()
            db.save_instruments(all_inst)
            by_symbol = {i.symbol: i for i in all_inst}
            missing = [s for s in symbols if s not in by_symbol]
            if missing:
                raise typer.BadParameter(
                    f"unknown instruments: {missing} (see `trader instruments`)"
                )
            return {s: by_symbol[s] for s in symbols}
        except typer.BadParameter:
            raise
        except Exception as exc:  # network issues -> use cache
            log.warning("could not fetch instruments (%s); using cached/fallback metadata", exc)
    return {s: db.load_instrument(s) or Instrument.fallback(s) for s in symbols}


def _parse_date(value: str | None) -> int:
    if not value:
        return now_ms()
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")


# ---- commands ---------------------------------------------------------------


@app.command()
def instruments(
    grep: Annotated[str | None, typer.Argument(help="Filter, e.g. TSLA or NVDA|AAPL")] = None,
    inst_type: Annotated[str | None, typer.Option("--type", help="e.g. CCY_PAIR")] = None,
    config: ConfigOpt = None,
) -> None:
    """List exchange instruments (use this to check for tokenized stocks)."""
    settings = load_settings(config)

    async def go():
        async with _rest(settings) as rest:
            return await rest.get_instruments()

    items = asyncio.run(go())
    terms = [t.upper() for t in grep.split("|")] if grep else []
    shown = 0
    for i in sorted(items, key=lambda x: (x.inst_type, x.symbol)):
        if inst_type and i.inst_type != inst_type:
            continue
        haystack = f"{i.symbol} {i.display_name} {i.base}".upper()
        if terms and not any(t in haystack for t in terms):
            continue
        shown += 1
        typer.echo(
            f"{i.symbol:<24} {i.inst_type:<16} tradable={i.tradable!s:<5} "
            f"tick={i.price_tick:g} qty_tick={i.qty_tick:g} {i.display_name}"
        )
    typer.echo(f"{shown} of {len(items)} instruments")


@app.command("backfill")
def backfill_cmd(
    days: Annotated[float, typer.Option(help="History to fetch")] = 90,
    interval: Annotated[str | None, typer.Option(help="Default: strategy interval")] = None,
    force: Annotated[bool, typer.Option(help="Delete stored candles and refetch")] = False,
    symbols: SymbolsOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Download historical candles into the local database."""
    settings = _settings(config, symbols, None)
    interval = interval or settings.strategy.interval
    db = Database(settings.db_path)

    async def go():
        end = now_ms()
        start = end - int(days * DAY_MS)
        async with _rest(settings) as rest:
            for sym in settings.symbols:
                if force:
                    db.delete_candles(sym, interval)
                n = await backfill(rest, db, sym, interval, start, end)
                lo, hi = db.candle_bounds(sym, interval)
                span = f"{_fmt_ts(lo)} -> {_fmt_ts(hi)}" if lo is not None else "empty"
                typer.echo(f"{sym} {interval}: +{n} candles, stored {span}")

    asyncio.run(go())


@app.command()
def backtest(
    days: Annotated[float, typer.Option(help="Test period length")] = 90,
    end: Annotated[
        str | None, typer.Option(help="Period end (ISO date, UTC). Default: now")
    ] = None,
    compare: Annotated[bool, typer.Option(help="Also run without the trend filter")] = False,
    offline: Annotated[bool, typer.Option(help="Use only candles already in the DB")] = False,
    trades_csv: Annotated[Path | None, typer.Option(help="Write round trips to CSV")] = None,
    symbols: SymbolsOpt = None,
    sets: SetOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Replay history through the strategy with simulated fills and fees."""
    from trader.backtest.engine import run_backtest
    from trader.backtest.metrics import format_report

    settings = _settings(config, symbols, sets)
    db = Database(settings.db_path)
    interval = settings.strategy.interval
    end_ts = _parse_date(end)
    start_ts = end_ts - int(days * DAY_MS)
    data_start = start_ts - warmup_ms(settings.strategy)

    async def load():
        if offline:
            return await _instruments(None, db, settings.symbols)
        async with _rest(settings) as rest:
            inst = await _instruments(rest, db, settings.symbols)
            for sym in settings.symbols:
                await backfill(rest, db, sym, interval, data_start, end_ts)
            return inst

    inst = asyncio.run(load())
    candles = {s: list(db.iter_candles(s, interval, data_start, end_ts)) for s in settings.symbols}
    for sym, series in candles.items():
        if not series:
            raise typer.BadParameter(f"no candles for {sym}; run `trader backfill` first")
        if series[0].ts > start_ts:
            typer.echo(f"warning: {sym} history starts {_fmt_ts(series[0].ts)}, after warm-up")

    result = run_backtest(settings, candles, start_ts, end_ts, inst)
    typer.echo(format_report(result, f"range_trend {','.join(settings.symbols)}"))
    if compare:
        baseline = settings.model_copy(
            update={"strategy": settings.strategy.model_copy(update={"trend_filter": False})}
        )
        typer.echo("")
        typer.echo(
            format_report(
                run_backtest(baseline, candles, start_ts, end_ts, inst), "baseline: no trend filter"
            )
        )
    if trades_csv:
        import csv

        with trades_csv.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                [
                    "symbol",
                    "entry_time",
                    "exit_time",
                    "qty",
                    "entry_price",
                    "exit_price",
                    "fees",
                    "pnl",
                    "return_pct",
                    "exit_reason",
                ]
            )
            for t in result.round_trips:
                w.writerow(
                    [
                        t.symbol,
                        _fmt_ts(t.entry_ts),
                        _fmt_ts(t.exit_ts),
                        t.qty,
                        t.entry_price,
                        t.exit_price,
                        round(t.fees, 6),
                        round(t.pnl, 6),
                        round(t.return_pct, 6),
                        t.exit_reason,
                    ]
                )
        typer.echo(f"wrote {len(result.round_trips)} round trips to {trades_csv}")


@app.command()
def paper(
    run: Annotated[str, typer.Option(help="Name of the paper account/run")] = "paper",
    reset: Annotated[bool, typer.Option(help="Discard saved state and start fresh")] = False,
    symbols: SymbolsOpt = None,
    sets: SetOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Run live paper trading (real-time data, simulated fills). Ctrl+C to stop."""
    from trader.live.paper import PaperRunner

    settings = _settings(config, symbols, sets)
    db = Database(settings.db_path)

    async def go():
        async with _rest(settings) as rest:
            inst = await _instruments(rest, db, settings.symbols)
            runner = PaperRunner(settings, db, rest, inst, run=run, reset=reset)
            await runner.run_forever()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(go())
    typer.echo("stopped; state saved")


@app.command("record")
def record_cmd(
    trades: Annotated[bool, typer.Option(help="Also record the public trade stream")] = True,
    symbols: SymbolsOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Record live tickers (and trades) to the database. Ctrl+C to stop."""
    from trader.live.recorder import record

    settings = _settings(config, symbols, None)
    db = Database(settings.db_path)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(record(settings.market_ws_url, settings.symbols, db, trades))


@app.command()
def status(
    run: Annotated[str, typer.Option(help="Paper run name")] = "paper",
    events: Annotated[int, typer.Option(help="Recent journal events to show")] = 15,
    config: ConfigOpt = None,
) -> None:
    """Show a paper run's balances, positions, working orders and recent events."""
    settings = load_settings(config)
    db = Database(settings.db_path)
    saved = db.load_state(f"paper:{run}")
    if not saved:
        typer.echo(f"no saved state for run {run!r}")
        raise typer.Exit(1)
    b = saved["engine"]["broker"]
    marks = b.get("marks", {})
    equity = b["cash"] + sum(
        p["qty"] * marks.get(p["symbol"], p["avg_price"]) for p in b["positions"]
    )
    typer.echo(f"run {run}  saved {_fmt_ts(saved['saved_ms'])} UTC")
    typer.echo(
        f"equity {equity:.2f}  cash {b['cash']:.2f}  fees paid {b.get('fees_paid', 0):.2f}"
        f"  halted={saved['engine'].get('risk', {}).get('halted', False)}"
    )
    for p in b["positions"]:
        mark = marks.get(p["symbol"], p["avg_price"])
        typer.echo(
            f"  POS {p['symbol']:<12} qty={p['qty']:g} avg={p['avg_price']:g} "
            f"mark={mark:g} upnl={(mark - p['avg_price']) * p['qty']:+.2f} "
            f"since {_fmt_ts(p['entry_ts'])}"
        )
    for o in b["orders"]:
        typer.echo(
            f"  ORD {o['symbol']:<12} {o['side']} {o['type']} qty={o['qty']:g} "
            f"price={o['price']} tag={o['tag']}"
        )
    for ts, kind, sym, data in db.recent_journal(run, events):
        typer.echo(f"  {_fmt_ts(ts)} {kind:<10} {sym or '-':<10} {json.loads(data)}")


@app.command()
def account(config: ConfigOpt = None) -> None:
    """Verify API credentials by fetching balances (read-only)."""
    settings = load_settings(config)

    async def go():
        async with _rest(settings) as rest:
            return await rest.get_user_balance()

    result = asyncio.run(go())
    for acct in result.get("data", []):
        typer.echo(
            f"total available: {acct.get('total_available_balance')} "
            f"(margin {acct.get('total_margin_balance')})"
        )
        for pos in acct.get("position_balances", []):
            typer.echo(
                f"  {pos.get('instrument_name'):<8} qty={pos.get('quantity')} "
                f"value={pos.get('market_value')}"
            )


if __name__ == "__main__":
    app()
