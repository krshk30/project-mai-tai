from __future__ import annotations

import ast
import asyncio
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from project_mai_tai.broker_adapters.webull import configured_webull_accounts
from project_mai_tai.db.models import OrbPaperEvent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.orb_paper_store import ORB_PAPER_ACCOUNT_NAME, OrbPaperDecision
from project_mai_tai.runtime_registry import (
    configured_broker_account_registrations,
    strategy_registration_map,
)
from project_mai_tai.services.orb_app import OrbService, _PendingPaperEntry, _SymbolState
from project_mai_tai.settings import Settings


class _PaperStore:
    def __init__(self) -> None:
        self.decisions: list[OrbPaperDecision] = []

    def append(self, decision: OrbPaperDecision) -> bool:
        self.decisions.append(decision)
        return True


class _NoIntentRedis:
    async def xadd(self, stream: str, *_args: object, **_kwargs: object) -> None:
        if stream.endswith("strategy-intents"):
            raise AssertionError("ORB paper path reached the OMS intent stream")


@pytest.mark.parametrize("provider", ("webull", "schwab", "alpaca"))
def test_orb_runtime_is_hard_coded_paper_even_with_hostile_broker_settings(
    provider: str,
) -> None:
    settings = Settings(
        orb_enabled=True,
        orb_broker_account_name="live:orb",
        orb_broker_provider=provider,
        webull_account_id="WB-LIVE",
    )

    registration = strategy_registration_map(settings)["orb"]
    assert registration.account_name == ORB_PAPER_ACCOUNT_NAME
    assert registration.execution_mode == "paper"
    assert registration.runtime_kind == "orb_paper"
    assert registration.metadata["provider"] == "none"
    assert all(item.name != ORB_PAPER_ACCOUNT_NAME for item in configured_broker_account_registrations(settings))
    assert "live:orb" not in configured_webull_accounts(settings)


def test_default_orb_service_does_not_read_a_checkout_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "MAI_TAI_ORB_ENABLED=true\nMAI_TAI_SCHWAB_CLIENT_SECRET=must-not-load\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MAI_TAI_ORB_ENABLED", raising=False)
    monkeypatch.delenv("MAI_TAI_SCHWAB_CLIENT_SECRET", raising=False)

    service = OrbService(redis_client=_NoIntentRedis())  # type: ignore[arg-type]

    assert service.settings.orb_enabled is False
    assert service.settings.schwab_client_secret is None


def test_v2_webull_registration_does_not_depend_on_orb() -> None:
    settings = Settings(
        orb_enabled=True,
        orb_broker_account_name="live:orb",
        orb_broker_provider="webull",
        strategy_schwab_1m_v2_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        webull_account_id="WB-LIVE",
    )

    accounts = {item.name: item for item in configured_broker_account_registrations(settings)}
    assert accounts["live:orb"].provider == "webull"
    assert configured_webull_accounts(settings)["live:orb"].account_id == "WB-LIVE"


def test_orb_modules_have_no_trade_intent_broker_or_dynamic_dispatch_path() -> None:
    paths = (
        Path("src/project_mai_tai/services/orb_app.py"),
        Path("src/project_mai_tai/orb_paper_store.py"),
    )
    sources = {path: path.read_text() for path in paths}

    for path, source in sources.items():
        tree = ast.parse(source)
        modules = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        call_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not any("broker_adapter" in module for module in modules), path
        assert "importlib" not in modules, path
        assert not ({"submit_order", "cancel_order", "replace_order"} & call_attributes), path
        assert "TradeIntentEvent" not in source, path
        assert "TradeIntentPayload" not in source, path
        assert "strategy-intents" not in source, path
        assert "virtual_positions" not in source, path
        assert "oms_managed_positions" not in source, path


def test_orb_paper_tape_has_no_live_order_position_or_account_identity() -> None:
    table = OrbPaperEvent.__table__

    assert table.name == "orb_paper_events"
    assert not table.foreign_keys
    assert not ({"broker_order_id", "broker_account_id", "fill_id", "position_id"} & set(table.c))


def test_real_orb_signal_records_paper_decision_without_redis_dispatch() -> None:
    store = _PaperStore()
    service = OrbService(
        settings=Settings(redis_stream_prefix="test"),
        redis_client=_NoIntentRedis(),  # type: ignore[arg-type]
        paper_store=store,  # type: ignore[arg-type]
    )
    service._running_high_mode = False
    service._reclaim_mode = False
    service._states["DAIC"] = _SymbolState(attempts=1, pending=True)
    service._pending_paper_entries = [
        _PendingPaperEntry("DAIC", 3.21, service._session_open_utc(), 1)
    ]

    asyncio.run(service._record_pending_paper_entries())

    assert len(store.decisions) == 1
    assert store.decisions[0].event_type == "PAPER_ENTRY_DECISION"
    assert store.decisions[0].symbol == "DAIC"
    assert store.decisions[0].entry_price == Decimal("3.21")
    assert service._states["DAIC"].paper_entries == 1
    assert service._states["DAIC"].pending is False


def test_paper_decision_identity_is_stable_for_an_exact_retry() -> None:
    service = OrbService(settings=Settings(), redis_client=_NoIntentRedis())  # type: ignore[arg-type]
    observed_at = service._session_open_utc()

    first = service._build_paper_entry_decision("DAIC", 3.21, observed_at=observed_at, attempt=1)
    retry = service._build_paper_entry_decision("DAIC", 3.21, observed_at=observed_at, attempt=1)

    assert first.event_key == retry.event_key


def test_paper_decision_identity_separates_repeated_observations() -> None:
    service = OrbService(settings=Settings(), redis_client=_NoIntentRedis())  # type: ignore[arg-type]
    observed_at = service._session_open_utc()

    first = service._build_paper_entry_decision("DAIC", 3.21, observed_at=observed_at, attempt=1)
    second = service._build_paper_entry_decision("DAIC", 3.21, observed_at=observed_at, attempt=2)

    assert first.event_key != second.event_key


def test_signal_path_refuses_a_non_paper_builder_result() -> None:
    store = _PaperStore()
    service = OrbService(
        settings=Settings(redis_stream_prefix="test"),
        redis_client=_NoIntentRedis(),  # type: ignore[arg-type]
        paper_store=store,  # type: ignore[arg-type]
    )
    service._states["DAIC"] = _SymbolState(attempts=1, pending=True)
    service._pending_paper_entries = [
        _PendingPaperEntry("DAIC", 3.21, service._session_open_utc(), 1)
    ]
    service._build_paper_entry_decision = lambda *args, **kwargs: object()  # type: ignore[method-assign]

    try:
        asyncio.run(service._record_pending_paper_entries())
    except RuntimeError as exc:
        assert "broker-disconnected" in str(exc)
    else:  # pragma: no cover - mutation makes this branch fail the test
        raise AssertionError("non-paper output crossed the ORB service boundary")
    assert store.decisions == []


@pytest.mark.parametrize("account_name", ("live:orb", "paper:orb", "live:schwab_1m_v2"))
def test_manual_orb_intent_is_refused_by_oms_before_any_dependency_is_touched(
    account_name: str,
) -> None:
    service = OmsRiskService.__new__(OmsRiskService)
    service.logger = logging.getLogger("test-oms-orb-paper-refusal")
    service._load_global_manual_stop_symbols = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("ORB intent crossed the OMS refusal boundary")
    )
    event = TradeIntentEvent(
        source_service="manual-test",
        payload=TradeIntentPayload(
            strategy_code="orb",
            broker_account_name=account_name,
            symbol="DAIC",
            side="buy",
            quantity=Decimal("1"),
            intent_type="open",
            reason="MANUAL_BROKER_ATTEMPT",
            metadata={},
        ),
    )

    assert asyncio.run(service.process_trade_intent(event)) == []


def test_the_paper_account_LABEL_itself_is_pinned_not_just_its_symbol() -> None:
    """⛔⭐⭐ ORBLBL — the gap that sat open three days (2026-09-06 -> 09-09).

    Every other assertion in this file compares `registration.account_name` against the SYMBOL
    `ORB_PAPER_ACCOUNT_NAME`. Both sides move together, so renaming the VALUE turns nothing red:

        ORB_PAPER_ACCOUNT_NAME = "paper:orb"   ->   "live:orb"     # whole file still green

    That is a check that cannot come out false. It was correctly sized as a LABELLING risk and not
    a reachability one - `orb_paper_store` imports no broker adapter, so a rename cannot conjure a
    broker route - but the label is not cosmetic:

      * `live:orb` is a REAL Webull margin account. It is the account v2's dual-broker fan-out
        routes every Webull leg through, and `MAI_TAI_ORB_ENABLED` stays true precisely so that
        account keeps existing. A paper label colliding with it would make the observer's modeled
        decisions indistinguishable from real-money fan-out fills in the durable record.
      * Reading `live:orb` fills as ORB activity has already happened once (2026-07-29) and cost a
        wrong report that ORB was live when it had not run for six days.

    So the literal is pinned here, and the `paper:` prefix with it.
    """
    assert ORB_PAPER_ACCOUNT_NAME == "paper:orb"
    assert ORB_PAPER_ACCOUNT_NAME.startswith("paper:")
    assert ORB_PAPER_ACCOUNT_NAME != "live:orb"
    assert not ORB_PAPER_ACCOUNT_NAME.startswith("live:")


def test_the_registration_carries_the_literal_paper_label_end_to_end() -> None:
    """The same pin one layer out: a rename must also turn the REGISTRATION red, not just the
    constant. Hostile broker settings, exactly as the parametrised test above supplies them."""
    settings = Settings(
        orb_enabled=True,
        orb_broker_account_name="live:orb",
        orb_broker_provider="webull",
        webull_account_id="WB-LIVE",
    )
    registration = strategy_registration_map(settings)["orb"]
    assert registration.account_name == "paper:orb"
    assert all(
        item.name != "paper:orb" for item in configured_broker_account_registrations(settings)
    ), "the paper label must never become a configured BROKER account"
