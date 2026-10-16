from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import Base, BotControlCommand, BrokerAccount, Strategy
from project_mai_tai.oms.bot_control import BotControlStore, ControlConflict
from project_mai_tai.services.bot_control_routes import (
    CSRF_COOKIE,
    ControlScope,
    build_control_router,
    configured_scope,
)
from project_mai_tai.settings import Settings


@pytest.fixture
def control_http():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    strategy, account = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            Strategy(id=strategy, code="momentum_warn_live", name="MOM", execution_mode="live")
        )
        session.add(
            BrokerAccount(id=account, name="live:orb", provider="webull", environment="live")
        )
    store = BotControlStore(sessions)
    store.seed(strategy)

    def scope(code):
        if code != "momentum_warn_live":
            raise ControlConflict("not_a_live_strategy")
        return ControlScope(strategy, (account,))

    app = FastAPI()
    app.include_router(build_control_router(store, scope))
    yield app, store, sessions, strategy
    engine.dispose()


def client(app, *, peer="127.0.0.1", host="project-mai-tai.live"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 12345)),
        base_url=f"https://{host}",
        headers={"X-Mai-Tai-Operator": "operator"},
    )


async def form(browser):
    response = await browser.get("/bot-control/momentum_warn_live")
    assert response.status_code == 200
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "confirm(" not in response.text
    return dict(
        csrf_token=browser.cookies[CSRF_COOKIE],
        command_id=str(uuid4()),
        request_id=str(uuid4()),
        expected_generation="0",
        action="STOP",
    )


@pytest.mark.asyncio
async def test_authenticated_post_commits_stop_and_same_request_replays(control_http):
    app, store, sessions, strategy = control_http
    async with client(app) as browser:
        fields = await form(browser)
        fields["actor_reference"] = "spoofed-body-actor"
        headers = {"Origin": "https://project-mai-tai.live", "Accept": "application/json"}
        first = await browser.post("/bot-control/momentum_warn_live", data=fields, headers=headers)
        assert first.status_code == 200 and first.json()["generation"] == 1
        assert first.json()["phase"] == "PENDING_CENSUS"
        assert not store.hydrate(strategy).admission_open
        second = await browser.post("/bot-control/momentum_warn_live", data=fields, headers=headers)
        assert second.status_code == 200 and second.json()["replayed"] is True
        assert second.json()["command_id"] == first.json()["command_id"]
    with sessions() as session:
        commands = session.scalars(select(BotControlCommand)).all()
        assert len(commands) == 1 and commands[0].actor_reference == "operator"


@pytest.mark.asyncio
async def test_missing_operator_header_refuses_before_side_effect(control_http):
    app, store, _, strategy = control_http
    async with client(app) as browser:
        fields = await form(browser)
        browser.headers.pop("X-Mai-Tai-Operator")
        result = await browser.post(
            "/bot-control/momentum_warn_live",
            data=fields,
            headers={"Origin": "https://project-mai-tai.live"},
        )
        assert result.status_code == 403
    assert store.hydrate(strategy).generation == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["203.0.113.1", "::1", "127.0.0.2"])
async def test_nonloopback_peer_cannot_spoof_forwarded_identity(control_http, peer):
    app, store, _, strategy = control_http
    async with client(app, peer=peer) as browser:
        result = await browser.post(
            "/bot-control/momentum_warn_live",
            data={},
            headers={"X-Forwarded-For": "127.0.0.1", "Origin": "https://project-mai-tai.live"},
        )
        assert result.status_code == 403
    assert store.hydrate(strategy).generation == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://evil.example",
        "http://project-mai-tai.live",
        "https://project-mai-tai.live.evil.example",
    ],
)
async def test_missing_or_cross_origin_refuses(control_http, origin):
    app, store, _, strategy = control_http
    async with client(app) as browser:
        fields = await form(browser)
        result = await browser.post(
            "/bot-control/momentum_warn_live",
            data=fields,
            headers={} if origin is None else {"Origin": origin},
        )
        assert result.status_code == 403
    assert store.hydrate(strategy).generation == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing_field", "bad_field", "missing_cookie", "bad_cookie"])
async def test_double_submit_csrf_is_required(control_http, kind):
    app, store, _, strategy = control_http
    async with client(app) as browser:
        fields = await form(browser)
        if kind == "missing_field":
            fields.pop("csrf_token")
        elif kind == "bad_field":
            fields["csrf_token"] = "b" * 43
        elif kind == "missing_cookie":
            browser.cookies.clear()
        else:
            browser.cookies.set(CSRF_COOKIE, "c" * 43, domain="project-mai-tai.live", path="/")
        result = await browser.post(
            "/bot-control/momentum_warn_live",
            data=fields,
            headers={"Origin": "https://project-mai-tai.live"},
        )
        assert result.status_code == 403
    assert store.hydrate(strategy).generation == 0


@pytest.mark.asyncio
async def test_hook_host_and_auth_prefix_cannot_reach_command(control_http):
    app, store, _, strategy = control_http
    async with client(app, host="hook.project-mai-tai.live") as browser:
        assert (
            await browser.post("/auth/bot-control/momentum_warn_live", data={})
        ).status_code == 404
        assert (await browser.post("/bot-control/momentum_warn_live", data={})).status_code == 403
    assert store.hydrate(strategy).generation == 0


@pytest.mark.asyncio
async def test_get_is_read_only_and_paper_scope_cannot_target_live(control_http):
    app, store, _, strategy = control_http
    async with client(app) as browser:
        await form(browser)
        assert store.hydrate(strategy).generation == 0
        fields = await form(browser)
        result = await browser.post(
            "/bot-control/momentum_30s",
            data=fields,
            headers={"Origin": "https://project-mai-tai.live"},
        )
        assert result.status_code == 409
    assert store.hydrate(strategy).generation == 0


@pytest.mark.asyncio
async def test_referer_is_valid_fallback_but_not_userinfo_or_foreign_origin(control_http):
    app, _, _, _ = control_http
    async with client(app) as browser:
        fields = await form(browser)
        bad = await browser.post(
            "/bot-control/momentum_warn_live",
            data=fields,
            headers={"Referer": "https://project-mai-tai.live@evil.example/path"},
        )
        assert bad.status_code == 403
        good = await browser.post(
            "/bot-control/momentum_warn_live",
            data=fields,
            headers={
                "Referer": "https://project-mai-tai.live/bot/momentum-30",
                "Accept": "application/json",
            },
        )
        assert good.status_code == 200


def test_orb_scope_uses_its_actual_configured_webull_route_not_atr_default(control_http):
    _, _, sessions, _ = control_http
    primary, routed = uuid4(), uuid4()
    with sessions.begin() as unit:
        unit.add(Strategy(id=uuid4(), code="orb_schwab", name="ORB", execution_mode="live"))
        unit.add(
            BrokerAccount(
                id=primary, name="live:schwab_1m_v2", provider="schwab", environment="live"
            )
        )
        unit.add(
            BrokerAccount(id=routed, name="live:orb-routed", provider="webull", environment="live")
        )
    settings = Settings(
        orb_country_routing_enabled=True,
        orb_webull_account_name="live:orb-routed",
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    scope = configured_scope(settings, sessions, "orb_schwab")
    assert scope.complete and set(scope.account_ids) == {primary, routed}
