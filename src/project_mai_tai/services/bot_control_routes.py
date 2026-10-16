"""Loopback/nginx-authenticated KILLSW1 POSTs; GET only renders read-only state."""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import asdict, dataclass
from html import escape
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Strategy, VirtualPosition
from project_mai_tai.oms.bot_control import BotControlStore, ControlConflict
from project_mai_tai.oms.bot_control_scope import (
    CONTROLLED_STRATEGY_CODES, is_controlled_strategy, live_scope, logical_account_mode,
)
from project_mai_tai.settings import Settings

ORIGIN = "https://project-mai-tai.live"
CSRF_COOKIE = "__Host-mai-tai-control-csrf"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


@dataclass(frozen=True)
class ControlScope:
    strategy_id: UUID
    account_ids: tuple[UUID, ...]
    complete: bool = True


def configured_scope(
    settings: Settings, sessions: sessionmaker[Session], code: str
) -> ControlScope:
    if not is_controlled_strategy(code):
        raise ControlConflict("strategy_not_in_controlled_set")
    names = []
    if code in {"momentum_warn_live", "schwab_1m_v2", "orb_schwab"}:
        # Include both legacy venues even when new admission is single-routed.
        names = [
            settings.strategy_schwab_1m_v2_account_name,
            (
                settings.orb_webull_account_name
                if code == "orb_schwab"
                else settings.strategy_schwab_1m_v2_webull_account_name
            ),
        ]
        if code == "orb_schwab" and not settings.orb_webull_account_name:
            names = [settings.strategy_schwab_1m_v2_account_name]
    with sessions() as session:
        strategy = session.scalar(select(Strategy).where(Strategy.code == code))
        if strategy is None or strategy.execution_mode != "live":
            raise ControlConflict("not_a_live_strategy")
        complete = bool(names) and all(name.startswith("live:") for name in names)
        accounts = session.scalars(select(BrokerAccount).where(BrokerAccount.name.in_(names))).all()
        if len(accounts) != len(set(names)):
            complete = False
        historical = session.scalars(
            select(BrokerAccount)
            .join(BrokerOrder, BrokerOrder.broker_account_id == BrokerAccount.id)
            .where(BrokerOrder.strategy_id == strategy.id)
            .distinct()
        ).all()
        positions = session.scalars(
            select(BrokerAccount)
            .join(VirtualPosition, VirtualPosition.broker_account_id == BrokerAccount.id)
            .where(VirtualPosition.strategy_id == strategy.id)
            .distinct()
        ).all()
        if any(logical_account_mode(account.name) == "UNKNOWN" for account in [*historical, *positions]):
            complete = False
        historical = [account for account in historical if live_scope(strategy.execution_mode, account.name)]
        positions = [account for account in positions if live_scope(strategy.execution_mode, account.name)]
        accounts = list(
            {account.id: account for account in [*accounts, *historical, *positions]}.values()
        )
        if any(
            not live_scope(strategy.execution_mode, account.name) or account.provider not in {"schwab", "webull"}
            for account in accounts
        ):
            complete = False
        accounts = [
            account
            for account in accounts
            if live_scope(strategy.execution_mode, account.name) and account.provider in {"schwab", "webull"}
        ]
        return ControlScope(
            strategy.id, tuple(sorted((account.id for account in accounts), key=str)), complete
        )


def _operator(request: Request) -> str:
    actor = request.headers.get("x-mai-tai-operator", "")
    if (
        request.client is None
        or request.client.host != "127.0.0.1"
        or request.url.hostname != "project-mai-tai.live"
        or not actor
        or actor != actor.strip()
        or len(actor) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in actor)
    ):
        raise HTTPException(403, "authenticated_loopback_operator_required")
    return actor


def _csrf_origin(request: Request) -> None:
    origin, referer = request.headers.get("origin"), request.headers.get("referer")
    if not origin and not referer:
        raise HTTPException(403, "origin_required")
    if origin is not None and origin != ORIGIN:
        raise HTTPException(403, "cross_origin_refused")
    if referer is not None:
        try:
            parsed = urlsplit(referer)
        except ValueError as exc:
            raise HTTPException(403, "cross_origin_refused") from exc
        if parsed.scheme != "https" or parsed.netloc != "project-mai-tai.live":
            raise HTTPException(403, "cross_origin_refused")


def build_control_router(
    store: BotControlStore, resolve_scope, *, settings: Settings | None = None
) -> APIRouter:
    router = APIRouter()

    @router.get("/bot-control/{strategy_code}")
    async def page(strategy_code: str, request: Request):
        _operator(request)
        if strategy_code not in CONTROLLED_STRATEGY_CODES:
            raise HTTPException(status_code=404)
        try:
            scope = await asyncio.to_thread(resolve_scope, strategy_code)
            state, phase = await asyncio.to_thread(store.describe, scope.strategy_id)
        except ControlConflict as exc:
            return HTMLResponse(escape(str(exc)), status_code=503)
        token = request.cookies.get(CSRF_COOKIE, "")
        if not _TOKEN.fullmatch(token):
            token = secrets.token_urlsafe(32)
        forms = []
        for action in ("STOP", "ON"):
            forms.append(
                f'<form method="post" action="/bot-control/{escape(strategy_code, quote=True)}" '
                'style="display:inline">'
                f'<input type="hidden" name="csrf_token" value="{token}">'
                f'<input type="hidden" name="request_id" value="{uuid4()}">'
                f'<input type="hidden" name="command_id" value="{uuid4()}">'
                f'<input type="hidden" name="expected_generation" value="{state.generation}">'
                f'<button name="action" value="{action}">{action}</button></form>'
            )
        limit = ""
        if strategy_code in {"schwab_1m_v2", "orb_schwab"}:
            routed_atr = strategy_code == "schwab_1m_v2" and bool(
                getattr(settings, "strategy_schwab_1m_v2_country_route_enabled", False)
            )
            limit = (
                "<p>STOP fences entries and cancels this bot's own working entries. "
                + ("Positions held across OMS restart are BLOCKED with a CRITICAL alert; "
                   "sell manually or wait for normal exit. Only proven post-boot positions "
                   "use the ATR exit owner to close at the buying broker. "
                   if routed_atr else
                   "Held legacy positions are BLOCKED with a CRITICAL alert; "
                   "sell manually or wait for normal exit. STOP does not sell them. ")
                + "A partial native Schwab BUY parent is blocked to preserve its protection.</p>"
            )
        content = (
            "<!doctype html><html><head>"
            + ('<meta http-equiv="refresh" content="5">' if state.active_command_id else "")
            + '</head><body style="font-family:inherit">'
            f"<strong>LIVE {escape(strategy_code)}</strong> "
            f"Admission: {state.desired_state}; generation {state.generation}. "
            f"Cleanup: {escape(phase)}. "
            "STOP is not broker-flat proof. Paper is unaffected. "
            + limit
            + " ".join(forms)
            + "</body></html>"
        )
        response = HTMLResponse(content, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            CSRF_COOKIE, token, secure=True, httponly=True, samesite="strict", path="/"
        )
        return response

    @router.post("/bot-control/{strategy_code}")
    async def command(strategy_code: str, request: Request):
        actor = _operator(request)
        _csrf_origin(request)
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415, "form_required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "control_request_too_large")
        try:
            form = parse_qs(body.decode("utf-8"), strict_parsing=True, max_num_fields=8)
            if any(len(values) != 1 for values in form.values()):
                raise ValueError("duplicate_fields")
            token = form.get("csrf_token", [""])[0]
            cookie = request.cookies.get(CSRF_COOKIE, "")
            if not _TOKEN.fullmatch(token) or not secrets.compare_digest(token, cookie):
                raise HTTPException(403, "csrf_refused")
            command_id, request_id = UUID(form["command_id"][0]), UUID(form["request_id"][0])
            generation = int(form["expected_generation"][0])
            action = form["action"][0]
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(422, "invalid_control_request") from exc
        try:
            scope = await asyncio.to_thread(resolve_scope, strategy_code)
            receipt = await asyncio.to_thread(
                store.request,
                strategy_id=scope.strategy_id,
                command_id=command_id,
                request_id=request_id,
                expected_generation=generation,
                action=action,
                account_ids=scope.account_ids,
                actor_reference=actor,
                reason="operator_bot_control",
                scope_complete=scope.complete,
            )
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if "application/json" in request.headers.get("accept", ""):
            payload = asdict(receipt)
            payload["command_id"] = str(receipt.command_id)
            return JSONResponse(payload)
        return RedirectResponse(f"/bot-control/{strategy_code}", status_code=303)

    return router
