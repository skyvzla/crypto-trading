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
async def test_ledger_web_shell_and_spa_fallback_are_served():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        page = await client.get("/ui/")
        # history 模式深链接必须回退到 SPA 外壳，而不是 404
        deep_link = await client.get("/ui/admissions")
        missing_asset = await client.get("/ui/assets/does-not-exist.js")

    assert page.status_code == 200
    assert '<div id="app">' in page.text
    assert deep_link.status_code == 200
    assert deep_link.text == page.text
    assert missing_asset.status_code == 404
