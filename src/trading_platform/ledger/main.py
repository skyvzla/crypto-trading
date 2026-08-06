"""
账本层主进程
启动 FastAPI 服务，提供账本查询和控制接口
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from trading_platform.ledger.db.models import LedgerDB, create_connection_pool
from trading_platform.ledger.api.routes import router
from trading_platform.shared.config import load_config


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# 全局数据库实例
_ledger_db: Optional[LedgerDB] = None


def get_ledger_db() -> LedgerDB:
    """获取全局数据库实例"""
    if _ledger_db is None:
        raise RuntimeError("Database not initialized")
    return _ledger_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global _ledger_db

    # 启动时初始化
    logger.info("Initializing ledger service...")

    # 加载配置
    config = load_config()
    db_config = config.get("database", {})

    # 构建 PostgreSQL DSN
    dsn = (
        f"postgresql://{db_config.get('user', 'postgres')}:"
        f"{db_config.get('password', 'postgres')}@"
        f"{db_config.get('host', 'localhost')}:"
        f"{db_config.get('port', 5432)}/"
        f"{db_config.get('database', 'trading_platform')}"
    )

    logger.info(f"Connecting to database: {db_config.get('host')}:{db_config.get('port')}/{db_config.get('database')}")

    # 创建连接池
    pool = await create_connection_pool(
        dsn=dsn,
        min_size=db_config.get("pool_min_size", 2),
        max_size=db_config.get("pool_max_size", 10)
    )

    _ledger_db = LedgerDB(pool)
    logger.info("Database connection pool created")

    # 初始化数据库表（可选，也可以手动执行 schema.sql）
    try:
        async with pool.connection() as conn:
            # 检查表是否存在
            result = await conn.execute(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = 'orders'
                )
                """
            )
            row = await result.fetchone()
            if not row or not row[0]:
                logger.warning("Tables not found. Please run schema.sql to initialize database.")
            else:
                logger.info("Database tables verified")
    except Exception as e:
        logger.error(f"Failed to verify database tables: {e}")

    logger.info("Ledger service started successfully")

    yield

    # 关闭时清理
    logger.info("Shutting down ledger service...")
    await pool.close()
    logger.info("Database connection pool closed")


# 创建 FastAPI 应用
app = FastAPI(
    title="Trading Platform Ledger API",
    description="账本层查询接口，提供订单、成交、持仓、盈亏统计和紧急控制",
    version="1.0.0",
    lifespan=lifespan
)

# CORS 中间件（允许 Web 前端访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(router)


# 根路径
@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "Trading Platform Ledger API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/v1/health"
    }


def main():
    """主函数"""
    # 加载配置
    config = load_config()
    ledger_config = config.get("ledger", {})

    host = ledger_config.get("host", "0.0.0.0")
    port = ledger_config.get("port", 8001)

    logger.info(f"Starting ledger service on {host}:{port}")

    # 启动 uvicorn
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=True
    )


if __name__ == "__main__":
    main()
