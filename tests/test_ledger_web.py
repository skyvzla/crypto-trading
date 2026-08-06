from pathlib import Path

import httpx
import pytest

from trading_platform.ledger.main import app


@pytest.mark.asyncio
async def test_ledger_web_assets_are_served():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        page = await client.get("/ui/")
        script = await client.get("/ui/app.js")
        styles = await client.get("/ui/styles.css")

    assert page.status_code == 200
    assert "Trade Ledger" in page.text
    assert "subcategory" in page.text
    assert script.status_code == 200
    assert "/subcategory-admissions" in script.text
    assert styles.status_code == 200
    assert "--accent" in styles.text


def test_ledger_web_does_not_expose_unconfirmed_controls():
    script = Path(
        "src/trading_platform/ledger/web/app.js"
    ).read_text()
    assert "CLOSE_ALL" not in script
    assert "account_control_state" not in script
