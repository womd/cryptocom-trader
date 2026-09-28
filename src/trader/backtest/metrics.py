"""Performance metrics and a plain-text report for backtest results."""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime

from .engine import BacktestResult

DAY_MS = 86_400_000


def max_drawdown(curve: list[tuple[int, float]]) -> float:
    peak, mdd = -math.inf, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, 1 - eq / peak)
    return mdd


def daily_sharpe(curve: list[tuple[int, float]], initial: float) -> float | None:
    """Annualized (365d) Sharpe of daily close-to-close equity returns, rf = 0."""
    closes: dict[int, float] = {}
    for ts, eq in curve:
        closes[ts // DAY_MS] = eq
    values = [initial] + [closes[d] for d in sorted(closes)]
    rets = [b / a - 1 for a, b in zip(values, values[1:], strict=False) if a > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var == 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(365)


def summarize(r: BacktestResult) -> dict:
    trips = r.round_trips
    wins = [t for t in trips if t.pnl > 0]
    losses = [t for t in trips if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    hold_h = [(t.exit_ts - t.entry_ts) / 3_600_000 for t in trips]
    return {
        "period_days": (r.end_ts - r.start_ts) / DAY_MS,
        "total_return": r.final_equity / r.initial_equity - 1,
        "benchmark_return": r.benchmark_return,
        "max_drawdown": max_drawdown(r.equity_curve),
        "sharpe": daily_sharpe(r.equity_curve, r.initial_equity),
        "trades": len(trips),
        "win_rate": len(wins) / len(trips) if trips else None,
        "avg_win_pct": _mean([t.return_pct for t in wins]),
        "avg_loss_pct": _mean([t.return_pct for t in losses]),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "avg_hold_hours": _mean(hold_h),
        "exposure": r.exposure,
        "fees_paid": r.fees_paid,
        "final_equity": r.final_equity,
        "exit_reasons": Counter(t.exit_reason for t in trips),
        "rejects": r.rejects,
        "open_positions": len(r.open_positions),
    }


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _num(x: float | None, fmt: str = ".2f") -> str:
    return "n/a" if x is None else format(x, fmt)


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def format_report(r: BacktestResult, title: str = "Backtest") -> str:
    s = summarize(r)
    lines = [
        f"== {title}: {_date(r.start_ts)} -> {_date(r.end_ts)} UTC ({s['period_days']:.1f} days)",
        f"  total return      {_pct(s['total_return'])}"
        f"   (buy & hold {_pct(s['benchmark_return'])})",
        f"  max drawdown      {_pct(-s['max_drawdown'])}",
        f"  sharpe (daily)    {_num(s['sharpe'])}",
        f"  trades            {s['trades']}   win rate {_pct(s['win_rate'])}"
        f"   profit factor {_num(s['profit_factor'])}",
        f"  avg win / loss    {_pct(s['avg_win_pct'])} / {_pct(s['avg_loss_pct'])}"
        f"   avg hold {_num(s['avg_hold_hours'], '.1f')}h",
        f"  exposure          {_pct(s['exposure'])}   fees paid {s['fees_paid']:.2f}",
        f"  final equity      {s['final_equity']:.2f}   open positions {s['open_positions']}",
    ]
    if s["exit_reasons"]:
        reasons = ", ".join(f"{k}={v}" for k, v in s["exit_reasons"].most_common())
        lines.append(f"  exits             {reasons}")
    if s["rejects"]:
        rejects = ", ".join(f"{k}={v}" for k, v in s["rejects"].most_common())
        lines.append(f"  rejected entries  {rejects}")
    return "\n".join(lines)
