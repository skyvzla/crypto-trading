"""
配置管理模块
使用 pydantic-settings 加载环境变量和配置文件
"""
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal


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

    data_dir: str = 'data/market'
    output_dir: str = 'reports'
    maker_fee_rate: float = 0.0002  # 0.02%
    taker_fee_rate: float = 0.0004  # 0.04%
    trading_start_ms: int | None = None  # 此前事件只用于指标预热
    limit_fill_fraction_per_bar: float = Field(default=1.0, gt=0, le=1)
    bar1s_time_shift_ms: int = 0  # 历史源已证实存在偏移时才显式设置
    prior_high_lookback_minutes: int = Field(default=240, gt=0)


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
