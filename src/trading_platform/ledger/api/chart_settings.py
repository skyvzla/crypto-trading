"""Global chart indicator settings API.

The chart is currently shared by the local installation, so settings are kept
under one stable database key rather than being associated with a user.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from trading_platform.ledger.db.models import LedgerDB


_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")
Color = Annotated[str, StringConstraints(pattern=_COLOR_PATTERN.pattern)]


class _ChartModel(BaseModel):
    """Reject typos and coercion in persisted chart configuration."""

    model_config = ConfigDict(extra="forbid", strict=True)


class IndicatorLine(_ChartModel):
    period: int = Field(gt=0, le=500)
    color: Color


def _validate_unique_periods(lines: list[IndicatorLine]) -> list[IndicatorLine]:
    periods = [line.period for line in lines]
    if len(periods) != len(set(periods)):
        raise ValueError("indicator line periods must be unique")
    return lines


class BollingerColors(_ChartModel):
    upper: Color
    middle: Color
    lower: Color


class MacdColors(_ChartModel):
    dif: Color
    dea: Color
    histogram_up: Color
    histogram_down: Color


class KdjColors(_ChartModel):
    k: Color
    d: Color
    j: Color


class EmaSettings(_ChartModel):
    enabled: bool = True
    lines: list[IndicatorLine] = Field(min_length=1, max_length=8)

    _unique_periods = field_validator("lines")(_validate_unique_periods)


class MaSettings(_ChartModel):
    enabled: bool = False
    lines: list[IndicatorLine] = Field(min_length=1, max_length=8)

    _unique_periods = field_validator("lines")(_validate_unique_periods)


class BollSettings(_ChartModel):
    enabled: bool = False
    period: int = Field(default=20, gt=0, le=500)
    deviation: float = Field(default=2.0, gt=0, le=10, strict=False)
    colors: BollingerColors


class MainChartSettings(_ChartModel):
    ema: EmaSettings
    ma: MaSettings
    boll: BollSettings


class VolumeSettings(_ChartModel):
    enabled: bool = True
    ma_lines: list[IndicatorLine] = Field(min_length=1, max_length=8)

    _unique_periods = field_validator("ma_lines")(_validate_unique_periods)


class MacdSettings(_ChartModel):
    enabled: bool = False
    fast_period: int = Field(default=12, gt=0, le=500)
    slow_period: int = Field(default=26, gt=0, le=500)
    signal_period: int = Field(default=9, gt=0, le=500)
    colors: MacdColors

    @model_validator(mode="after")
    def validate_period_order(self) -> "MacdSettings":
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be less than slow_period")
        return self


class KdjSettings(_ChartModel):
    enabled: bool = False
    period: int = Field(default=9, gt=0, le=500)
    colors: KdjColors


class RsiSettings(_ChartModel):
    enabled: bool = False
    lines: list[IndicatorLine] = Field(min_length=1, max_length=8)

    _unique_periods = field_validator("lines")(_validate_unique_periods)


class AtrSettings(_ChartModel):
    enabled: bool = False
    period: int = Field(default=14, gt=0, le=500)
    color: Color


class SubChartSettings(_ChartModel):
    volume: VolumeSettings
    macd: MacdSettings
    kdj: KdjSettings
    rsi: RsiSettings
    atr: AtrSettings


class ChartSettings(_ChartModel):
    """The complete replacement document accepted by the PUT endpoint."""

    main: MainChartSettings
    sub: SubChartSettings


class ChartSettingsResponse(ChartSettings):
    """Persisted settings plus server-managed metadata."""

    updated_at: datetime | None = None


DEFAULT_CHART_SETTINGS: dict[str, Any] = {
    "main": {
        "ema": {
            "enabled": False,
            "lines": [
                {"period": 9, "color": "#f5c451"},
                {"period": 21, "color": "#66b3ff"},
            ],
        },
        "ma": {
            "enabled": False,
            "lines": [
                {"period": 5, "color": "#f59e0b"},
                {"period": 10, "color": "#22c55e"},
                {"period": 20, "color": "#3b82f6"},
            ],
        },
        "boll": {
            "enabled": False,
            "period": 20,
            "deviation": 2.0,
            "colors": {
                "upper": "#ef4444",
                "middle": "#eab308",
                "lower": "#22c55e",
            },
        },
    },
    "sub": {
        "volume": {
            "enabled": True,
            "ma_lines": [
                {"period": 5, "color": "#f5c451"},
                {"period": 20, "color": "#4da3ff"},
            ],
        },
        "macd": {
            "enabled": False,
            "fast_period": 12,
            "slow_period": 26,
            "signal_period": 9,
            "colors": {
                "dif": "#4da3ff",
                "dea": "#f5c451",
                "histogram_up": "#2ebd85",
                "histogram_down": "#f05252",
            },
        },
        "kdj": {
            "enabled": False,
            "period": 9,
            "colors": {"k": "#4da3ff", "d": "#f5c451", "j": "#d98bff"},
        },
        "rsi": {
            "enabled": False,
            "lines": [
                {"period": 6, "color": "#f5c451"},
                {"period": 12, "color": "#4da3ff"},
                {"period": 24, "color": "#d98bff"},
            ],
        },
        "atr": {"enabled": False, "period": 14, "color": "#14b8a6"},
    },
}


def default_chart_settings() -> ChartSettings:
    """Return a validated copy so callers cannot mutate global defaults."""

    return ChartSettings.model_validate(DEFAULT_CHART_SETTINGS)


router = APIRouter(prefix="/api/v1/chart-settings", tags=["chart-settings"])


async def get_db(request: Request) -> LedgerDB:
    db = getattr(request.app.state, "ledger_db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return db


@router.get("", response_model=ChartSettingsResponse)
async def get_chart_settings(
    db: LedgerDB = Depends(get_db),
) -> ChartSettingsResponse:
    stored, updated_at = await db.get_chart_settings()
    settings = (
        default_chart_settings()
        if not stored
        else ChartSettings.model_validate(stored)
    )
    return ChartSettingsResponse(
        **settings.model_dump(),
        updated_at=updated_at,
    )


@router.put("", response_model=ChartSettingsResponse)
async def put_chart_settings(
    settings: ChartSettings,
    db: LedgerDB = Depends(get_db),
) -> ChartSettingsResponse:
    stored, updated_at = await db.upsert_chart_settings(
        settings.model_dump(mode="json")
    )
    persisted = ChartSettings.model_validate(stored)
    return ChartSettingsResponse(
        **persisted.model_dump(),
        updated_at=updated_at,
    )


__all__ = [
    "AtrSettings",
    "BollSettings",
    "ChartSettings",
    "ChartSettingsResponse",
    "DEFAULT_CHART_SETTINGS",
    "EmaSettings",
    "IndicatorLine",
    "KdjSettings",
    "MacdSettings",
    "MaSettings",
    "MainChartSettings",
    "RsiSettings",
    "SubChartSettings",
    "VolumeSettings",
    "default_chart_settings",
    "router",
]
