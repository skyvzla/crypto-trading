"""
账本层主进程
启动 FastAPI 服务，提供账本查询和控制接口
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""

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

    try:
        async with pool.connection() as conn:
            result = await conn.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name IN ('orders', 'trades', 'positions', 'subcategory_admission', 'subcategory_admission_audit', 'strategy_audit_events')
                """
            )
            tables = await result.fetchone()
            if not tables or tables[0] != 6:
                raise RuntimeError("Required ledger tables are missing; apply ledger/db/schema.sql")
            logger.info("Database tables verified")
    except Exception as e:
        await pool.close()
        raise RuntimeError(f"Failed to verify database tables: {e}") from e

    app.state.ledger_db = LedgerDB(pool)
    logger.info("Database connection pool created")
    logger.info("Ledger service started successfully")

    try:
        yield
    finally:
        logger.info("Shutting down ledger service...")
        await pool.close()
        app.state.ledger_db = None
        logger.info("Database connection pool closed")


# 创建 FastAPI 应用
app = FastAPI(
    title="Trading Platform Ledger API",
    description="账本层查询接口与 subcategory 交易池准入控制",
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

web_dir = Path(__file__).with_name("web")
app.mount("/ui", StaticFiles(directory=web_dir, html=True), name="ledger-ui")


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
