"""Case-specific controlled boundary checks, NOT historical native-flat proof.

Actual source-bound market replay needs its own reviewed bars/quotes packet.
No old WFF/boot/cost modules are collected or called by these controls.
"""

from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from decimal import Decimal
from zoneinfo import ZoneInfo
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.brokerdecides1_sync import account_names
from project_mai_tai.db.models import Base, BrokerAccount, Strategy
from tests.line_restore_acceptance_factory import make_line_restore_case
from tools.l12_worx_olox_replay import CASES, consume_book, evidence_summary, guard_receipt, install_recording_emitters, load_e_packet, load_evidence, load_olox_partial_market, make_causal_market_case, replay_market_packet, replay_original_callbacks, select_available_bar_rows


CONTRACT = json.loads((Path(__file__).parents[1] / "fixtures/l12_worx_olox_case_contract_20261022.json").read_text())
RETAINED = Path(__file__).parents[1] / "fixtures/l12_worx_olox_retained"


def retained_input(name):
    defaults = {"L12_E_PACKET_ROOT": RETAINED,
                "L12_OLOX_RETAINED_BARS": RETAINED / "SLOTCAP-OLOX-BARS-20261021.jsonl"}
    return os.environ.get(name, str(defaults[name]))


def retained_cases():
    explicit = os.environ.get("L12_CASE_RETAINED_EXPORT")
    if explicit is not None:
        return load_evidence(explicit)  # Full122 bytes/universe remain hash-checked.
    raw = (RETAINED / "case_subset.json").read_bytes()
    assert sha256(raw).hexdigest() == "09958ee138421b6e91c987eefe1586a09aa59318fa20421e55b288d22510bb05"
    subset = json.loads(raw)
    assert subset["source_sha256"] == "31147bd20bb1f0c3e9a997bc45f9dbf41ef5fb71a6a58bec4b9c0a9e30c6c5f6"
    assert subset["source_record_count"] == 122 and set(subset["cases"]) == set(CASES)
    assert subset["scope"] == "EXACT_SELECTED_RECORDS_NOT_FULL_122_EXPORT_OR_NATIVE_BOOK"
    rows = {}
    for case_id, (index, symbol, day, flip) in CASES.items():
        selected = subset["cases"][case_id]
        assert selected["source_pointer"] == f"/{index}"
        row = selected["record"]
        assert (row["sym"], row["day"], row["flip"]) == (symbol, day, flip)
        rows[case_id] = row
    return rows


@pytest.fixture
def journal_factory(tmp_path):
    """CONTROLLED current journal, never historical native or economic proof."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'l12-journal.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Strategy(code="schwab_1m_v2", name="CONTROLLED L12 publication"))
        session.add_all(BrokerAccount(name=binding["account"], provider=binding["provider"],
            environment="test") for binding in CONTRACT["controlled_account_bindings"])
    try:
        yield factory
    finally:
        engine.dispose()


def make_case(record):
    # The bar label and actual line time remain separate; this is a controlled
    # decision clock, not a claim that native books were read at that time.
    day = record["day"]
    now = int(datetime.fromisoformat(f"{day}T{record['flip_bar_et']}:00")
        .replace(tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000) + 60_000
    short = int(datetime.fromisoformat(f"{day}T{record['previous_sell_et']}:00")
        .replace(tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    case = make_line_restore_case(symbol=record["symbol"], now_ms=now, settings_overrides={
        "strategy_schwab_1m_v2_enabled": True,
        "strategy_schwab_1m_v2_brokerdecides1_enabled": True,
        "strategy_schwab_1m_v2_brokerdecides1_max_age_ms": 20_000,
        # Native response identities must not inherit the deliberate safe paper/
        # empty production defaults. These are controlled test labels, no wires.
        "strategy_schwab_1m_v2_account_name": CONTRACT["controlled_account_bindings"][0]["account"],
        "strategy_schwab_1m_v2_webull_account_name": CONTRACT["controlled_account_bindings"][1]["account"],
        "strategy_schwab_1m_v2_broker_provider": "simulated",
        "oms_adapter": "simulated",
    })
    state = case.strategy.watchlist_state(case.symbol)
    state.atr_short_flip_bar_ts = short
    # CONTROLLED OLD cap-only blocker: L12 is an exception to this note, not a
    # universal native gate. This is not historical cap/owner/native evidence
    # and supplies no bars, line-complete attestation or entry permission.
    state.cw_seed_cap_watch_start_ms = short
    state.cw_resting_taken = state.cw_reclaim_taken = True
    return case


def controlled_packet(case, mode):
    started = case.now_ms - (20_001 if mode == "stale" else 100)
    bindings = CONTRACT["controlled_account_bindings"]
    if tuple(binding["account"] for binding in bindings) != account_names(case.settings):
        raise ValueError("controlled_account_settings_mismatch")
    books = []
    for index, binding in enumerate(bindings):
        books.append({
            "account": binding["account"], "provider": binding["provider"], "wire_started_ms": started,
            "wire_completed_ms": started + 5,
            "working": [case.symbol] if mode == "working" and index == 1 else [],
            "positions": [case.symbol] if mode == "position" and index == 1 else [],
            "complete": mode != "partial" or index == 0,
            "reason": "CONTROLLED_EXTERNAL_RESPONSE_NOT_HISTORICAL",
        })
    return {
        "evidence_kind": "CONTROLLED", "source_ref": "new-test-fixture-not-native-history",
        "epoch": "L12_WORX_OLOX_CONTROLLED", "started_ms": started,
        "completed_ms": started + 10, "books": books,
        "inflight": [case.symbol] if mode == "CANCELACK" else [],
        "filled_segments": [[case.symbol, case.strategy.watchlist_state(case.symbol).atr_short_flip_bar_ts]] if mode == "filled" else [],
        "local_complete": True,
        "reason": "CONTROLLED_NEGATIVE_OR_FLAT_NOT_HISTORICAL",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("record", CONTRACT["cases"], ids=lambda row: row["case_id"])
@pytest.mark.parametrize("mode", ["flat", "working", "position", "stale", "CANCELACK", "partial", "missing", "filled"])
async def test_actual_publisher_consumer_card_on_each_case(record, mode):
    case = make_case(record)
    state = case.strategy.watchlist_state(case.symbol)
    before = (state.cw_resting_taken, state.cw_reclaim_taken, state.retry_one_segment_id,
        state.retry_one_closes_in_segment, state.flip_owner_opportunity_id)
    await consume_book(case, None if mode == "missing" else controlled_packet(case, mode))
    receipt = guard_receipt(case)
    assert receipt["native_card"]["allowed"] is (mode == "flat")
    if mode == "working":
        assert receipt["native_card"]["reason"] == "native_working_order"
    if mode == "position":
        assert receipt["native_card"]["reason"] == "native_position"
    if mode == "stale":
        assert receipt["native_card"]["reason"] == "snapshot_stale"
    if mode == "CANCELACK":
        assert receipt["native_card"]["reason"] == "own_inflight_UNKNOWN_or_working"
    if mode == "filled":
        assert receipt["native_card"]["reason"] == "buy_fill_in_segment"
    assert receipt["draft_count"] == 0  # Native card alone is NOT a market BUY.
    assert before == (state.cw_resting_taken, state.cw_reclaim_taken, state.retry_one_segment_id,
        state.retry_one_closes_in_segment, state.flip_owner_opportunity_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("record", CONTRACT["cases"], ids=lambda row: row["case_id"])
async def test_lostack_reservation_survives_missing_book(record):
    case = make_case(record)
    await consume_book(case, controlled_packet(case, "flat"))
    memory = case.strategy._brokerdecides1_memory
    memory.reserve(case.symbol, f"CONTROLLED_LOSTACK:{record['case_id']}")
    await consume_book(case, None)
    assert not guard_receipt(case)["native_card"]["allowed"]
    assert memory.reservation_tokens() == frozenset({f"CONTROLLED_LOSTACK:{record['case_id']}"})
    assert not case.schwab.intents and not case.webull.intents


@pytest.mark.asyncio
@pytest.mark.parametrize("record", CONTRACT["cases"], ids=lambda row: row["case_id"])
async def test_incomplete_market_packet_cannot_manufacture_case_buy(record):
    case = make_case(record)
    result = await replay_market_packet(case, {"source_ref": "retained-122case-row"})
    assert result["status"] == "UNKNOWN"
    assert {"events", "coverage", "settings_source"} == set(result["missing"])
    assert result["steps"] == []
    assert not case.schwab.intents and not case.webull.intents


def test_actual_retained_export_binding_keeps_missing_native_books_unknown():
    rows = retained_cases()
    assert set(rows) == set(CASES)
    for case_id, row in rows.items():
        receipt = evidence_summary(case_id, row)
        assert receipt["historical_native_book"] == "UNKNOWN_NOT_STORED_IN_THIS_EXPORT"
        assert receipt["historical_buy_acceptance"] == "UNKNOWN_NOT_EXECUTED"
    assert rows["WORX-20261020-1027"]["P"] == 4.11
    assert rows["OLOX-20261021-1329"]["P"] == 1.03
    assert any("trig=1.0200" in line for line in rows["OLOX-20261021-1329"]["bot_log_lines"])


def test_case_contract_does_not_turn_census_trigger_into_arm_override():
    record = CONTRACT["cases"][1]
    assert record["census_P"] == "1.03"
    assert record["logged_arm_trigger"] == "1.0200"
    assert record["historical_native_book"] == "UNKNOWN"


@pytest.mark.parametrize("record", CONTRACT["cases"], ids=lambda row: row["case_id"])
def test_controlled_account_identity_is_explicit_not_paper_or_zip_inference(record):
    case = make_case(record)
    packet = controlled_packet(case, "flat")
    assert account_names(case.settings) == ("live:schwab_1m_v2", "live:orb")
    assert [(book["account"], book["provider"]) for book in packet["books"]] == [
        ("live:schwab_1m_v2", "schwab"), ("live:orb", "webull"),
    ]
    assert case.settings.strategy_schwab_1m_v2_broker_provider == "simulated"
    assert case.settings.oms_adapter == "simulated"
    assert packet["evidence_kind"] == "CONTROLLED"


@pytest.mark.asyncio
@pytest.mark.parametrize("record", CONTRACT["cases"], ids=lambda row: row["case_id"])
@pytest.mark.parametrize("mode", ["working", "position", "stale", "CANCELACK", "missing"])
async def test_real_emitter_refuses_negative_book_before_xadd(record, mode, journal_factory):
    from project_mai_tai.brokerdecides1_prewire_authority import SIDECAR
    from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft
    from project_mai_tai.v2_removed_wait import RemovedWaitStore

    case = make_case(record)
    transport = install_recording_emitters(case)
    await consume_book(case, None if mode == "missing" else controlled_packet(case, mode))
    case.bot.session_factory = journal_factory
    store = RemovedWaitStore(journal_factory, prewire_authority=True)
    case.strategy._removed_wait_dispatch_persist = store.record_dispatch
    token = f"CONTROLLED-NEGATIVE-{uuid4().hex}"
    opportunity = str(case.strategy.watchlist_state(case.symbol).atr_short_flip_bar_ts)
    # Earn the real current journal capability so this control reaches the
    # native-card refusal, rather than stopping at a missing-authority guard.
    capability = store.record_dispatch({"schema_version": 1, "strategy_code": "schwab_1m_v2",
        "brokerdecides_exception": True, "symbol": case.symbol, "opportunity_id": opportunity,
        "account_names": list(account_names(case.settings)), "kind": "attempt",
        "attempt_token": token, "account_name": case.schwab.broker_account_name})
    # Explicit synthetic boundary stimulus, NOT a bar-generated/historical draft.
    draft = TradeIntentDraft(case.symbol, "buy", "open", Decimal("1"),
        "CONTROLLED_NEGATIVE_SEND_FENCE_ONLY", {"fanout_segment_id": opportunity,
            "clearwait_dispatch_token": token})
    setattr(draft, SIDECAR, capability)
    case.schwab.publication_boundary_factory = case.bot._brokerdecides1_publication_boundary
    case.schwab.on_prewire_refusal = case.bot._brokerdecides1_prewire_refusal
    with pytest.raises(ValueError, match="brokerdecides1_hold"):
        await case.schwab.emit(draft)
    assert transport.publications == []
    assert not case.schwab.intents and not case.webull.intents


@pytest.mark.parametrize("case_id,expected_trigger", [
    ("WORX-20261020-1027", "4.1400"), ("OLOX-20261021-1329", "1.0200"),
])
def test_e_packet_binds_original_arm_not_census_or_1325_quote_wait(case_id, expected_trigger):
    manifest = json.loads((Path(__file__).parents[1] / "fixtures/l12_worx_olox_e_packet_20261022.json").read_text())
    packet = load_e_packet(retained_input("L12_E_PACKET_ROOT"), case_id, manifest)
    assert packet["issues"] == []
    assert len(packet["arms"]) == 1
    assert packet["arms"][0]["trigger"] == expected_trigger
    assert packet["native_book"] == "UNKNOWN_NEVER_STORED"
    assert packet["asof_working_or_position"] == "UNKNOWN_NOT_DERIVED_FROM_FINAL_STATUSES"


@pytest.mark.parametrize("case_id", tuple(CASES))
def test_e_packet_preserves_fill_order_intent_reason_and_account_links(case_id):
    manifest = json.loads((Path(__file__).parents[1] / "fixtures/l12_worx_olox_e_packet_20261022.json").read_text())
    packet = load_e_packet(retained_input("L12_E_PACKET_ROOT"), case_id, manifest)
    assert len(packet["fill_order_intent_links"]) == len(packet["fills_raw"])
    for link in packet["fill_order_intent_links"]:
        assert link["order_raw"] is not None
        assert link["fill_raw"]["order_id"] == link["order_raw"]["id"]
        assert link["fill_raw"]["account"] == link["order_raw"]["account"]
        assert link["order_raw"]["intent_id"]
        assert link["order_raw"]["reason"]


def test_e_olox_original_1329_partial_bars_are_not_full_prefix_or_native_clock():
    packet = load_olox_partial_market(retained_input("L12_OLOX_RETAINED_BARS"))
    assert packet["case_id"] == "OLOX-20261021-1329"
    assert packet["coverage"]["complete"] is False
    assert packet["events"][-1]["row"]["bar_time"] == "2026-10-21T17:29:00+00:00"
    assert packet["events"][-1]["phase"] == "live"
    assert packet["events"][0]["row"]["bar_time"] == "2026-10-21T11:20:00+00:00"
    assert all("NOT_NATIVE_CALLBACK_TIME" in event["clock_basis"] for event in packet["events"])
    assert not any(event["kind"] == "quote" for event in packet["events"])
    assert len(packet["events"]) + len(packet["excluded_source_rows"]) == packet["source_row_count"]
    assert any(row["reason"] == "HISTORY_AVAILABLE_AFTER_TARGET_PROXY" for row in packet["excluded_source_rows"])
    assert "EARLIER_BARS_AVAILABLE_ONLY_AFTER_TARGET" in packet["coverage"]["missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["flat", "working", "position", "stale", "LOSTACK", "CANCELACK"])
async def test_e_olox_real_market_callbacks_preserve_partial_scope_under_native_branches(mode, journal_factory):
    packet = load_olox_partial_market(retained_input("L12_OLOX_RETAINED_BARS"))
    case = make_causal_market_case(packet)
    # The current consumer rechecks reserved tokens in the durable journal.
    # An actual empty CONTROLLED table cannot retire the LOSTACK reservation.
    case.bot.session_factory = journal_factory
    assert case.now_ms == packet["events"][0]["received_ms"]
    assert case.strategy.watchlist_state(case.symbol).atr_short_flip_bar_ts == 0
    result = await replay_original_callbacks(case, packet, native_mode=mode)
    assert result["status"] == "UNKNOWN_PARTIAL_MARKET_CALLBACK_DIAGNOSTIC"
    assert len(result["steps"]) == len(packet["events"])
    assert result["steps"][-1]["raw_pointer"] == packet["events"][-1]["source_pointer"]
    assert result["native_evidence"] == "CONTROLLED_NOT_HISTORICAL"
    assert result["original_market_coverage"]["complete"] is False
    assert result["initialization"]["initial_short_ms"] == 0
    assert result["initialization"]["initial_bar_count"] == 0
    # A real callback-derived SHORT must exist; absent witness is a failure,
    # never a skip/conditional PASS supplied by the independent line hold.
    witnessed = [step for step in result["steps"]
        if step["atr_state_from_callback"] == "short"
        and 0 < step["short_ms_from_callback"] <= step["observed_ms"]
        and step["line_segment_from_callback"] == step["short_ms_from_callback"]]
    assert witnessed, "UNEXERCISED: no causal SHORT from retained partial bars"
    expected_reason = {
        "flat": "broker_flat_no_inflight_no_segment_fill",
        "working": "native_working_order", "position": "native_position",
        "stale": "snapshot_stale", "LOSTACK": "local_send_reserved",
        "CANCELACK": "own_inflight_UNKNOWN_or_working",
    }[mode]
    scoped = [step for step in witnessed if step["native_card"]["reason"] != "outside_L12_exception"]
    assert all(step["native_card"]["reason"] == expected_reason for step in scoped)
    assert all(step["native_card"]["allowed"] is (mode == "flat") for step in scoped)
    assert all(step["native_card"]["allowed"] is True for step in witnessed if step not in scoped)
    # Do not invent an OLD cap/owner preimage in this causal market replay.
    # Verify the delivered book itself even when the actual strategy correctly
    # leaves this ordinary callback outside its narrow L12 exception.
    native = case.strategy._brokerdecides1_memory.assess(accounts=account_names(case.settings),
        symbol=case.symbol, segment=witnessed[-1]["line_segment_from_callback"], now_ms=case.now_ms,
        max_age_ms=case.settings.strategy_schwab_1m_v2_brokerdecides1_max_age_ms)
    assert native.reason == expected_reason and native.allowed is (mode == "flat")
    assert all(not step["line_ready"] for step in witnessed)
    # A missing prefix/quotes/boot cannot become qualified flat BUY acceptance.
    assert not case.schwab.intents and not case.webull.intents
    if mode == "LOSTACK":
        assert "CONTROLLED_LOSTACK_CALLBACK" in case.strategy._brokerdecides1_memory.reservation_tokens()


@pytest.mark.asyncio
async def test_e_worx_absent_original_bars_does_not_borrow_olox_or_seed_permission():
    result = await replay_original_callbacks(None, {"case_id": "WORX-20261020-1027", "events": []}, native_mode="flat")
    assert result == {"status": "UNKNOWN", "missing": ["ORIGINAL_MARKET_CALLBACK_EVENTS"], "steps": []}


@pytest.mark.asyncio
async def test_e_partial_market_needs_explicit_diagnostic_scope_not_positive_acceptance():
    packet = load_olox_partial_market(retained_input("L12_OLOX_RETAINED_BARS"))
    del packet["purpose"]
    case = make_causal_market_case(packet)
    result = await replay_market_packet(case, packet)
    assert result == {"status": "UNKNOWN", "missing": ["complete_market_coverage"], "steps": []}
    assert not case.schwab.intents and not case.webull.intents


def test_e_callback_initialization_has_no_future_short_cap_or_watch_preimage():
    packet = load_olox_partial_market(retained_input("L12_OLOX_RETAINED_BARS"))
    case = make_causal_market_case(packet)
    state = case.strategy.watchlist_state(case.symbol)
    assert case.now_ms == packet["events"][0]["received_ms"]
    assert case._l12_causal_initialization["asof_ms"] == case.now_ms
    assert state.atr_short_flip_bar_ts == 0 and not state.bars
    assert state.flip_owner_opportunity_id == state.fanout_segment_id == 0
    assert (state.cw_resting_taken, state.cw_reclaim_taken) == (
        case._l12_causal_initialization["initial_cap_notes"]["resting_taken"],
        case._l12_causal_initialization["initial_cap_notes"]["reclaim_taken"],
    )
    assert state.cw_seed_cap_watch_start_ms <= case.now_ms


@pytest.mark.asyncio
async def test_e_future_census_time_case_is_rejected_before_callback_or_publication():
    packet = load_olox_partial_market(retained_input("L12_OLOX_RETAINED_BARS"))
    future_case = make_case(CONTRACT["cases"][1])
    with pytest.raises(ValueError, match="causal_case_initialization_required_no_clock_rewind"):
        await replay_original_callbacks(future_case, packet, native_mode="flat")
    assert not future_case.schwab.intents and not future_case.webull.intents


def test_e_causal_cutoff_keeps_late_old_minute_out_of_earlier_decision_and_records_it():
    rows = [
        (1, {"bar_time": "2026-10-21T16:58:00+00:00", "created_at": "2026-10-21T18:40:12.109118+00:00"}),
        (2, {"bar_time": "2026-10-21T17:28:00+00:00", "created_at": "2026-10-21T17:29:02+00:00"}),
        (3, {"bar_time": "2026-10-21T17:29:00+00:00", "created_at": "2026-10-21T17:30:02.343675+00:00"}),
    ]
    included, excluded, cutoff = select_available_bar_rows(rows, target_bar_time="2026-10-21T17:29:00+00:00")
    assert [item[0] for item in included] == [2, 3]
    assert excluded == [{"line_number": 1, **rows[0][1], "reason": "HISTORY_AVAILABLE_AFTER_TARGET_PROXY"}]
    assert cutoff["created_at"] == "2026-10-21T17:30:02.343675+00:00"
    assert len(included) + len(excluded) == len(rows)


def test_e_causal_cutoff_preserves_microseconds_instead_of_millisecond_floor():
    rows = [
        (1, {"bar_time": "2026-10-21T17:27:00+00:00", "created_at": "2026-10-21T17:30:02.343674+00:00"}),
        (2, {"bar_time": "2026-10-21T17:28:00+00:00", "created_at": "2026-10-21T17:30:02.343676+00:00"}),
        (3, {"bar_time": "2026-10-21T17:29:00+00:00", "created_at": "2026-10-21T17:30:02.343675+00:00"}),
    ]
    included, excluded, _ = select_available_bar_rows(rows, target_bar_time="2026-10-21T17:29:00+00:00")
    assert [item[0] for item in included] == [1, 3]
    assert [item["line_number"] for item in excluded] == [2]


def test_e_causal_cutoff_still_refuses_missing_or_duplicate_original_target():
    row = {"bar_time": "2026-10-21T17:29:00+00:00", "created_at": "2026-10-21T17:30:02+00:00"}
    for rows in ([], [(1, row), (2, dict(row))]):
        with pytest.raises(ValueError, match="original_1329_unique_target_bar_required"):
            select_available_bar_rows(rows, target_bar_time=row["bar_time"])
