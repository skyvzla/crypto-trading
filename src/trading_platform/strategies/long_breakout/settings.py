"""Environment-backed configuration for the dedicated long-breakout worker."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .core import SUPPORTED_TIMEFRAMES


class LongBreakoutSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LONG_BREAKOUT_")

    mode: Literal["testnet"] = "testnet"
    account_id: str = "long_breakout_testnet"
    symbols: str = "BTCUSDT"
    timeframe: Literal["4h", "1d"] = "4h"
    lookback_bars: int | None = Field(default=None, gt=0)
    entry_notional_usdt: Decimal = Field(default=Decimal("10"), gt=0)
    leverage: int = Field(default=1, ge=1, le=3)
    breakout_buffer_pct: Decimal = Field(default=Decimal("0"), ge=0)
    take_profit_pct: Decimal | None = Field(default=Decimal("0.02"), gt=0)
    stop_loss_pct: Decimal | None = Field(default=Decimal("0.01"), gt=0)
    entry_enabled: bool = False
    subcategory: str = "long_breakout"
    wal_path: Path = Path("data/wal/long_breakout.jsonl")
    dedicated_strategy_account: bool = True
    market_poll_seconds: float = Field(default=30, ge=5, le=300)
    heartbeat_seconds: float = Field(default=10, ge=5, le=60)
    poll_interval_seconds: float = Field(default=5, ge=1, le=60)
    max_poll_attempts: int = Field(default=12, ge=1, le=120)

    @field_validator("lookback_bars", mode="before")
    @classmethod
    def empty_lookback_uses_timeframe_default(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("account_id", "symbols", "subcategory", mode="after")
    @classmethod
    def require_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_dedicated_testnet_worker(self) -> "LongBreakoutSettings":
        if not self.dedicated_strategy_account:
            raise ValueError("long_breakout requires a dedicated Binance account")
        symbols = self.symbol_list
        if not symbols:
            raise ValueError("at least one symbol is required")
        if len(symbols) != len(set(symbols)):
            raise ValueError("symbols must be unique")
        expected_lookback = SUPPORTED_TIMEFRAMES[self.timeframe]
        if self.lookback_bars is None:
            self.lookback_bars = expected_lookback
        elif self.lookback_bars != expected_lookback:
            raise ValueError(
                f"lookback_bars must be {expected_lookback} for a 7-day "
                f"{self.timeframe} box"
            )
        if self.wal_path.name == "spike_short.jsonl":
            raise ValueError("long_breakout must not share the Spike WAL")
        return self

    @property
    def symbol_list(self) -> list[str]:
        return [
            symbol.strip().upper()
            for symbol in self.symbols.split(",")
            if symbol.strip()
        ]
