"""
账本层主进程
启动 FastAPI 服务，提供账本查询和控制接口
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from trading_platform.ledger.db.models import LedgerDB, create_connection_pool
from trading_platform.ledger.db.migrations import apply_migrations, verify_current
from trading_platform.ledger.api.routes import router
from trading_platform.ledger.api.backtests import router as backtest_router
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
        result = await apply_migrations(pool)
        current = await verify_current(pool)
        logger.info(
            "Database schema verified at version %04d; applied=%s",
            current,
            result.applied_versions or "none",
        )
    except Exception as e:
        await pool.close()
        raise RuntimeError(f"Failed to migrate or verify database schema: {e}") from e

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
app.include_router(backtest_router)

# Vite 产物目录，相对进程工作目录解析（容器内为 /app）。
# 未构建时跳过挂载，保证 API 与 dev server 模式下服务仍可启动。
web_dist = Path(load_config()["ledger"]["web_dist"])
web_index = web_dist / "index.html"
if web_index.is_file():
    app.mount(
        "/assets",
        StaticFiles(directory=web_dist / "assets"),
        name="ledger-ui-assets",
    )

    @app.get("/", include_in_schema=False)
    async def ledger_ui_root() -> FileResponse:
        return FileResponse(web_index)

    @app.get("/{client_path:path}", include_in_schema=False)
    async def ledger_ui_history_fallback(client_path: str) -> FileResponse:
        first_segment = client_path.partition("/")[0]
        if (
            first_segment in {"api", "assets", "ui"}
            or Path(client_path).suffix
        ):
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(web_index)
else:
    logger.warning(
        "Web UI not built at %s; run 'npm run build' in web/ "
        "or use the Vite dev server",
        web_dist.resolve(),
    )

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
