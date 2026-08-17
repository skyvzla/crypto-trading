from pathlib import Path

import httpx
import pytest

from trading_platform.ledger.main import app


WEB_DIST = Path("web/dist/index.html")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not WEB_DIST.is_file(),
    reason="web/dist 未构建；容器构建阶段会生成",
)
async def test_ledger_web_shell_is_served_from_root():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        page = await client.get("/")
        history_pages = [
            await client.get(path)
            for path in (
                "/overview",
                "/calendar",
                "/positions",
                "/trades",
                "/performance",
                "/backtests/reports",
                "/strategy-risk",
                "/universe",
                "/categories",
            )
        ]
        old_entry = await client.get("/ui/")
        missing_asset = await client.get("/assets/does-not-exist.js")
        missing_api = await client.get("/api/v1/does-not-exist")

    assert page.status_code == 200
    assert '<div id="app">' in page.text
    assert all(response.status_code == 200 for response in history_pages)
    assert all('<div id="app">' in response.text for response in history_pages)
    assert old_entry.status_code == 404
    assert missing_asset.status_code == 404
    assert missing_api.status_code == 404
    assert missing_api.headers["content-type"].startswith("application/json")
