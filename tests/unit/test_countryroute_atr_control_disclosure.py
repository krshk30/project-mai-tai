"""The authenticated page describes its selected owner, not a future layer."""
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest

from project_mai_tai.services.bot_control_routes import build_control_router, ControlScope
from project_mai_tai.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("code", ["schwab_1m_v2", "orb_schwab", "momentum_warn_live"])
async def test_page_matches_atr_owner_and_preserves_other_bot_limits(enabled, code):
    identity = uuid4()
    state = SimpleNamespace(desired_state="RUN", generation=0, active_command_id=None)
    store = SimpleNamespace(describe=lambda _: (state, "IDLE"))
    app = FastAPI()
    app.include_router(build_control_router(
        store, lambda _: ControlScope(identity, (uuid4(),)),
        settings=Settings(_env_file=None, strategy_schwab_1m_v2_country_route_enabled=enabled),
    ))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="https://project-mai-tai.live",
    ) as browser:
        result = await browser.get(f"/bot-control/{code}", headers={"X-Mai-Tai-Operator": "operator"})
    assert result.status_code == 200
    assert "STOP is not broker-flat proof. Paper is unaffected." in result.text
    if code == "momentum_warn_live":
        assert "Held legacy positions" not in result.text
        assert "post-boot positions" not in result.text
        return
    assert "partial native Schwab BUY parent is blocked" in result.text
    if code == "schwab_1m_v2" and enabled:
        assert "Positions held across OMS restart are BLOCKED" in result.text
        assert "Only proven post-boot positions" in result.text
        assert "buying broker" in result.text
        assert "Held legacy positions" not in result.text
    else:
        assert "Held legacy positions are BLOCKED" in result.text
        assert "STOP does not sell them" in result.text
        assert "post-boot positions" not in result.text
