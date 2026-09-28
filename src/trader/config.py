"""Settings: non-secret config from a TOML file, secrets from the environment / .env."""

from __future__ import annotations

import json
import tomllib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    PROD = "prod"
    UAT = "uat"


ENDPOINTS = {
    Environment.PROD: {
        "rest": "https://api.crypto.com/exchange/v1/",
        "market_ws": "wss://stream.crypto.com/exchange/v1/market",
        "user_ws": "wss://stream.crypto.com/exchange/v1/user",
    },
    Environment.UAT: {
        "rest": "https://uat-api.3ona.co/exchange/v1/",
        "market_ws": "wss://uat-stream.3ona.co/exchange/v1/market",
        "user_ws": "wss://uat-stream.3ona.co/exchange/v1/user",
    },
}


class StrategyParams(BaseModel):
    """Parameters of the `range_trend` strategy. See PLAN.md for the rules."""

    interval: str = "5m"
    trend_interval: str = "15m"
    window_hours: float = 24.0
    min_range_pct: float = 0.015
    entry_zone: float = 0.15
    exit_zone: float = 0.85
    trend_filter: bool = True
    ema_fast: int = 50
    ema_slow: int = 200
    trend_band: float = 0.001
    slope_lookback: int = 4
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    require_confirmation: bool = True
    max_hold_hours: float = 24.0
    trail_in_uptrend: bool = True
    trail_pct: float = 0.01
    entry_timeout_bars: int = 3
    cooldown_bars: int = 6

    @model_validator(mode="after")
    def _check(self) -> StrategyParams:
        if not 0 <= self.entry_zone < self.exit_zone <= 1:
            raise ValueError("need 0 <= entry_zone < exit_zone <= 1")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be < ema_slow")
        return self


class RiskParams(BaseModel):
    max_position_pct: float = 0.10
    max_open_positions: int = 3
    stop_loss_pct: float = 0.015
    daily_loss_halt_pct: float = 0.03
    min_notional: float = 10.0
    long_only: bool = True


class FeeParams(BaseModel):
    """Fractions, e.g. 0.0025 = 0.25%. Defaults are deliberately pessimistic; set your tier."""

    maker: float = 0.0025
    taker: float = 0.005
    slippage: float = 0.0005
    half_spread: float = 0.0002  # used when no live bid/ask is available (backtests)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRADER_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: Environment = Environment.PROD
    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None

    db_path: Path = Path("data/trader.db")
    symbols: list[str] = Field(default_factory=lambda: ["BTC_USD", "ETH_USD", "SOL_USD"])
    quote_currency: str = "USD"
    starting_cash: float = 10_000.0

    strategy: StrategyParams = Field(default_factory=StrategyParams)
    risk: RiskParams = Field(default_factory=RiskParams)
    fees: FeeParams = Field(default_factory=FeeParams)

    @property
    def rest_url(self) -> str:
        return ENDPOINTS[self.environment]["rest"]

    @property
    def market_ws_url(self) -> str:
        return ENDPOINTS[self.environment]["market_ws"]

    @property
    def user_ws_url(self) -> str:
        return ENDPOINTS[self.environment]["user_ws"]

    @model_validator(mode="after")
    def _check_symbols(self) -> Settings:
        suffix = f"_{self.quote_currency}"
        bad = [s for s in self.symbols if not s.endswith(suffix)]
        if bad:
            raise ValueError(
                f"all symbols must be quoted in {self.quote_currency} (single cash balance): {bad}"
            )
        return self


def apply_set(data: dict, assignment: str) -> None:
    """Apply a dotted `key.path=value` override; value is parsed as JSON when possible."""
    key, sep, raw = assignment.partition("=")
    if not sep:
        raise ValueError(f"expected key=value, got {assignment!r}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    *parents, leaf = key.strip().split(".")
    node = data
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value


def load_settings(
    config_path: Path | None = None, sets: list[str] | None = None, **overrides
) -> Settings:
    """Load `config.toml` (if present) + env/.env, then explicit overrides and `--set`s."""
    data: dict = {}
    path = config_path or Path("config.toml")
    if path.exists():
        data = tomllib.loads(path.read_text())
    data.update({k: v for k, v in overrides.items() if v is not None})
    for assignment in sets or []:
        apply_set(data, assignment)
    return Settings(**data)
