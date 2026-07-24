"""Webull mirror-on-FILL in EXTENDED HOURS (mirror-EH).

A MARKET master + native-OCO combo are BOTH RTH-only on Webull (417 in EH). So when the primary
Schwab v2 buy-open FILLS in extended hours, the mirror must instead emit a single-leg marketable
EH-LIMIT master (NO combo) priced off OUR fresh ask and bounded by the P-B1 max-cross cap; the
mirrored Webull position is then exit-managed by the account-aware software EH-limit CW ladder
(#390, no broker OCO in EH). See docs/premarket-eod-exit-design.md.

Load-bearing properties proven here:
- EH + mirror-EH flag ON -> LIMIT master, order_type=limit, extended_hours=true, session=AM/PM,
  ask-derived limit_price, NO bracket / native_oco_bracket / bracket_* keys;
- the cap: an ask past schwab_fill*(1+max_cross%) -> ABANDON (no submit);
- no fresh ask in EH -> ABANDON (no blind EH order);
- mirror-EH flag OFF in EH -> byte-identical to today: MARKET + combo (the RTH-only mirror);
- RTH (mirror-EH flag ON) -> byte-identical: MARKET + combo;
- the max_cross threshold is pinned (mutation-detecting).

Harness mirrors test_webull_mirror_on_fill.py: a real OmsRiskService over an in-memory SQLite
session_factory with an injectable, request-capturing broker adapter, and the same eh/rth
monkeypatch fixtures the EH-entry tests use to pin the session clock.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.base import Base
from project_mai_tai.oms import service as oms_service
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

PRIMARY = "paper:schwab_1m_v2"
WEBULL = "live:v2_webull"


# --------------------------------------------------------------------------- helpers

class _FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    async def xadd(self, stream, fields, **kwargs):
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def get(self, key):
        return None

    async def set(self, key, value, ex=None):
        del ex
        return True

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        return []

    async def aclose(self):
        return None


async def _noop_sync_broker_state(*, account_names=None):
    del account_names
    return None


class _CaptureAdapter:
    """Records every submit_order request and returns a simple 'accepted' report."""

    def __init__(self) -> None:
        self.requests: list[OrderRequest] = []
        self._n = 0

    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        self.requests.append(request)
        self._n += 1
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=f"wb-{self._n}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request: OrderRequest):
        return None

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _oms(*, adapter, mirror_on: bool = True, mirror_eh_on: bool = True,
         webull_account: str = WEBULL) -> OmsRiskService:
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_v2_exit_management_enabled=True,
            strategy_schwab_1m_v2_account_name=PRIMARY,
            strategy_schwab_1m_v2_webull_account_name=webull_account,
            strategy_schwab_1m_v2_webull_mirror_enabled=mirror_on,
            strategy_schwab_1m_v2_webull_mirror_eh_enabled=mirror_eh_on,
        ),
        redis_client=_FakeRedis(),
        session_factory=_session_factory(),
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]
    return service


def _seed_fresh_ask(service: OmsRiskService, symbol: str, ask: float, *, age_ms: float = 0.0) -> None:
    service._latest_quotes_by_symbol[symbol] = {
        "bid": ask - 0.02,
        "ask": ask,
        "received_at": datetime.now(timezone.utc) - timedelta(milliseconds=age_ms),
    }


@pytest.fixture()
def eh(monkeypatch):
    """Pin the OMS session clock to PRE-MARKET (extended hours)."""
    monkeypatch.setattr(oms_service, "_is_regular_market_session", lambda now=None: False)
    monkeypatch.setattr(oms_service, "_extended_hours_session", lambda now=None: "AM")


@pytest.fixture()
def rth(monkeypatch):
    """Pin the OMS session clock to REGULAR hours."""
    monkeypatch.setattr(oms_service, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(oms_service, "_extended_hours_session", lambda now=None: None)


async def _mirror(service: OmsRiskService, *, symbol="VSME", qty="10", fill=2.50):
    await service._mirror_v2_fill_to_webull(
        symbol=symbol, quantity=Decimal(qty), schwab_fill_price=fill,
        source_metadata={"path": "ATR Flip"},
    )


# --------------------------------------------------------------------------- EH tests

@pytest.mark.asyncio
async def test_eh_builds_limit_master_no_combo(eh):
    """EH + mirror-EH ON: a single-leg marketable EH-LIMIT master, NO native-OCO combo.
    order_type=limit, extended_hours=true, session=AM, limit priced off the ask (+buffer)."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True, mirror_eh_on=True)
    # ask 2.51 is inside the max-cross cap of fill 2.50 (cap = 2.50*1.01 = 2.525).
    _seed_fresh_ask(service, "VSME", ask=2.51)
    await _mirror(service, symbol="VSME", fill=2.50)

    assert len(adapter.requests) == 1
    req = adapter.requests[0]
    assert req.broker_account_name == WEBULL
    assert req.order_type == "limit"
    md = req.metadata
    # NO combo — the broker OCO is RTH-only.
    assert "bracket" not in md
    assert "native_oco_bracket" not in md
    assert "bracket_entry_type" not in md
    assert "bracket_target_price" not in md
    assert "bracket_stop_price" not in md
    # EH LIMIT master tagged for the adapter's single-leg EH path.
    assert md["order_type"] == "limit"
    assert md["extended_hours"] == "true"
    assert md["session"] == "AM"
    assert md["oms_v2_mirror_eh"] == "true"
    # limit = min(ask*(1+0.3%), cap) = min(2.51*1.003, 2.525) = 2.5175 -> tick-round DOWN to 2.51.
    assert float(md["limit_price"]) == pytest.approx(2.51, abs=1e-9)
    assert float(md["limit_price"]) <= 2.50 * 1.01 + 1e-9  # never above the cap
    assert md["path"] == "ATR Flip"


@pytest.mark.asyncio
async def test_eh_limit_is_capped_at_max_cross(eh):
    """The EH limit never exceeds schwab_fill*(1+max_cross%) even when ask*(1+buffer) would."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True, mirror_eh_on=True)
    # ask 2.52 is inside cap (2.525); buffered = 2.52*1.003 = 2.5276 > cap -> clamp to cap 2.525 -> 2.52.
    _seed_fresh_ask(service, "VSME", ask=2.52)
    await _mirror(service, symbol="VSME", fill=2.50)

    md = adapter.requests[0].metadata
    assert float(md["limit_price"]) <= 2.525 + 1e-9
    assert float(md["limit_price"]) == pytest.approx(2.52, abs=1e-9)


@pytest.mark.asyncio
async def test_eh_ask_past_cap_abandons(eh):
    """An ask that has run past the max-cross cap -> ABANDON: no submit (no chase)."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True, mirror_eh_on=True)
    _seed_fresh_ask(service, "VSME", ask=2.60)  # 2.60 > cap 2.525 (fill 2.50 +1%)
    await _mirror(service, symbol="VSME", fill=2.50)

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_eh_no_fresh_ask_abandons(eh):
    """No fresh ask in EH -> ABANDON: no blind EH order."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True, mirror_eh_on=True)
    # no _seed_fresh_ask -> no quote for VSME
    await _mirror(service, symbol="VSME", fill=2.50)

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_eh_stale_ask_abandons(eh):
    """A stale ask (older than the EH quote-age) is NOT fresh -> ABANDON in EH."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True, mirror_eh_on=True)
    _seed_fresh_ask(service, "VSME", ask=2.51, age_ms=60_000)  # 60s >> 2000ms default
    await _mirror(service, symbol="VSME", fill=2.50)

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_eh_flag_off_is_market_combo(eh):
    """mirror-EH flag OFF in EH -> byte-identical to today: MARKET master + native-OCO combo
    (the RTH-only mirror the broker 417s in EH — but the code path is unchanged)."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True, mirror_eh_on=False)
    _seed_fresh_ask(service, "VSME", ask=2.51)
    await _mirror(service, symbol="VSME", fill=2.50)

    assert len(adapter.requests) == 1
    md = adapter.requests[0].metadata
    assert adapter.requests[0].order_type == "market"
    assert md["bracket"] == "true"
    assert md["native_oco_bracket"] == "true"
    assert md["bracket_entry_type"] == "MARKET"
    assert "limit_price" not in md
    assert "extended_hours" not in md


# --------------------------------------------------------------------------- RTH unchanged

@pytest.mark.asyncio
async def test_rth_is_market_combo_even_with_eh_flag_on(rth):
    """RTH with mirror-EH ON -> byte-identical: MARKET master + native-OCO combo, ask-anchored
    target/stop. The EH branch never fires in RTH."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True, mirror_eh_on=True)
    _seed_fresh_ask(service, "VSME", ask=3.00)
    await _mirror(service, symbol="VSME", fill=2.50)

    assert len(adapter.requests) == 1
    req = adapter.requests[0]
    assert req.order_type == "market"
    md = req.metadata
    assert md["bracket"] == "true"
    assert md["native_oco_bracket"] == "true"
    assert md["bracket_entry_type"] == "MARKET"
    # Anchored to the ASK (3.00), NOT the Schwab fill (2.50).
    assert float(md["bracket_target_price"]) == pytest.approx(3.00 * 1.02, rel=1e-6)
    assert float(md["bracket_stop_price"]) == pytest.approx(3.00 * 0.95, rel=1e-6)
    assert "limit_price" not in md
    assert "extended_hours" not in md
    assert "oms_v2_mirror_eh" not in md


# --------------------------------------------------------------------------- threshold pin

@pytest.mark.asyncio
async def test_max_cross_cap_threshold_is_pinned(eh):
    """Pin the max-cross cap VALUE: at the DEFAULT 1.0% an ask of 2.5251 (just past cap 2.525)
    ABANDONS; widening the setting to 2.0% (cap 2.55) lets the SAME ask through. A mutation of the
    max_cross default flips this pair -> the test catches it."""
    # DEFAULT 1.0% -> cap 2.525; ask 2.5251 is past it -> abandon.
    adapter1 = _CaptureAdapter()
    svc1 = _oms(adapter=adapter1, mirror_on=True, mirror_eh_on=True)
    _seed_fresh_ask(svc1, "VSME", ask=2.5251)
    await _mirror(svc1, symbol="VSME", fill=2.50)
    assert adapter1.requests == []  # abandoned at 1.0%

    # 2.0% -> cap 2.55; the SAME ask now fits -> a LIMIT is built.
    adapter2 = _CaptureAdapter()
    svc2 = _oms(adapter=adapter2, mirror_on=True, mirror_eh_on=True)
    svc2.settings.oms_v2_eh_entry_max_cross_pct = 2.0
    _seed_fresh_ask(svc2, "VSME", ask=2.5251)
    await _mirror(svc2, symbol="VSME", fill=2.50)
    assert len(adapter2.requests) == 1
    assert adapter2.requests[0].order_type == "limit"
