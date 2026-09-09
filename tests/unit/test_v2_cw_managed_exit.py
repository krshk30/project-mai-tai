"""Confirmed-window (variant CW) OMS managed exit — PRs #2/#3, end-to-end.

Drives `_evaluate_v2_managed_exit` with the CW flag on through the REAL emit path
(SimulatedBrokerAdapter) on the SQLite schema, mirroring test_v2_managed_exit.py. Proves
the CW exit REPLACES the scale/floor ladder: full close at +2% (CW_TARGET) or -5%
(CW_HARD_STOP) or a bar-close flip (CW_FLIP, armed via an account-neutral ATR observation),
and NO exit between the two bounds when no flip is pending. Also proves the dispatcher
arms the in-memory pending set only when CW is enabled.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    BrokerAccount,
    BrokerOrder,
    OmsManagedPosition,
    Strategy,
    SystemIncident,
    TradeIntent,
)
from project_mai_tai.oms import service as service_module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

ACCT = "paper:schwab_1m_v2"
WEBULL_ACCT = "paper:webull_v2"
SYM = "VSME"


class _FakeRedis:
    async def xadd(self, *a, **kw):
        return b"1-1"


class _ConfirmationAdapter(SimulatedBrokerAdapter):
    def __init__(self, *, armed: bool = False, release_result: str = "released") -> None:
        super().__init__()
        self.armed = armed
        self.release_result = release_result
        self.release_calls: list[tuple[str, str]] = []

    async def fetch_armed_native_oco_symbols(
        self, broker_account_name: str, symbols: list[str]
    ) -> set[str]:
        del broker_account_name
        return set(symbols) if self.armed else set()

    async def release_native_oco_for_close(
        self, broker_account_name: str, entry_broker_order_id: str
    ) -> str:
        self.release_calls.append((broker_account_name, entry_broker_order_id))
        return self.release_result


def _make_sf() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    tables = [t for t in Base.metadata.sorted_tables
              if t.name not in ("market_trade_ticks", "market_quote_ticks")]
    Base.metadata.create_all(engine, tables=tables)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _svc(
    sf,
    *,
    cw: bool = True,
    floor: bool = False,
    dual: bool = False,
    adapter: SimulatedBrokerAdapter | None = None,
) -> OmsRiskService:
    settings = Settings(
        oms_v2_exit_management_enabled=True,
        oms_v2_exit_close_on_fill_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=cw,
        strategy_schwab_1m_v2_account_name=ACCT,
        strategy_schwab_1m_v2_webull_account_name=WEBULL_ACCT,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=dual,
        oms_v2_cw_floor_exit_enabled=floor,
    )
    svc = OmsRiskService(
        settings, redis_client=_FakeRedis(), session_factory=sf,
        broker_adapter=adapter or SimulatedBrokerAdapter(),
    )
    with sf() as s:
        svc.store.ensure_strategy(s, "schwab_1m_v2", name="v2")
        svc.store.ensure_broker_account(s, ACCT, provider="simulated", environment="test")
        if dual:
            svc.store.ensure_broker_account(
                s, WEBULL_ACCT, provider="simulated", environment="test"
            )
        s.commit()
    return svc


def _arm(
    svc,
    sf,
    *,
    entry=10.0,
    qty=100,
    entry_time: datetime | None = None,
    account: str = ACCT,
) -> None:
    with sf() as s:
        svc.store.create_managed_position(
            s, strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol=SYM, entry_price=Decimal(str(entry)), quantity=qty, entry_path="ATR Flip",
            entry_time=entry_time,
        )
        s.commit()
    svc._managed_v2_symbols.add((ACCT, SYM))


def _bind_flip(svc, sf, *, bar_time_ms: int | None = None) -> int:
    bar_time_ms = bar_time_ms or int((datetime.now(UTC) - timedelta(seconds=90)).timestamp() * 1000)
    svc._arm_cw_flip_pending(
        (ACCT, SYM),
        bar_time_ms=bar_time_ms,
        managed_row_id=_open_row_id(sf),
    )
    return bar_time_ms


def _atr_observation_message(bar_time_ms: int) -> dict[str, str]:
    return {
        "data": json.dumps(
            {
                "event_type": "v2_atr_sell_observation",
                "symbol": SYM,
                "bar_time_ms": bar_time_ms,
                "decision_id": f"atr-sell:{SYM}:{bar_time_ms}",
            }
        )
    }


def _record_filled_entry(sf, *, broker_order_id: str = "entry-order-1") -> None:
    with sf() as s:
        strategy = s.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        account = s.scalar(select(BrokerAccount).where(BrokerAccount.name == ACCT))
        assert strategy is not None and account is not None
        s.add(
            BrokerOrder(
                intent_id=None,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=f"entry-client-{broker_order_id}",
                broker_order_id=broker_order_id,
                symbol=SYM,
                side="buy",
                order_type="stop_limit",
                time_in_force="day",
                quantity=Decimal("100"),
                status="filled",
                payload={"native_oco_bracket": "true"},
            )
        )
        s.commit()


def _quote(
    svc,
    bid: float,
    *,
    ask: float | None = None,
    received_at: datetime | None = None,
) -> None:
    svc._latest_quotes_by_symbol[SYM] = {
        "bid": bid, "ask": bid + 0.01 if ask is None else ask,
        "received_at": received_at or datetime.now(UTC),
    }


def _row(sf) -> OmsManagedPosition | None:
    with sf() as s:
        return s.scalar(select(OmsManagedPosition).where(OmsManagedPosition.symbol == SYM))


def _sell_intents(sf) -> list[TradeIntent]:
    with sf() as s:
        return list(s.scalars(select(TradeIntent).where(
            TradeIntent.symbol == SYM, TradeIntent.side == "sell")).all())


def _ref(i: TradeIntent) -> Decimal:
    return Decimal(i.payload["metadata"]["reference_price"])


@pytest.mark.asyncio
async def test_cw_target_full_close_at_plus_2pct():
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)                       # >= +2% target (10.20)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    i = intents[0]
    assert i.intent_type == "close" and Decimal(str(i.quantity)) == Decimal("100")
    assert i.reason.endswith("CW_TARGET")
    assert _ref(i) == Decimal("10.2000")          # target LEVEL, not the 10.25 bid
    assert _row(sf).current_quantity == 0 or _row(sf).status == "closed"


@pytest.mark.asyncio
async def test_cw_hard_stop_full_close_at_minus_5pct():
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.40)                          # <= -5% stop (9.50)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_HARD_STOP")
    assert _ref(intents[0]) == Decimal("9.5000")   # stop LEVEL


@pytest.mark.asyncio
async def test_cw_no_exit_between_bounds_without_flip():
    # -5% < bid < +2% and no flip pending -> the CW exit does NOTHING (proves the
    # scale/floor ladder is NOT running under CW).
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.90)                          # -1%
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert _sell_intents(sf) == []
    assert _row(sf).status == "open"


@pytest.mark.asyncio
async def test_cw_flip_full_close_at_bid():
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    now = datetime.now(UTC)
    bar_time_ms = int((now - timedelta(seconds=90)).timestamp() * 1000)
    _arm(svc, sf, entry=10.0, qty=100, entry_time=now - timedelta(seconds=120))
    # Arm the flip via the dispatcher event, then a quote inside the bounds closes it.
    await svc._handle_stream_message(_atr_observation_message(bar_time_ms))
    assert (ACCT, SYM) in svc._cw_flip_pending
    _quote(svc, bid=9.90)                          # inside bounds, but flip pending
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_FLIP")
    assert _ref(intents[0]) == Decimal("9.9000")   # fills at the bid
    assert (ACCT, SYM) not in svc._cw_flip_pending  # consumed


@pytest.mark.asyncio
async def test_atr_observation_closes_each_accounts_managed_quantity() -> None:
    """C3: unequal legs close from their own managed rows, never strategy quantity."""

    sf = _make_sf()
    svc = _svc(sf, cw=True, dual=True)
    now = datetime.now(UTC)
    _arm(svc, sf, qty=2, entry_time=now - timedelta(minutes=5), account=ACCT)
    _arm(svc, sf, qty=1, entry_time=now - timedelta(minutes=5), account=WEBULL_ACCT)
    bar_time_ms = int((now - timedelta(seconds=90)).timestamp() * 1000)

    await svc._handle_stream_message(_atr_observation_message(bar_time_ms))
    _quote(svc, bid=9.90)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    await svc._evaluate_v2_managed_exit(WEBULL_ACCT, SYM)

    with sf() as session:
        rows = session.execute(
            select(BrokerAccount.name, TradeIntent.quantity)
            .join(TradeIntent, TradeIntent.broker_account_id == BrokerAccount.id)
            .where(TradeIntent.symbol == SYM, TradeIntent.side == "sell")
        ).all()
    assert {name: int(quantity) for name, quantity in rows} == {ACCT: 2, WEBULL_ACCT: 1}


@pytest.mark.asyncio
async def test_NUR_1329_position_never_inherits_the_1234_flip(monkeypatch) -> None:
    """NUR 2026-09-08: an ownerless 12:34 decision must not sell the 13:29 position."""
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    clock = {"now": datetime(2026, 9, 8, 16, 34, 2, tzinfo=UTC)}
    monkeypatch.setattr(service_module, "utcnow", lambda: clock["now"])
    bar_time_ms = int(datetime(2026, 9, 8, 16, 33, tzinfo=UTC).timestamp() * 1000)

    await svc._handle_stream_message(_atr_observation_message(bar_time_ms))
    assert (ACCT, SYM) not in svc._cw_flip_pending

    clock["now"] = datetime(2026, 9, 8, 17, 29, 46, tzinfo=UTC)
    _arm(svc, sf, entry=3.24, qty=2, entry_time=clock["now"])
    _quote(svc, bid=3.20, received_at=clock["now"])
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert _sell_intents(sf) == []
    assert _row_status_open(sf), "the 13:29 position must not inherit the 12:34 decision"


@pytest.mark.asyncio
async def test_cw_flip_refuses_a_position_opened_after_the_decision_bar(monkeypatch) -> None:
    """A delayed stream event must not bind an old bar to the row that happens to be open now."""
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    bar_time = datetime(2026, 9, 8, 16, 33, tzinfo=UTC)
    clock = {"now": bar_time + timedelta(seconds=90)}
    monkeypatch.setattr(service_module, "utcnow", lambda: clock["now"])
    _arm(svc, sf, entry=3.24, qty=2, entry_time=bar_time + timedelta(seconds=70))

    await svc._handle_stream_message(
        _atr_observation_message(int(bar_time.timestamp() * 1000))
    )

    assert (ACCT, SYM) not in svc._cw_flip_pending
    assert _row_status_open(sf)


@pytest.mark.asyncio
async def test_cw_flip_without_a_bar_identity_never_arms() -> None:
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=3.24, qty=2)

    await svc._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_atr_sell_observation",
                    "symbol": SYM,
                }
            )
        }
    )

    assert (ACCT, SYM) not in svc._cw_flip_pending
    assert _row_status_open(sf)


@pytest.mark.asyncio
async def test_cw_flip_decision_expires_before_it_can_act(monkeypatch) -> None:
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    bar_time = datetime(2026, 9, 8, 16, 33, tzinfo=UTC)
    clock = {"now": bar_time + timedelta(seconds=62)}
    monkeypatch.setattr(service_module, "utcnow", lambda: clock["now"])
    _arm(svc, sf, entry=3.24, qty=2, entry_time=bar_time - timedelta(minutes=5))

    await svc._handle_stream_message(
        _atr_observation_message(int(bar_time.timestamp() * 1000))
    )
    assert (ACCT, SYM) in svc._cw_flip_pending

    clock["now"] = bar_time + timedelta(seconds=181)
    _quote(svc, bid=3.20, received_at=clock["now"])
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert _sell_intents(sf) == []
    assert (ACCT, SYM) not in svc._cw_flip_pending
    assert _row_status_open(sf)


@pytest.mark.asyncio
async def test_cw_flip_decision_refuses_a_replacement_position(monkeypatch) -> None:
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    bar_time = datetime(2026, 9, 8, 16, 33, tzinfo=UTC)
    clock = {"now": bar_time + timedelta(seconds=62)}
    monkeypatch.setattr(service_module, "utcnow", lambda: clock["now"])
    _arm(svc, sf, entry=3.24, qty=2, entry_time=bar_time - timedelta(minutes=5))
    row_a = _open_row_id(sf)

    await svc._handle_stream_message(
        _atr_observation_message(int(bar_time.timestamp() * 1000))
    )
    _close_open_row(sf)
    _match_production_partial_index(sf)
    _arm(svc, sf, entry=3.24, qty=2, entry_time=clock["now"])
    assert _open_row_id(sf) != row_a

    _quote(svc, bid=3.20, received_at=clock["now"])
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert _sell_intents(sf) == []
    assert (ACCT, SYM) not in svc._cw_flip_pending
    assert _row_status_open(sf), "a decision for row A must never migrate to replacement row B"


@pytest.mark.asyncio
async def test_cw_flip_decision_still_executes_for_its_own_position(monkeypatch) -> None:
    """Control: ownership must discriminate, not disable the ATR exit."""
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    bar_time = datetime(2026, 9, 8, 16, 33, tzinfo=UTC)
    clock = {"now": bar_time + timedelta(seconds=62)}
    monkeypatch.setattr(service_module, "utcnow", lambda: clock["now"])
    _arm(svc, sf, entry=3.24, qty=2, entry_time=bar_time - timedelta(minutes=5))

    await svc._handle_stream_message(
        _atr_observation_message(int(bar_time.timestamp() * 1000))
    )
    _quote(svc, bid=3.20, received_at=clock["now"])
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_FLIP")


@pytest.mark.asyncio
async def test_cw_flip_releases_an_armed_native_oco_before_closing() -> None:
    """MOBX 2026-09-08: a bar-close flip must not be swallowed by native-OCO stand-down."""
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True, release_result="released")
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    _record_filled_entry(sf)
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = datetime.now(UTC)
    _bind_flip(svc, sf)
    _quote(svc, bid=9.90)

    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.release_calls == [(ACCT, "entry-order-1")]
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_FLIP")
    assert (ACCT, SYM) not in svc._cw_flip_pending


@pytest.mark.asyncio
async def test_cw_flip_never_sells_when_native_oco_release_is_unanswerable() -> None:
    """Uncertain cancellation keeps the broker bracket authoritative and cannot retry per quote."""
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True, release_result="unanswerable")
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    _record_filled_entry(sf)
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = datetime.now(UTC)
    _bind_flip(svc, sf)

    for bid in (9.90, 9.89, 9.88):
        _quote(svc, bid=bid)
        await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.release_calls == [(ACCT, "entry-order-1")]
    assert _sell_intents(sf) == []
    assert (ACCT, SYM) not in svc._cw_flip_pending


@pytest.mark.asyncio
async def test_cw_flip_reports_uncovered_when_close_fails_after_oco_release(capsys) -> None:
    """A released bracket plus a refused close must never disappear as a quiet one-shot."""
    sf = _make_sf()
    adapter = _RejectingConfirmationAdapter(armed=True, release_result="released")
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    _record_filled_entry(sf)
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = datetime.now(UTC)
    _bind_flip(svc, sf)
    _quote(svc, bid=9.90)

    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    output = capsys.readouterr().out

    assert adapter.release_calls == [(ACCT, "entry-order-1")]
    assert len(_sell_intents(sf)) == 1
    assert "[OMS-V2-CW-FLIP-UNCOVERED]" in output
    assert f"managed_row={_open_row_id(sf)}" in output
    assert "oco_released=1 close_outcome=refused" in output
    assert (ACCT, SYM) not in svc._cw_flip_pending
    with sf() as session:
        incidents = list(session.scalars(select(SystemIncident)).all())
    assert len(incidents) == 1
    assert incidents[0].severity == "critical"
    assert incidents[0].status == "open"
    assert "CW flip UNCOVERED" in incidents[0].title
    assert incidents[0].payload == {
        "source": "oms_v2_cw_flip_uncovered",
        "broker_account_name": ACCT,
        "symbol": SYM,
        "managed_row_id": _open_row_id(sf),
        "oco_released": True,
        "close_outcome": "refused",
    }


@pytest.mark.asyncio
async def test_cw_flip_refuses_release_if_position_changes_before_target_read() -> None:
    """A replacement between the snapshot and release read must keep its own OCO untouched."""
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True, release_result="released")
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    row_a = _open_row_id(sf)
    _record_filled_entry(sf, broker_order_id="entry-order-A")
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = datetime.now(UTC)
    _bind_flip(svc, sf)
    _quote(svc, bid=9.90)

    original_run_db = svc._run_db
    swapped = False

    async def _replace_before_release_read(fn, *, commit=True):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if getattr(fn, "__name__", "") == "_read_release_target" and not swapped:
            swapped = True
            _close_open_row(sf)
            _match_production_partial_index(sf)
            _arm(svc, sf, entry=11.0, qty=100)
            _record_filled_entry(sf, broker_order_id="entry-order-B")
        return await original_run_db(fn, commit=commit)

    svc._run_db = _replace_before_release_read  # type: ignore[method-assign]

    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert swapped, "the replacement must occur after snapshot A and before the release read"
    assert _open_row_id(sf) != row_a
    assert _row_status_open(sf)
    assert adapter.release_calls == []
    assert _sell_intents(sf) == []


@pytest.mark.asyncio
async def test_armed_native_oco_keeps_owning_target_even_with_a_pending_flip() -> None:
    """The exception is only for the ATR flip; it must not revive the whole software ladder."""
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True)
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = datetime.now(UTC)
    _bind_flip(svc, sf)
    _quote(svc, bid=10.25)

    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.release_calls == []
    assert _sell_intents(sf) == []
    assert (ACCT, SYM) in svc._cw_flip_pending


@pytest.mark.asyncio
async def test_cw_flip_defers_when_the_oco_already_resolved_by_fill() -> None:
    """An OCO child that already sold wins; the flip closes bookkeeping and emits no second sell."""
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True, release_result="resolved_by_fill")
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    _record_filled_entry(sf)
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = datetime.now(UTC)
    _bind_flip(svc, sf)
    _quote(svc, bid=9.90)

    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.release_calls == [(ACCT, "entry-order-1")]
    assert _sell_intents(sf) == []
    assert _row(sf).status == "closed"
    assert (ACCT, SYM) not in svc._managed_v2_symbols


@pytest.mark.asyncio
async def test_cw_flip_does_not_sell_a_replacement_position_after_oco_release() -> None:
    """The broker await for position A must never let its flip close replacement position B."""
    sf = _make_sf()

    class _ReplaceDuringRelease(_ConfirmationAdapter):
        async def release_native_oco_for_close(
            self, broker_account_name: str, entry_broker_order_id: str
        ) -> str:
            self.release_calls.append((broker_account_name, entry_broker_order_id))
            _close_open_row(sf)
            _match_production_partial_index(sf)
            _arm(svc, sf, entry=11.0, qty=100)
            return "released"

    adapter = _ReplaceDuringRelease(armed=True)
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    row_a = _open_row_id(sf)
    _record_filled_entry(sf)
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = datetime.now(UTC)
    _bind_flip(svc, sf)
    _quote(svc, bid=9.90)

    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.release_calls == [(ACCT, "entry-order-1")]
    assert _open_row_id(sf) != row_a
    assert _row_status_open(sf)
    assert (ACCT, SYM) in svc._managed_v2_symbols
    assert _sell_intents(sf) == []


@pytest.mark.asyncio
async def test_cw_target_takes_precedence_over_pending_flip():
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _bind_flip(svc, sf)
    _quote(svc, bid=10.25)                          # +2% AND flip pending -> target wins
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_TARGET")
    assert (ACCT, SYM) not in svc._cw_flip_pending


@pytest.mark.asyncio
async def test_dispatcher_arms_pending_only_when_cw_enabled():
    sf = _make_sf()
    on = _svc(sf, cw=True)
    now = datetime.now(UTC)
    bar_time_ms = int((now - timedelta(seconds=90)).timestamp() * 1000)
    _arm(on, sf, entry_time=now - timedelta(seconds=120))
    await on._handle_stream_message(_atr_observation_message(bar_time_ms))
    assert (ACCT, SYM) in on._cw_flip_pending

    off = _svc(_make_sf(), cw=False)
    await off._handle_stream_message(_atr_observation_message(bar_time_ms))
    assert (ACCT, SYM) not in off._cw_flip_pending


@pytest.mark.asyncio
async def test_confirmation_exit_releases_native_oco_before_close() -> None:
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True)
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    await svc._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_confirmation_exit",
                    "symbol": SYM,
                    "broker_account_name": ACCT,
                    "source_fill_id": "fill-1",
                    "broker_order_id": "entry-order-1",
                    "evaluated_at_ms": "1",
                    "atr_state": "short",
                    "should_exit": True,
                    "entry_slot": "first",
                }
            )
        }
    )
    _quote(svc, bid=9.9)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.release_calls == [(ACCT, "entry-order-1")]
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CONFIRMATION_EXIT")


@pytest.mark.asyncio
async def test_confirmation_exit_never_arms_for_reclaim() -> None:
    sf = _make_sf()
    svc = _svc(sf, cw=True, adapter=_ConfirmationAdapter())
    await svc._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_confirmation_exit",
                    "symbol": SYM,
                    "broker_account_name": ACCT,
                    "evaluated_at_ms": "1",
                    "atr_state": "short",
                    "should_exit": True,
                    "entry_slot": "reclaim",
                }
            )
        }
    )
    assert (ACCT, SYM) not in svc._confirmation_exit_pending


@pytest.mark.asyncio
async def test_confirmation_exit_refuses_once_when_oco_release_is_unconfirmed() -> None:
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True, release_result="unanswerable")
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    svc._confirmation_exit_pending[(ACCT, SYM)] = {
        "evaluated_at_ms": "1",
        "broker_order_id": "entry-order-1",
        # production always stamps the episode it was accepted into; the double must too
        "bound_managed_row_id": _open_row_id(sf),
    }
    _quote(svc, bid=9.9)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert _sell_intents(sf) == []
    assert (ACCT, SYM) not in svc._confirmation_exit_pending, (
        "an unanswerable one-shot must fail closed without polling the broker on every quote"
    )


@pytest.mark.asyncio
async def test_confirmation_exit_defers_to_an_oco_leg_that_already_filled() -> None:
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True, release_result="resolved_by_fill")
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    svc._confirmation_exit_pending[(ACCT, SYM)] = {
        "source_fill_id": "fill-1",
        "evaluated_at_ms": "1",
        "broker_order_id": "entry-order-1",
        "bound_managed_row_id": _open_row_id(sf),
    }
    closed: list[tuple[str, str]] = []

    async def close_resolved(acct: str, symbol: str, *, detail=None, expected_row_id=None) -> None:
        # the double must accept what production now passes, or it hides a TypeError in the OMS
        closed.append((acct, symbol, expected_row_id))

    svc._close_resolved_oco_managed_row = close_resolved  # type: ignore[method-assign]
    _quote(svc, bid=9.9)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.release_calls == [(ACCT, "entry-order-1")]
    assert closed == [(ACCT, SYM, _open_row_id(sf))], (
        "the resolved close must name the episode the identity check verified"
    )
    assert _sell_intents(sf) == []
    assert (ACCT, SYM) not in svc._confirmation_exit_pending


# --- CW floor exit (2026-07-14): arm at +2%, ride, close on fall-back-to-floor ---


@pytest.mark.asyncio
async def test_cw_floor_arms_at_target_then_exits_on_fallback():
    sf = _make_sf()
    svc = _svc(sf, cw=True, floor=True)
    _arm(svc, sf, entry=10.0, qty=100)                # target/floor = 10.20
    _quote(svc, bid=10.25)                            # reaches +2% -> ARM, no exit
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert _sell_intents(sf) == []
    assert (ACCT, SYM) in svc._cw_floor_armed
    assert _row(sf).status == "open"
    _quote(svc, bid=10.60)                            # rides higher -> still no exit
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert _sell_intents(sf) == []
    _quote(svc, bid=10.20)                            # falls through the persisted +4.5% ratchet
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_FLOOR")
    assert _ref(intents[0]) == Decimal("10.4500")     # the ratcheted floor level, not fixed +2%
    assert (ACCT, SYM) not in svc._cw_floor_armed


@pytest.mark.asyncio
async def test_cw_floor_consumes_bid_ratchet_with_both_fallback_outcomes_reachable():
    sf = _make_sf()
    svc = _svc(sf, cw=True, floor=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)  # arm fixed +2% minimum
    _quote(svc, bid=10.60)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)  # peak +6% -> durable floor +4.5%

    _quote(svc, bid=10.46)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert _sell_intents(sf) == []  # above 10.45 ratchet: keep riding

    _quote(svc, bid=10.44)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_FLOOR")
    assert _ref(intents[0]) == Decimal("10.4500")


@pytest.mark.asyncio
async def test_cw_floor_ignores_wide_ask_bounce_when_bid_does_not_really_move():
    sf = _make_sf()
    svc = _svc(sf, cw=True, floor=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    _quote(svc, bid=10.25, ask=12.00)  # spread bounce: ask jumps, executable bid does not
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    _quote(svc, bid=10.21, ask=10.22)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert _sell_intents(sf) == []
    assert _row(sf).floor_price == Decimal("10.05000000")


@pytest.mark.asyncio
async def test_cw_floor_off_is_hard_target_byte_identical():
    sf = _make_sf()
    svc = _svc(sf, cw=True, floor=False)              # floor OFF -> +2% is a HARD close
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1 and intents[0].reason.endswith("CW_TARGET")
    assert (ACCT, SYM) not in svc._cw_floor_armed


# ═══════════════════════════════════════════════════════════════════════════════════════════
# CONF1 — THE DECISION MUST BELONG TO THE POSITION IT SELLS.  Built on the live IMRN trade,
# 2026-09-04, which is a test with a KNOWN ANSWER.
#
# What happened: the confirmation for the 11:34:11 fill read the 11:35 bar (short) and exited
# that position correctly at 11:36:09. Nothing cleared the pending entry. Forty-three minutes
# later a NEW position opened at 12:18:03, and the SAME stale decision fired against it ~20
# times in 33 seconds — into protective legs placed seconds earlier. 20 refusals, the reject
# ceiling, and 36 minutes with exits suppressed.
#
# ⛔ The bar selection was never at fault: every evaluation on 09-03 and 09-04 used a bar whose
# START was after its fill and read it at that bar's CLOSE. The fault is that a decision had no
# owner and no expiry.
# ═══════════════════════════════════════════════════════════════════════════════════════════

class _RejectingConfirmationAdapter(_ConfirmationAdapter):
    """Sells are REFUSED, exactly as IMRN's were.

    ⛔ This is the difference between a test that can fail and one that cannot. When the sell is
    ACCEPTED it becomes a working exit order, `dedup_active` goes true, and that incidentally pops
    the pending confirmation — so a missing one-shot pop is invisible. Live, every sell was
    REJECTED, no working order ever existed, and the stale decision re-fired on every quote tick.
    """

    async def submit_order(self, request):  # type: ignore[no-untyped-def]
        if request.intent_type == "close":
            return [
                ExecutionReport(
                    event_type="rejected",
                    origin="broker",
                    client_order_id=request.client_order_id,
                    broker_order_id=None,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason="simulated oversold refusal (IMRN replay)",
                    metadata=dict(request.metadata),
                )
            ]
        return await super().submit_order(request)


def _open_row_id(sf) -> str:
    with sf() as s:
        row = s.scalar(
            select(OmsManagedPosition).where(
                OmsManagedPosition.symbol == SYM, OmsManagedPosition.status == "open"
            )
        )
        return str(row.id) if row is not None else ""


def _match_production_partial_index(sf) -> None:
    """SQLite cannot express this index's predicate, so the fixture is STRICTER than production.

    ⛔ In Postgres `uq_oms_managed_positions_open_symbol` is PARTIAL —
    `postgresql_where=text("status = 'open'")` — so a closed row does not block the next episode
    and every episode gets a FRESH row id. SQLite drops the predicate and enforces a full UNIQUE
    on (broker_account_name, symbol), which would make the two-episode replay below impossible to
    write. Dropping the index restores production's semantics; it does not relax a real invariant,
    it removes one SQLite invented.
    """
    from sqlalchemy import text as _text
    with sf() as s:
        s.execute(_text("DROP INDEX IF EXISTS uq_oms_managed_positions_open_symbol"))
        s.commit()


def _close_open_row(sf) -> None:
    """End the episode the way a real close does — the row goes to status=closed."""
    with sf() as s:
        row = s.scalar(
            select(OmsManagedPosition).where(
                OmsManagedPosition.symbol == SYM, OmsManagedPosition.status == "open"
            )
        )
        row.status = "closed"
        row.current_quantity = 0
        s.commit()


@pytest.mark.asyncio
async def test_a_confirmation_decided_for_an_EARLIER_position_must_not_sell_the_next_one():
    """⛔ THE IMRN DEFECT, with its known answer: position B must NOT be sold.

    Position A is entered and its confirmation is armed against A. A closes. B opens on the same
    (account, symbol). The stale decision must be DROPPED, not applied to B.
    """
    sf = _make_sf()
    svc = _svc(sf, cw=True, adapter=_ConfirmationAdapter(release_result="released"))

    _arm(svc, sf, entry=10.0, qty=100)          # position A — the 11:34:11 fill
    row_a = _open_row_id(sf)
    svc._confirmation_exit_pending[(ACCT, SYM)] = {
        "source_fill_id": "fill-A",
        "evaluated_at_ms": "1",
        "broker_order_id": "entry-order-A",
        "bound_managed_row_id": row_a,
    }

    _close_open_row(sf)                          # A exits
    _match_production_partial_index(sf)
    _arm(svc, sf, entry=10.0, qty=100)          # position B — the 12:18:03 entry
    row_b = _open_row_id(sf)
    assert row_b and row_b != row_a, "the replay needs two distinct episodes"

    _quote(svc, bid=9.9)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert _sell_intents(sf) == [], (
        "a confirmation decided for a position that has already closed must never sell the "
        "position that replaced it — this is the IMRN 12:18 sell"
    )
    assert (ACCT, SYM) not in svc._confirmation_exit_pending, "the stale decision must be dropped"
    assert _row_status_open(sf), "position B must still be open"
    # ⛔⭐⭐ codex-2 #897 R1: refusing the SELL is not enough. Reaching the protection reconcile at
    # all RELEASES position B's native OCO — stripping its broker-side stop on the strength of a
    # decision about a position that no longer exists, and then declining to sell it.
    assert svc.broker_adapter.release_calls == [], (
        "a stale confirmation must be dropped BEFORE any OCO release; releasing B's protection is "
        "worse than the sell we were already refusing"
    )


@pytest.mark.asyncio
async def test_a_stale_confirmation_never_reaches_protection_even_when_it_would_RESOLVE_BY_FILL():
    """⛔ The other protection outcome, and the more destructive one.

    `resolved_by_fill` does not merely release — it CLOSES the managed row. Reached with a stale
    decision, it would close position B on the strength of position A's OCO leg having filled.
    """
    sf = _make_sf()
    adapter = _ConfirmationAdapter(release_result="resolved_by_fill")
    svc = _svc(sf, cw=True, adapter=adapter)

    _arm(svc, sf, entry=10.0, qty=100)
    row_a = _open_row_id(sf)
    svc._confirmation_exit_pending[(ACCT, SYM)] = {
        "source_fill_id": "fill-A",
        "evaluated_at_ms": "1",
        "broker_order_id": "entry-order-A",
        "bound_managed_row_id": row_a,
    }
    _close_open_row(sf)
    _match_production_partial_index(sf)
    _arm(svc, sf, entry=10.0, qty=100)          # position B
    assert _open_row_id(sf) != row_a

    closed: list[tuple[str, str]] = []

    async def _close_resolved(a: str, s: str, *, detail=None) -> None:
        closed.append((a, s))

    svc._close_resolved_oco_managed_row = _close_resolved  # type: ignore[method-assign]
    _quote(svc, bid=9.9)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.release_calls == [], "must not reconcile protection for a closed position"
    assert closed == [], "must NOT close position B on position A's OCO fill"
    assert _row_status_open(sf), "position B must still be open"
    assert (ACCT, SYM) not in svc._confirmation_exit_pending


def _row_status_open(sf) -> bool:
    with sf() as s:
        return s.scalar(
            select(OmsManagedPosition).where(
                OmsManagedPosition.symbol == SYM, OmsManagedPosition.status == "open"
            )
        ) is not None


@pytest.mark.asyncio
async def test_a_correctly_bound_confirmation_STILL_exits_its_own_position():
    """⭐ THE CONTROL. Without this, an implementation that never exits passes every test above.

    Same setup, one difference: the pending decision is bound to the position that is open.
    """
    sf = _make_sf()
    svc = _svc(sf, cw=True, adapter=_ConfirmationAdapter(release_result="released"))
    _arm(svc, sf, entry=10.0, qty=100)
    svc._confirmation_exit_pending[(ACCT, SYM)] = {
        "source_fill_id": "fill-A",
        "evaluated_at_ms": "1",
        "broker_order_id": "entry-order-A",
        "bound_managed_row_id": _open_row_id(sf),
    }
    _quote(svc, bid=9.9)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    intents = _sell_intents(sf)
    assert len(intents) == 1, "its own position must still be exited"
    assert intents[0].reason.endswith("CONFIRMATION_EXIT")


@pytest.mark.asyncio
async def test_the_confirmation_exit_fires_EXACTLY_ONCE_across_many_ticks():
    """⛔ THE 20-SELL BURST. The pending entry was never popped after emitting, so once it became
    executable it re-emitted on EVERY quote tick until the reject ceiling stopped it."""
    sf = _make_sf()
    svc = _svc(sf, cw=True, adapter=_RejectingConfirmationAdapter(release_result="released"))
    _arm(svc, sf, entry=10.0, qty=100)
    svc._confirmation_exit_pending[(ACCT, SYM)] = {
        "source_fill_id": "fill-A",
        "evaluated_at_ms": "1",
        "broker_order_id": "entry-order-A",
        "bound_managed_row_id": _open_row_id(sf),
    }

    for _ in range(8):                            # eight quote ticks, as a live spread would give
        _quote(svc, bid=9.9)
        await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert len(_sell_intents(sf)) == 1, (
        "the confirmation exit is a ONE-SHOT decision; re-emitting per tick is what produced 20 "
        "rejected sells in 33 seconds on IMRN"
    )
    assert (ACCT, SYM) not in svc._confirmation_exit_pending


@pytest.mark.asyncio
async def test_a_confirmation_with_no_open_position_is_refused_at_accept_not_armed():
    """⛔ An unbound confirmation is a decision looking for a victim. Refuse it at the door."""
    sf = _make_sf()
    svc = _svc(sf, cw=True)                       # no managed row at all

    await svc._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_confirmation_exit",
                    "symbol": SYM,
                    "broker_account_name": ACCT,
                    "source_fill_id": "fill-orphan",
                    "evaluated_at_ms": "1",
                    "atr_state": "short",
                    "should_exit": True,
                    "entry_slot": "first",
                }
            )
        }
    )

    assert (ACCT, SYM) not in svc._confirmation_exit_pending, (
        "with no open position there is nothing to exit; arming it is how it finds the NEXT one"
    )


@pytest.mark.asyncio
async def test_accepting_a_confirmation_binds_it_to_the_open_position():
    """The accept path must stamp the episode identity, or the check above has nothing to compare."""
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=10.0, qty=100)
    expected = _open_row_id(sf)

    await svc._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_confirmation_exit",
                    "symbol": SYM,
                    "broker_account_name": ACCT,
                    "source_fill_id": "fill-B",
                    "evaluated_at_ms": "1",
                    "atr_state": "short",
                    "should_exit": True,
                    "entry_slot": "first",
                }
            )
        }
    )

    pending = svc._confirmation_exit_pending.get((ACCT, SYM))
    assert pending is not None
    assert pending["bound_managed_row_id"] == expected


@pytest.mark.asyncio
async def test_a_position_REPLACED_DURING_the_broker_await_is_not_closed_by_the_old_fill():
    """⛔⭐⭐ codex-2 #897 R2 — the interleaving the identity check alone cannot cover.

    The identity check passes while position A is open. Then
    `_reconcile_confirmation_exit_protection` awaits a BROKER round trip, and inside that await A
    closes and B opens. `resolved_by_fill` comes back and the close, scoped only by
    (account, symbol), would shut B on the strength of A's OCO leg filling.

    This test performs the replacement INSIDE the await, which is the only way to reach it.
    """
    sf = _make_sf()

    class _ReplaceDuringAwait(_ConfirmationAdapter):
        """Ends episode A and opens B while the OMS is awaiting the release result."""

        def __init__(self) -> None:
            super().__init__(release_result="resolved_by_fill")
            self.swapped = False

        async def release_native_oco_for_close(self, broker_account_name: str, entry_broker_order_id: str) -> str:
            self.release_calls.append((broker_account_name, entry_broker_order_id))
            if not self.swapped:
                self.swapped = True
                _close_open_row(sf)                     # A ends
                _match_production_partial_index(sf)
                _arm(svc, sf, entry=10.0, qty=100)      # B opens — inside the await
            return self.release_result

    adapter = _ReplaceDuringAwait()
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)                  # position A
    row_a = _open_row_id(sf)
    svc._confirmation_exit_pending[(ACCT, SYM)] = {
        "source_fill_id": "fill-A",
        "evaluated_at_ms": "1",
        "broker_order_id": "entry-order-A",
        "bound_managed_row_id": row_a,
    }

    _quote(svc, bid=9.9)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.swapped, "the replacement must have happened inside the await, or this proves nothing"
    row_b = _open_row_id(sf)
    assert row_b and row_b != row_a, "B must be a distinct episode"
    assert _row_status_open(sf), (
        "position B must still be OPEN — it was closed on position A's OCO fill, which is the "
        "replacement-during-await race"
    )
    assert (ACCT, SYM) in svc._managed_v2_symbols, (
        "refusing the close must not silently remove the replacement from managed-exit evaluation"
    )
    assert _sell_intents(sf) == []
