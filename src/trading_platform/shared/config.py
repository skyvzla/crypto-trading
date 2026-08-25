"""
配置管理模块
使用 pydantic-settings 加载环境变量和配置文件
"""
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal


# 10% is a stress-test ceiling; normal experiments should use far smaller values.
MARKET_SLIPPAGE_MAX_BPS = 1_000.0


class DatabaseConfig(BaseSettings):
    """PostgreSQL 数据库配置"""
    model_config = SettingsConfigDict(env_prefix='DB_')

    host: str = 'localhost'
    port: int = 5432
    user: str = 'postgres'
    password: str = 'postgres'
    database: str = 'trading_platform'

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


class RedisConfig(BaseSettings):
    """Redis 配置"""
    model_config = SettingsConfigDict(env_prefix='REDIS_')

    host: str = 'localhost'
    port: int = 6379
    db: int = 0
    password: str | None = None


class BinanceConfig(BaseSettings):
    """Binance API 配置"""
    model_config = SettingsConfigDict(env_prefix='BINANCE_')

    api_key: str = Field(default='')
    api_secret: str = Field(default='')
    base_url: str = 'https://fapi.binance.com'
    ws_base_url: str = 'wss://fstream.binance.com'
    testnet: bool = False

    @model_validator(mode="after")
    def apply_testnet_endpoints(self) -> "BinanceConfig":
        """Use Binance Futures testnet endpoints when testnet is enabled."""
        if self.testnet:
            if self.base_url == "https://fapi.binance.com":
                self.base_url = "https://demo-fapi.binance.com"
            if self.ws_base_url == "wss://fstream.binance.com":
                self.ws_base_url = "wss://stream.binancefuture.com"
        return self


class MarketLayerConfig(BaseSettings):
    """行情层配置"""
    model_config = SettingsConfigDict(env_prefix='MARKET_')

    host: str = '0.0.0.0'
    port: int = 8000
    data_dir: str = 'data/market'
    redis: RedisConfig = Field(default_factory=RedisConfig)


class StrategyConfig(BaseSettings):
    """策略配置基类"""
    model_config = SettingsConfigDict(env_prefix='STRATEGY_')

    account_id: str = Field(description="交易账户ID")
    market_api_url: str = 'http://localhost:8000'
    risk_max_position_value_usdt: float = 10000.0
    risk_max_symbols: int = 10


class LedgerConfig(BaseSettings):
    """账本层配置"""
    model_config = SettingsConfigDict(env_prefix='LEDGER_')

    host: str = '0.0.0.0'
    port: int = 8001
    web_dist: str = 'web/dist'
    db: DatabaseConfig = Field(default_factory=DatabaseConfig)


class BacktestConfig(BaseSettings):
    """回测配置"""
    model_config = SettingsConfigDict(env_prefix='BACKTEST_')

    # Sweep 并发属于主机级运行配置，避免不同实验 TOML/命令覆盖并发规模。
    workers: int = Field(default=13, gt=0)
    data_dir: str = 'data/market'
    output_dir: str = 'reports'
    maker_fee_rate: float = 0.0002  # 0.02%
    taker_fee_rate: float = 0.0004  # 0.04%
    # MARKET 成交的方向不利价格冲击；0 保持历史回测口径。
    market_slippage_bps: float = Field(
        default=0.0, ge=0, le=MARKET_SLIPPAGE_MAX_BPS
    )
    trading_start_ms: int | None = None  # 此前事件只用于指标预热
    limit_fill_fraction_per_bar: float = Field(default=1.0, gt=0, le=1)
    bar1s_time_shift_ms: int = 0  # 历史源已证实存在偏移时才显式设置
    prior_high_lookback_minutes: int = Field(default=240, gt=0)
    strategy_path: str | None = None
    spike_strategy_version: str = "v1"
    spike_entry_tier_mode: str = "three-tier"
    spike_rise_low_lookback_minutes: int = 0
    spike_min_rise_duration_minutes: int = 0
    spike_early_profit_unlock_ratio: float | None = None

    @field_validator("market_slippage_bps", mode="after")
    @classmethod
    def normalize_market_slippage_bps(cls, value: float) -> float:
        """Keep zero-valued identities stable regardless of their sign."""
        return 0.0 if value == 0 else value


def load_config() -> dict:
    """加载配置（简化版本，返回字典）"""
    import os

    return {
        "database": {
            "host": os.getenv("DB_HOST", "localhost"),
            "port": int(os.getenv("DB_PORT", "5432")),
            "user": os.getenv("DB_USER", "postgres"),
            "password": os.getenv("DB_PASSWORD", "postgres"),
            "database": os.getenv("DB_DATABASE", "trading_platform"),
            "pool_min_size": int(os.getenv("DB_POOL_MIN_SIZE", "2")),
            "pool_max_size": int(os.getenv("DB_POOL_MAX_SIZE", "10")),
        },
        "ledger": {
            "host": os.getenv("LEDGER_HOST", "0.0.0.0"),
            "port": int(os.getenv("LEDGER_PORT", "8001")),
            "web_dist": os.getenv("LEDGER_WEB_DIST", "web/dist"),
        },
        "redis": {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", "6379")),
            "db": int(os.getenv("REDIS_DB", "0")),
        },
    }
