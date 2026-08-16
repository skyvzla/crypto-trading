"""策略运行状态需要 LEDGER_TEST_DSN 指向真实 PostgreSQL。"""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from trading_platform.ledger.api.routes import router
from trading_platform.ledger.db.migrations import apply_migrations, verify_current
from trading_platform.ledger.db.models import (
    LedgerDB,
    StrategyRuntimeStatus,
    create_connection_pool,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"),
    reason="LEDGER_TEST_DSN not set",
)


@pytest.fixture
async def ledger():
    pool = await create_connection_pool(os.environ["LEDGER_TEST_DSN"], 1, 4)
    await apply_migrations(pool)
    assert await verify_current(pool) == 8
    yield LedgerDB(pool)
    await pool.close()


def runtime_status(
    *,
    account_id: str,
    instance_id: str,
    started_at: datetime,
    heartbeat_at: datetime,
    status: str = "running",
) -> StrategyRuntimeStatus:
    return StrategyRuntimeStatus(
        account_id=account_id,
        strategy_id="spike_short",
        instance_id=instance_id,
        mode="testnet",
        status=status,
        entry_enabled=True,
        halted=False,
        halt_reason=None,
        gate_conditions={"lease": True, "user_stream": True},
        started_at=started_at,
        heartbeat_at=heartbeat_at,
        stopped_at=None,
    )


@pytest.mark.asyncio
async def test_runtime_status_rejects_old_instance_and_supports_queries(ledger):
    account = f"runtime-{uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    first = runtime_status(
        account_id=account,
        instance_id="instance-1",
        started_at=now - timedelta(minutes=2),
        heartbeat_at=now - timedelta(seconds=20),
    )
    assert await ledger.upsert_strategy_runtime_status(first) is True

    first.status = "degraded"
    first.entry_enabled = False
    first.gate_conditions = {"lease": True, "user_stream": False}
    first.heartbeat_at = now
    assert await ledger.upsert_strategy_runtime_status(first) is True

    delayed_heartbeat = runtime_status(
        account_id=account,
        instance_id="instance-1",
        started_at=first.started_at + timedelta(minutes=1),
        heartbeat_at=now - timedelta(seconds=1),
        status="running",
    )
    assert await ledger.upsert_strategy_runtime_status(delayed_heartbeat) is False

    same_start = runtime_status(
        account_id=account,
        instance_id="instance-tied",
        started_at=first.started_at,
        heartbeat_at=now + timedelta(seconds=1),
    )
    assert await ledger.upsert_strategy_runtime_status(same_start) is False

    newer = runtime_status(
        account_id=account,
        instance_id="instance-2",
        started_at=now - timedelta(minutes=1),
        heartbeat_at=now,
    )
    assert await ledger.upsert_strategy_runtime_status(newer) is True

    first.heartbeat_at = now + timedelta(seconds=2)
    assert await ledger.upsert_strategy_runtime_status(first) is False
    current = await ledger.get_strategy_runtime_status(
        account_id=account,
        strategy_id="spike_short",
    )
    assert current == newer

    items, total = await ledger.list_strategy_runtime_statuses(
        account_id=account,
        strategy_id="spike_short",
    )
    assert total == 1
    assert items == [newer]
    missing, missing_total = await ledger.list_strategy_runtime_statuses(
        account_id=account,
        strategy_id="other",
    )
    assert missing == []
    assert missing_total == 0


@pytest.mark.asyncio
async def test_runtime_status_api_marks_only_live_states_stale(ledger):
    now = datetime.now(timezone.utc)
    stale_account = f"stale-{uuid4().hex[:10]}"
    active_account = f"active-{uuid4().hex[:10]}"
    stopped_account = f"stopped-{uuid4().hex[:10]}"
    await ledger.upsert_strategy_runtime_status(
        runtime_status(
            account_id=stale_account,
            instance_id="stale-instance",
            started_at=now - timedelta(minutes=1),
            heartbeat_at=now - timedelta(seconds=20),
            status="degraded",
        )
    )
    await ledger.upsert_strategy_runtime_status(
        runtime_status(
            account_id=active_account,
            instance_id="active-instance",
            started_at=now - timedelta(seconds=5),
            heartbeat_at=now,
        )
    )
    stopped = runtime_status(
        account_id=stopped_account,
        instance_id="stopped-instance",
        started_at=now - timedelta(minutes=2),
        heartbeat_at=now - timedelta(minutes=1),
        status="stopped",
    )
    stopped.entry_enabled = False
    stopped.stopped_at = now - timedelta(minutes=1)
    await ledger.upsert_strategy_runtime_status(stopped)

    app = FastAPI()
    app.state.ledger_db = ledger
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        stale_response = await client.get(
            "/api/v1/strategy-runtime-status",
            params={"account_id": stale_account, "strategy_id": "spike_short"},
        )
        active_response = await client.get(
            "/api/v1/strategy-runtime-status",
            params={"account_id": active_account},
        )
        stopped_response = await client.get(
            "/api/v1/strategy-runtime-status",
            params={"account_id": stopped_account},
        )

    assert stale_response.status_code == 200
    stale_page = stale_response.json()
    assert stale_page["total"] == 1
    assert stale_page["items"][0]["status"] == "degraded"
    assert stale_page["items"][0]["effective_status"] == "stale"
    assert stale_page["items"][0]["gate_conditions"] == {
        "lease": True,
        "user_stream": True,
    }
    assert active_response.status_code == 200
    assert active_response.json()["items"][0]["effective_status"] == "running"
    assert stopped_response.status_code == 200
    assert stopped_response.json()["items"][0]["effective_status"] == "stopped"
