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
        old_entry = await client.get("/ui/")
        missing_asset = await client.get("/assets/does-not-exist.js")

    assert page.status_code == 200
    assert '<div id="app">' in page.text
    assert old_entry.status_code == 404
    assert missing_asset.status_code == 404
