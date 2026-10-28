"""Bounded offline service lifecycle with real durable loaders and line callback.

Not continuous-loop/native acceptance. Never accepts a production DB factory.
All global patches are scoped and same-process calls must remain serial.
"""

import asyncio
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import inspect
import json
from pathlib import Path
import sys
from time import perf_counter_ns as _manual_perf_ns
from unittest.mock import patch

from sqlalchemy import text
from project_mai_tai.brokerdecides1 import decode
from project_mai_tai.brokerdecides1_sync import refresh_consumer
from project_mai_tai.services import schwab_1m_v2_bot as module
from tools.l12_today_close_replay import stage_replay

_RUNNING = False
C11929_CONTROLLED_VERSION = "C11929_CONTROLLED_NATIVE_BOOT_V1"
LOOPS = ("_heartbeat_loop", "_state_publish_loop", "_country_reload_loop",
    "_rpg_handoff_loop", "_line_restoration_loop", "_line_source_events_loop",
    "_keepwatch_history_loop", "_scanner_consumer_loop", "_position_poll_loop",
    "_primary_order_events_loop", "_fanout_outcome_loop", "_task_liveness_loop")
STORES = ("_configure_fanout_identity_store", "_configure_flip_entry_ownership_store",
    "_configure_restart_rest_store", "_configure_country_router",
    "_configure_removed_wait_store", "_recover_soft_rest_boot", "_configure_fanout_outcome_journal")
CLOCK_MODULES = ("fanout_segment_store", "v2_flip_entry_ownership", "v2_restart_rest_store",
    "v2_removed_wait", "falseflip1_runtime", "atr_country_route", "fanout_outcome_consumer")
L12_ONLY_LOOPS = ("_heartbeat_loop", "_state_publish_loop", "_country_reload_loop",
    "_rpg_handoff_loop", "_line_restoration_loop", "_line_source_events_loop",
    "_scanner_consumer_loop", "_position_poll_loop", "_primary_order_events_loop",
    "_fanout_outcome_loop", "_task_liveness_loop")


def prepare_recorded_timeline(*, mapping, recorded_names, chronology_by_name,
                              observed_names, file_rows_by_name, adapter_pins,
                              data_sha256, file_port_sha256):
    """C1 16:39 input seam only; does not initialize stores or claim boot PASS.

    Caller must supply the reviewed recorded population, not derive it from
    successful L11 case outcomes. Original nonce/copy/role guards remain in
    replay_service_boot; none of these mapped records replace native headers.
    """
    from tools.l12_recorded_boot_timeline import (
        ADAPTER_PINS, DATA_SHA, FILE_PORT_SHA, RecordedSessionMap,
        map_file_history_rows, timeline_denominator)
    if (type(mapping) is not RecordedSessionMap or adapter_pins != ADAPTER_PINS
            or data_sha256 != DATA_SHA or file_port_sha256 != FILE_PORT_SHA):
        raise ValueError("exact_reviewed_L11_recorded_adapter_required")
    population = timeline_denominator(recorded_names=recorded_names,
        chronology_by_name=chronology_by_name, observed_names=observed_names)
    if (type(file_rows_by_name) is not dict
            or set(file_rows_by_name) - set(population["names"])):
        raise ValueError("recorded_history_outside_watched_population")
    histories, unknown = {}, []
    for name in population["names"]:
        if name not in file_rows_by_name or not file_rows_by_name[name]:
            unknown.append(name)
        else:
            histories[name] = map_file_history_rows(file_rows_by_name[name], mapping)
    return dict(status="RECORDED_RETIMED_INPUTS_ONLY_TIMELINE_EXECUTION_UNRUN",
        mapping=mapping.receipt(), population=population, histories=histories,
        missing_history=unknown, missing_native_facts=population["names"],
        chronology=deepcopy(chronology_by_name),
        seed_N_of_N_by_0700="UNRUN", old_session_entry_blocks="UNRUN",
        first_BUY_flip_by_name={name: "UNRUN" for name in population["names"]},
        roll_0400=dict(MANSTOP_empty="UNRUN", Mirror4_retired="UNKNOWN_UNLESS_COMMITTED_FIRST"),
        actual_copy_guard="REQUIRED_UNCHANGED_AT_SERVICE_BOUNDARY",
        owner_scheduler="PENDING_MULTI_NAME_EXTENSION_NOT_SINGLE_SYMBOL_CLOCK_REUSE",
        permission_granted=False)


def prepare_recorded_fleet_timeline(*, blobs, mapping, adapter_pins, file_port_sha256,
                                    ruling_1720_sha256, poll_interval_ms, phase_ms):
    """Existing FILE adapter, exact Friday18 packet, no invented initial members.

    External polls/phase must be supplied and match actual Settings at execution;
    neither the seed bar limit nor a complete native book is defaulted here.
    """
    from tools.l12_recorded_boot_timeline import (
        ADAPTER_PINS, FILE_PORT_SHA, FLEET_FILES, _digest, decode_friday_fleet,
        map_file_history_rows, timeline_denominator)
    if adapter_pins != ADAPTER_PINS or file_port_sha256 != FILE_PORT_SHA:
        raise ValueError("exact_reviewed_L11_FILE_model_required")
    _digest(ruling_1720_sha256)
    if (type(poll_interval_ms) is not int or poll_interval_ms <= 0
            or type(phase_ms) is not int or not 0 <= phase_ms < poll_interval_ms):
        raise ValueError("explicit_actual_Settings_cadence_and_phase_required")
    fleet = decode_friday_fleet(blobs, mapping)
    records = [row for rows in fleet["chronology"].values() for row in rows]
    records.sort(key=lambda row: (row["original_ns"], int(row["event_id"])))
    events = [dict(kind="scanner_delta", clock_ns=row["original_ns"], watchlist=[row["symbol"]],
        raw_ref=row["raw_ref"], raw_sha256=row["raw_sha256"]) for row in records]
    anchor = 1792742400000
    cutoff = anchor + 16 * 3600000
    count = len(range(anchor + 60000 + phase_ms, cutoff + 1, poll_interval_ms))
    if count + len(events) + 1 > 100000:
        raise ValueError("finite_predeclared_timeline_envelope_exceeded_no_growth")
    for at in range(anchor + 60000 + phase_ms, cutoff + 1, poll_interval_ms):
        events.append(dict(kind="history_poll", clock_ns=at * 1000000, watchlist=None,
            raw_ref="C11720_DECLARED_POLL_GRID_NOT_ORIGINAL_CALLBACK", raw_sha256=ruling_1720_sha256))
    events.append(dict(kind="observe_end", clock_ns=cutoff * 1000000, watchlist=None,
        raw_ref="C11720_DECLARED_END_NOT_ORIGINAL_CALLBACK", raw_sha256=ruling_1720_sha256))
    events.sort(key=lambda row: (row["clock_ns"], {"scanner_delta": 0, "history_poll": 1, "observe_end": 2}[row["kind"]]))
    population = timeline_denominator(recorded_names=fleet["names"],
        chronology_by_name=fleet["chronology"], observed_names=[])
    prepared = dict(status="FRIDAY18_RETIMED_INPUTS_NOT_BOOT_PASS", mapping=mapping.receipt(),
        population=population, histories={name: map_file_history_rows(rows, mapping)
            for name, rows in fleet["file_rows"].items()},
        missing_history=[name for name, rows in fleet["file_rows"].items() if not rows],
        source_inputs_actual=dict(FLEET_FILES), ruling_1720_sha256=ruling_1720_sha256,
        poll_interval_ms=poll_interval_ms, phase_ms=phase_ms,
        scanner_records_by_ref={row["raw_ref"]: row for row in records},
        original19_case_plan="NOT_USED_AS_FRIDAY_POPULATION", permission_granted=False)
    return dict(prepared=prepared, fleet=fleet, events=events,
        pending_bar_limit="UNKNOWN_NO_DEFAULT", seed_denominator=18,
        missing_confirm_names=fleet["missing_confirm_names"], native_permission="UNKNOWN")


def prepare_c11929_controlled_native(*, accounts, modeled_boot_ms, variant,
        working_provider=None, working_symbol=None):
    """Explicit modeled fixtures, never raw responses or native permission.

    Returned model timestamps satisfy the APP data type, NOT a wire interval.
    Both native completeness and local completeness remain False.
    """
    from project_mai_tai.brokerdecides1 import AdmissionSnapshot, NativeAccountBook, encode
    from tools.l12_close_asof_contract import NATIVE
    from tools.l12_recorded_boot_timeline import FLEET_NAMES
    if (type(accounts) is not dict or set(accounts) != {"schwab", "webull"}
            or any(type(value) is not str or not value.startswith("live:") for value in accounts.values())
            or len(set(accounts.values())) != 2 or type(modeled_boot_ms) is not int or modeled_boot_ms <= 0
            or variant not in {"EMPTY_FLAT", "ONE_WORKING_BUY"}
            or variant == "EMPTY_FLAT" and (working_provider is not None or working_symbol is not None)
            or variant == "ONE_WORKING_BUY" and (working_provider not in accounts or working_symbol not in FLEET_NAMES)):
        raise ValueError("C11929_exact_controlled_variant_accounts_and_modeled_clock_required")
    blobs, roles = {}, {}
    for role, provider in NATIVE.items():
        working = ([working_symbol] if variant == "ONE_WORKING_BUY"
            and provider == working_provider and role.endswith("working_orders") else [])
        body = dict(version=C11929_CONTROLLED_VERSION, purpose="CONTROLLED_NOT_NATIVE_RESPONSE",
            role=role, provider=provider, account=accounts[provider], modeled_boot_ms=modeled_boot_ms,
            working_buy_symbols=working, gross_position_symbols=[], complete=False,
            http_status=None, request_started_ms=None, response_completed_ms=None,
            native_permission="UNKNOWN")
        reference = "C11929-CONTROLLED/" + role + ".json"
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        blobs[reference] = raw
        roles[role] = dict(reference=reference, sha256=sha256(raw).hexdigest(),
            origin="CONTROLLED_NOT_NATIVE_RESPONSE", clock_domain="MODELED_BOOT_MS_NOT_WIRE_CLOCK")
    books = tuple(NativeAccountBook(account=accounts[provider], provider=provider,
        wire_started_ms=modeled_boot_ms, wire_completed_ms=modeled_boot_ms,
        working=frozenset((working_symbol,)) if provider == working_provider else frozenset(),
        positions=frozenset(), complete=False, reason="C11929_CONTROLLED_UNKNOWN_NOT_NATIVE")
        for provider in ("schwab", "webull"))
    snapshot = AdmissionSnapshot(epoch="C11929-CONTROLLED-NOT-PUBLISHER-EPOCH", generation=1,
        started_ms=modeled_boot_ms, completed_ms=modeled_boot_ms, books=books,
        inflight=frozenset(), filled_segments=frozenset(), local_complete=False,
        reason="C11929_CONTROLLED_UNKNOWN_NOT_NATIVE")
    raw = encode(snapshot).encode()
    reference = "C11929-CONTROLLED/admission_snapshot.json"
    blobs[reference] = raw
    roles["admission_snapshot"] = dict(reference=reference, pointer="", sha256=sha256(raw).hexdigest(),
        origin="CONTROLLED_UNKNOWN_ENTRY_PERMISSION", clock_domain="MODELED_BOOT_MS_NOT_WIRE_CLOCK")
    return dict(version=C11929_CONTROLLED_VERSION, variant=variant, accounts=dict(accounts),
        modeled_boot_ms=modeled_boot_ms, working_provider=working_provider, working_symbol=working_symbol,
        publisher_epoch=snapshot.epoch, artifacts=roles, blobs=blobs,
        native_permission="UNKNOWN", actual_native_acquisition=False)


def observe_combined_seed_state(case, *, causal_observer=None):
    """Observe real ON predicates only; never seed, set readiness or send IO.

    This is one timestamped sample, NOT a 04:00->07:00/first-flip certificate.
    N includes retained KEEPWATCH observations, not subscription-only held exits.
    """
    bot = case.bot
    if (type(bot) is not module.SchwabV2BotService or not getattr(bot, "_keepwatch_enabled", False)
            or getattr(case.settings, "strategy_schwab_1m_v2_keepwatch1_enabled", False) is not True
            or getattr(case.settings, "strategy_schwab_1m_v2_brokerdecides1_enabled", False) is not True):
        raise ValueError("actual_combined_ON_service_required_for_seed_observation")
    now_ms = bot.strategy._now_ms()
    if type(now_ms) is not int or case.now_ms != now_ms:
        raise ValueError("seed_observation_clock_mismatch_no_retiming")
    names = sorted(module.SchwabV2BotService._observation_symbols(bot))
    warm = module.SchwabV2BotService._warmup_ready_symbols(bot)
    rows = []
    for symbol in names:
        # In the checked KEEPWATCH-ON branch this is a read-only receipt lookup,
        # not the legacy database seed path. Do not substitute _db_seeded counts.
        complete = module.SchwabV2BotService._seed_strategy_bars_from_db(bot, symbol) is True
        proof = bot._keepwatch_proven.get(symbol)
        rows.append(dict(symbol=symbol, seed_complete=complete, warmup_ready=symbol in warm,
            line_buy_ready=(bot.strategy.line_buy_ready(symbol)
                if callable(getattr(bot.strategy, "line_buy_ready", None)) else None),
            history_anchor_ms=getattr(proof, "anchor_ms", None),
            history_through_ms=getattr(proof, "through_ms", None),
            history_started_ns=getattr(proof, "started_ns", None),
            history_completed_ns=getattr(proof, "completed_ns", None),
            massive_sha256=getattr(proof, "massive_sha256", None),
            schwab_sha256=getattr(proof, "schwab_sha256", None)))
        if causal_observer is not None:
            rows[-1]["causal_line_witness"] = causal_observer.sample(symbol)
    complete = sum(row["seed_complete"] for row in rows)
    pending = []
    if not names:
        pending.append("EMPTY_WATCHED_POPULATION_NOT_0_OF_0_PASS")
    if not bot._boot_exclusion_sources_readable:
        pending.append("INELIGIBLE_EXCLUSION_UNREADABLE")
    if not bot._boot_scanner_selected:
        pending.append("EMPTY_TRADEABLE_SCANNER_POPULATION")
    if complete != len(names):
        pending.append("KEEPWATCH_FULL_SESSION_HISTORY_SEED_INCOMPLETE")
    if any(not row["warmup_ready"] for row in rows):
        pending.append("REST_WARMUP_INCOMPLETE")
    if not bot._boot_state_restoration_complete:
        pending.append("FLEET_BOOT_RESTORATION_LATCH_CLOSED")
    if not getattr(bot.strategy, "_cw_armed_segment_safety_enabled", False):
        pending.append("ARMED_SEGMENT_SAFETY_OFF_NOT_ALL_ON")
    return dict(status="OBSERVED_SEED_SAMPLE_NOT_FULL_TIMELINE_QUALIFICATION", observed_ms=now_ms,
        watched=names, scanner_selected=sorted(bot._boot_scanner_selected),
        current_watchlist=sorted(bot._watchlist), seed_complete=complete, seed_denominator=len(names),
        seed_N_of_N=bool(names) and complete == len(names), symbols=rows, blockers=pending,
        l14_receipt_path_configured=bool(getattr(case.settings, "atr_simulation_aggregate_receipt_path", None)),
        l14_receipt_file_verified=False, actual_0400_to_0700_and_first_flip="UNRUN",
        permission_granted=False, native_completeness="UNQUALIFIED")


def require_private_factory(factory, *, target_capability=None):
    if target_capability is not None:
        from tools.l12_main_nonce_target import MainNonceTarget
        if type(target_capability) is not MainNonceTarget:
            raise ValueError("reviewed_Main_nonce_target_capability_required")
        return target_capability.require_factory(factory)
    engine = getattr(factory, "kw", {}).get("bind")
    if engine is None:
        raise ValueError("explicit_private_sessionmaker_required")
    url = engine.url
    if url.get_backend_name() == "postgresql":
        if (url.database != "oct23_l12_arendt" or url.host not in (None, "")
                or url.query.get("host") != "/private/tmp/oct23-l12-arendt-pg/socket"
                or str(url.query.get("port")) != "55521"):
            raise ValueError("private_pg_target_required")
    elif url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise ValueError("private_file_db_required_for_threaded_loaders")
        target = Path(url.database).resolve()
        if not str(target).startswith("/private/tmp/oct23-l12-arendt-boot-"):
            raise ValueError("private_sqlite_target_required")
    else:
        raise ValueError("unsupported_private_database")


def verify_private_session(session, *, target_capability=None):
    """Read actual target identity before allowing any replay/copy writes."""
    if target_capability is not None:
        from tools.l12_main_nonce_target import MainNonceTarget
        if type(target_capability) is not MainNonceTarget:
            raise ValueError("reviewed_Main_nonce_target_capability_required")
        return target_capability.verify_session(session)
    engine = session.get_bind()
    if engine.dialect.name == "postgresql":
        identity = tuple(session.execute(text("SELECT current_database(), current_setting('port'), "
            "current_setting('data_directory'), inet_server_addr()")).one())
        if identity != ("oct23_l12_arendt", "55521", "/private/tmp/oct23-l12-arendt-pg/data", None):
            raise ValueError("connected_private_pg_identity_mismatch")
    elif engine.dialect.name == "sqlite":
        rows = session.execute(text("PRAGMA database_list")).all()
        main = [row[2] for row in rows if row[1] == "main"]
        if (len(main) != 1 or Path(main[0]).resolve() != Path(engine.url.database).resolve()
                or any(row[1] not in {"main", "temp"} for row in rows)):
            raise ValueError("connected_private_sqlite_identity_mismatch")
    else:
        raise ValueError("unreviewed_connected_dialect")


def manual_stop_roll_readback(*, before, after, bot_symbols, global_symbols,
                              expected_session_start_ms):
    """Pure receipt assessment, not a replacement for real scanner operations.

    Retained older snapshots need not be deleted to become session-ineligible.
    An empty loader return alone is not proof: loader exceptions also return {}.
    """
    if type(expected_session_start_ms) is not int:
        raise ValueError("exact_reviewed_session_anchor_required")
    allowed = {"bot_manual_stop_symbols", "global_manual_stop_symbols"}
    fields = {"id", "snapshot_type", "created_at", "created_ms", "created_ns", "payload",
        "payload_sha256", "matches_current_session"}
    for rows in (before, after):
        if (type(rows) is not list or len(rows) > 4096
                or any(type(row) is not dict or set(row) != fields
                    or row["snapshot_type"] not in allowed or type(row["id"]) is not str
                    or type(row["created_ms"]) is not int
                    or type(row["created_ns"]) is not int
                    or row["created_ns"] // 1000000 != row["created_ms"]
                    or type(row["matches_current_session"]) is not bool for row in rows)
                or len({row["id"] for row in rows}) != len(rows)):
            raise ValueError("complete_bounded_actual_MANSTOP_readback_required")
    if (type(bot_symbols) is not dict or type(global_symbols) is not set
            or any(type(symbols) is not set for symbols in bot_symbols.values())):
        raise ValueError("actual_manual_stop_loader_result_types_required")
    old = {row["id"]: row for row in before}
    new = {row["id"]: row for row in after}
    additions = sorted(new.keys() - old.keys())
    changed = sorted(key for key in new.keys() & old.keys() if new[key] != old[key])
    removed = sorted(old.keys() - new.keys())
    def latest_by_type(rows):
        latest = {}
        for kind in allowed:
            group = [row for row in rows if row["snapshot_type"] == kind]
            if not group:
                continue
            newest = max(row["created_ns"] for row in group)
            tied = [row for row in group if row["created_ns"] == newest]
            if len(tied) != 1:
                raise ValueError("MANSTOP_latest_timestamp_tie_requires_explicit_review")
            latest[kind] = tied[0]
        return latest

    latest = latest_by_type(before)
    expected_removed = sorted(row["id"] for row in latest.values()
        if not row["matches_current_session"])
    current_rows = [row for row in after if row["matches_current_session"]]
    expected_bots, expected_globals = {}, set()
    for row in latest_by_type(after).values():
        if not row["matches_current_session"]:
            continue
        payload = row["payload"]
        if type(payload) is not dict:
            raise ValueError("MANSTOP_current_payload_unreadable_not_empty")
        if row["snapshot_type"] == "bot_manual_stop_symbols":
            bots = payload.get("bots")
            if (type(bots) is not dict or any(type(key) is not str or type(value) is not list
                    or any(type(symbol) is not str or not symbol.strip() for symbol in value)
                    for key, value in bots.items())):
                raise ValueError("MANSTOP_current_bot_payload_unreadable_not_empty")
            expected_bots = {key: {symbol.upper() for symbol in value} for key, value in bots.items()}
        else:
            symbols = payload.get("symbols")
            if type(symbols) is not list or any(type(value) is not str or not value.strip() for value in symbols):
                raise ValueError("MANSTOP_current_global_payload_unreadable_not_empty")
            expected_globals = {value.upper() for value in symbols}
    loaders_match = bot_symbols == expected_bots and global_symbols == expected_globals
    observable_empty = not any(expected_bots.values()) and not expected_globals and loaders_match
    exact_delta = not additions and not changed and removed == expected_removed
    return dict(status=("OBSERVED_MANSTOP_04_ROLL_EMPTY_NOT_FULL_BOOT" if exact_delta and observable_empty
        else "MANSTOP_04_ROLL_UNPROVEN_OR_NONEMPTY_STOP"),
        expected_session_start_ms=expected_session_start_ms, removed_ids=removed,
        expected_stale_latest_ids=expected_removed, unexpected_additions=additions,
        changed_ids=changed, surviving_current_session_rows=current_rows,
        retained_previous_session_rows=[dict(row,
            age_from_target_session_ms=expected_session_start_ms - row["created_ms"])
            for row in after if not row["matches_current_session"]],
        actual_bot_symbols={key: sorted(value) for key, value in bot_symbols.items()},
        actual_global_symbols=sorted(global_symbols), before=deepcopy(before), after=deepcopy(after),
        loader_results_match_actual_latest_payloads=loaders_match,
        permission_granted=False, Mirror4_retired="UNKNOWN_UNLESS_SEPARATELY_COMMITTED_FIRST")


def require_manual_stop_application_bounds(values):
    """Actual NEW transaction defaults, not the rolled-back identity probe."""
    if tuple(values) != ("750ms", "250ms"):
        raise ValueError("MANSTOP_actual_application_transaction_defaults_must_be_750ms_250ms")


@contextmanager
def scoped_existing_scanner_boot_clock(scanner, factory, now_provider):
    """Only the existing scanner clock port, never readiness or persisted clocks."""
    from project_mai_tai.services.strategy_engine_app import StrategyEngineService
    if (type(scanner) is not StrategyEngineService or scanner.session_factory is not factory
            or not callable(now_provider)):
        raise ValueError("actual_same_factory_scanner_clock_port_required")
    owner = scanner.state.alert_engine
    original = owner.now_provider
    if not callable(original):
        raise ValueError("actual_scanner_existing_clock_port_missing")
    receipt = dict(scope="RECONSTRUCTED_RETIMED_BOOTGATE_ONLY",
        port="state.alert_engine.now_provider", original_port_restored=False,
        durable_created_at="UNCHANGED", native_headers="UNCHANGED", permission_granted=False)
    manager = patch.object(owner, "now_provider", now_provider)
    manager.__enter__()
    primary = None
    try:
        yield receipt
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            manager.__exit__(type(primary) if primary is not None else None, primary,
                primary.__traceback__ if primary is not None else None)
            if owner.now_provider is not original:
                raise ValueError("original_scanner_clock_port_cleanup_UNKNOWN")
            receipt["original_port_restored"] = True
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note("scanner_clock_port_cleanup_type=" + type(cleanup).__name__)


def replay_existing_scanner_manual_stop_roll(scanner, factory, *, target_capability,
        expected_session_start_ms, copy_binding, copy_guard, writer_guard):
    """Main-only finite call to existing scanner methods, never an empty-state shim.

    Main must bind a new owned writer lease AFTER copies finish, actual consumer
    source review and exact restored scanner instance. This does not start services
    or construct factories/targets. A fresh source/method review is still required.
    """
    from sqlalchemy import select
    from project_mai_tai.db.models import DashboardSnapshot
    from project_mai_tai.services.strategy_engine_app import StrategyEngineService
    from tools.l12_private_full23_lifecycle import CheckedFactory
    from tools.l12_main_nonce_target import MainNonceTarget
    if (type(scanner) is not StrategyEngineService or type(factory) is not CheckedFactory
            or type(target_capability) is not MainNonceTarget
            or scanner.session_factory is not factory
            or type(factory.target_capability) is not MainNonceTarget
            or factory.target_capability.reviewed_sha256 != target_capability.reviewed_sha256
            or type(expected_session_start_ms) is not int
            or not callable(copy_guard) or not callable(writer_guard)):
        raise ValueError("actual_scanner_checked_factory_original_nonce_and_owned_writer_required")
    from tools.l12_recorded_boot_timeline import _aware_ms, _aware_ns
    actual_anchor = _aware_ms(scanner._current_strategy_session_start_utc().isoformat())
    if actual_anchor != expected_session_start_ms:
        raise ValueError("actual_scanner_session_anchor_disagrees_no_clock_assignment")

    def guarded():
        if (copy_guard(deepcopy(copy_binding), factory) is not True
                or writer_guard(factory, target_capability) is not True):
            raise ValueError("MANSTOP_copy_source_or_owned_writer_proof_UNKNOWN")

    def readback():
        guarded()
        with factory() as session:
            try:
                verify_private_session(session, target_capability=target_capability)
                rows = session.scalars(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type.in_(("bot_manual_stop_symbols", "global_manual_stop_symbols"))
                ).order_by(DashboardSnapshot.created_at, DashboardSnapshot.id).limit(4097)).all()
                if len(rows) > 4096:
                    raise ValueError("MANSTOP_complete_readback_bound_exceeded_no_truncation")
                result = []
                for row in rows:
                    if row.created_at is None:
                        raise ValueError("MANSTOP_actual_created_at_missing")
                    result.append(dict(id=str(row.id), snapshot_type=row.snapshot_type,
                        created_at=row.created_at.isoformat(), created_ms=_aware_ms(row.created_at.isoformat()),
                        created_ns=_aware_ns(row.created_at.isoformat()),
                        payload=deepcopy(row.payload), payload_sha256=sha256(json.dumps(row.payload,
                            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
                        matches_current_session=scanner._snapshot_matches_current_strategy_session(
                            row, require_session_marker=True)))
                return result
            finally:
                session.rollback()

    guarded()
    with factory() as application_defaults:
        try:
            # CheckedFactory has already rolled back its bounded identity probe.
            # Do not verify_session again before this read: that would mask
            # unbounded defaults with SET LOCAL from another probe transaction.
            require_manual_stop_application_bounds(application_defaults.execute(text(
                "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')"
            )).one())
        finally:
            application_defaults.rollback()
    before = readback()
    guarded()
    scanner._purge_stale_manual_stop_snapshots()
    after = readback()
    guarded()
    bots = scanner._load_manual_stop_symbols()
    globals_ = scanner._load_global_manual_stop_symbols()
    guarded()
    return manual_stop_roll_readback(before=before, after=after, bot_symbols=bots,
        global_symbols=globals_, expected_session_start_ms=expected_session_start_ms)


def manual_stop_twenty_plan():
    """C1 literal denominator, not a configurable subset or measured result."""
    return [dict(scope=scope, cycle=cycle, action=action)
        for scope in ("bot", "global") for cycle in range(1, 21)
        for action in ("stop", "resume", "stop")]


class RecordedManualStopTransport:
    """Injected runtime-controls sink only; never opens Redis or another client."""

    def __init__(self):
        self.events = []

    async def xadd(self, stream, fields):
        if (len(self.events) >= 120 or type(stream) is not str
                or type(fields) is not dict or set(fields) != {"data"}
                or type(fields["data"]) is not str):
            raise ValueError("MANSTOP_exact_bounded_runtime_control_event_required")
        self.events.append(dict(stream=stream, fields=dict(fields)))
        return "CONTROLLED_NO_NETWORK_" + str(len(self.events))


def manual_stop_pair_p95_ns(samples):
    """Nearest-rank p95 of all120 successful DELETE+INSERT cursor pairs."""
    if (type(samples) is not list or len(samples) != 120
            or any(type(value) is not int or value < 0 for value in samples)):
        raise ValueError("MANSTOP_all120_actual_cursor_pairs_required_no_deselection")
    return sorted(samples)[113]


def compare_manual_stop_twenty(baseline, upgraded):
    if (type(baseline) is not dict or type(upgraded) is not dict
            or baseline.get("arm") != "C1_BASELINE_SOURCE21"
            or upgraded.get("arm") != "UPGRADED_SOURCE21_PLUS2"
            or any(row.get("status") != "MANSTOP120_ROUTE_STAGES_OBSERVED_NOT_INSTALL_PERMISSION"
                or row.get("L12_unchanged") is not True for row in (baseline, upgraded))):
        raise ValueError("MANSTOP_two_complete_separate_populated_arms_required")
    old = manual_stop_pair_p95_ns(baseline["cursor_pair_ns"])
    new = manual_stop_pair_p95_ns(upgraded["cursor_pair_ns"])
    return dict(baseline_p95_ns=old, upgraded_p95_ns=new, permitted_delta_ns=5000000,
        latency_within_C11329_bound=new <= old + 5000000,
        referenced_parent_23503=upgraded.get("referenced_parent_23503") is True,
        permission_granted=False, quiet_host="NOT_ESTABLISHED_BY_FUNCTIONAL_CALLER")


async def replay_existing_manual_stop_twenty(app, factory, *, arm, copy_binding,
        copy_guard, writer_guard, source_guard, symbol, strategy_code,
        maximum_projection_rows, maximum_projection_bytes):
    """Existing real routes, original private capability, one finite leased arm.

    Main constructs the actual app with injected read ports and the explicit
    local transport below. No app/factory/engine/provider/lease is created here.
    Route commits are real; a later failure is NOT rolled back by this caller.
    """
    from fastapi import FastAPI
    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError
    from project_mai_tai.services import control_plane as control
    from tools.l12_private_full23_lifecycle import CheckedFactory
    from tools.l12_nonce_copy_lease import NonceCopyLease
    from tools.l12_main_nonce_target import MainNonceTarget
    from tools.l12_scoped_chunks_v2 import canonical
    if (type(app) is not FastAPI or type(factory) is not CheckedFactory
            or type(factory.target_capability) is not MainNonceTarget
            or type(factory.nonce_lease) is not NonceCopyLease
            or arm not in {"C1_BASELINE_SOURCE21", "UPGRADED_SOURCE21_PLUS2"}
            or type(symbol) is not str or not symbol or symbol != symbol.strip().upper()
            or type(strategy_code) is not str or not strategy_code or strategy_code != strategy_code.strip()
            or type(maximum_projection_rows) is not int or maximum_projection_rows <= 0
            or type(maximum_projection_bytes) is not int or maximum_projection_bytes <= 0
            or not all(callable(value) for value in (copy_guard, writer_guard, source_guard))):
        raise ValueError("MANSTOP_exact_original_nonce_factory_finite_arm_and_claim_required")
    repository = app.state.repository
    if (type(repository) is not control.ControlPlaneRepository
            or repository.session_factory is not factory
            or type(repository.redis) is not RecordedManualStopTransport
            or repository.redis.events):
        raise ValueError("MANSTOP_actual_repository_same_factory_fresh_local_transport_required")
    if (getattr(repository.settings, "strategy_schwab_1m_v2_keepwatch1_enabled", None) is not True
            or getattr(repository.settings, "strategy_schwab_1m_v2_brokerdecides1_enabled", None) is not True):
        raise ValueError("MANSTOP_actual_combined_L11_L12_ON_Settings_required")
    endpoints = {}
    for path, name in (("/bot/symbol/stop", "bot_symbol_stop"),
            ("/bot/symbol/resume", "bot_symbol_resume"),
            ("/scanner/symbol/stop", "scanner_symbol_stop"),
            ("/scanner/symbol/resume", "scanner_symbol_resume")):
        routes = [route for route in app.routes if getattr(route, "path", None) == path]
        if (len(routes) != 1 or "GET" not in routes[0].methods
                or routes[0].endpoint.__name__ != name
                or routes[0].endpoint.__module__ != control.__name__
                or routes[0].endpoint.__code__ not in control.build_app.__code__.co_consts
                or inspect.getclosurevars(routes[0].endpoint).nonlocals.get("app") is not app):
            raise ValueError("MANSTOP_real_unwrapped_build_app_route_required")
        endpoints[path] = routes[0].endpoint
    for name in ("set_bot_manual_stop_symbol", "remove_bot_manual_stop_symbol",
            "set_global_manual_stop_symbol", "remove_global_manual_stop_symbol"):
        method = getattr(repository, name)
        if not inspect.ismethod(method) or method.__func__ is not getattr(control.ControlPlaneRepository, name):
            raise ValueError("MANSTOP_real_repository_methods_required")
    lease = factory.nonce_lease

    def guarded():
        lease.require_postcopy("manual_stop")
        if (copy_guard(deepcopy(copy_binding), factory) is not True
                or writer_guard(factory, factory.target_capability) is not True
                or source_guard(app, factory) is not True):
            raise ValueError("MANSTOP_current_copy_writer_or_handler_source_UNKNOWN")

    def read_projections():
        guarded()
        result = {}
        with factory() as session:
            try:
                for table, order in (("brokerdecides_dispatch_retirements", "attempt_id"),
                        ("brokerdecides_dispatch_evidence", "attempt_id,entity_kind,entity_id")):
                    exists = session.scalar(text("SELECT to_regclass(:name) IS NOT NULL"),
                        dict(name="public." + table))
                    if type(exists) is not bool:
                        raise ValueError("MANSTOP_projection_presence_UNKNOWN")
                    if not exists:
                        result[table] = dict(state="SOURCE_ABSENT", rows=None, sha256=None)
                        continue
                    rows = session.scalars(text("SELECT to_jsonb(t)::text FROM public." + table
                        + " AS t ORDER BY " + order + " LIMIT :bound"),
                        dict(bound=maximum_projection_rows + 1)).all()
                    if len(rows) > maximum_projection_rows:
                        raise ValueError("MANSTOP_full_projection_row_bound_exceeded_no_truncation")
                    digest, size = sha256(), 0
                    for raw in rows:
                        if type(raw) is not str:
                            raise ValueError("MANSTOP_projection_raw_PG_text_required")
                        encoded = raw.encode()
                        size += len(encoded)
                        if size > maximum_projection_bytes:
                            raise ValueError("MANSTOP_full_projection_byte_bound_exceeded_no_truncation")
                        digest.update(len(encoded).to_bytes(8, "big"))
                        digest.update(encoded)
                    result[table] = dict(state="PRESENT", rows=len(rows), sha256=digest.hexdigest())
            finally:
                session.rollback()
        if arm == "C1_BASELINE_SOURCE21":
            if any(row["state"] != "SOURCE_ABSENT" for row in result.values()):
                raise ValueError("MANSTOP_baseline_must_not_pretend_new_projection_is_old_source")
        elif any(row["state"] != "PRESENT" or row["rows"] <= 0 for row in result.values()):
            raise ValueError("MANSTOP_upgraded_actual_positive_L12_rows_required")
        return result

    def read_stopped(scope):
        from project_mai_tai.db.models import DashboardSnapshot
        from sqlalchemy import select
        guarded()
        with factory() as session:
            try:
                rows = session.scalars(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == ("bot_manual_stop_symbols" if scope == "bot"
                        else "global_manual_stop_symbols"))
                    .order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1)).all()
                if not rows:
                    return False
                if not repository._snapshot_matches_current_scanner_session(rows[0],
                        session_start=control.current_scanner_session_start_utc(), require_session_marker=True):
                    return False
                payload = rows[0].payload
                if type(payload) is not dict:
                    raise ValueError("MANSTOP_route_readback_payload_UNKNOWN")
                values = payload.get("bots", {}).get(strategy_code, []) if scope == "bot" else payload.get("symbols", [])
                if type(values) is not list:
                    raise ValueError("MANSTOP_route_readback_membership_UNKNOWN")
                return symbol in values
            finally:
                session.rollback()

    guarded()
    with factory() as defaults:
        try:
            require_manual_stop_application_bounds(defaults.execute(text(
                "SELECT current_setting('statement_timeout'),current_setting('lock_timeout')")).one())
        finally:
            defaults.rollback()
    if any(read_stopped(scope) for scope in ("bot", "global")):
        raise ValueError("MANSTOP_explicit_control_symbol_already_stopped_no_initial_clear")
    before = read_projections()
    stages, samples, pending, current = [], [], {}, []
    engine = factory.kw["bind"]

    def before_cursor(connection, cursor, statement, parameters, context, executemany):
        stripped = statement.lstrip()
        operation = ("DELETE" if stripped.startswith("DELETE FROM dashboard_snapshots ") else
            "INSERT" if stripped.startswith("INSERT INTO dashboard_snapshots ") else None)
        if operation:
            pending[id(context)] = (operation, _manual_perf_ns())

    def after_cursor(connection, cursor, statement, parameters, context, executemany):
        pair = pending.pop(id(context), None)
        if pair is not None:
            current.append(dict(operation=pair[0], duration_ns=_manual_perf_ns() - pair[1]))

    primary, installed = None, []
    try:
        for name, listener in (("before_cursor_execute", before_cursor), ("after_cursor_execute", after_cursor)):
            event.listen(engine, name, listener)
            installed.append((name, listener))
        lease.journal(canonical(dict(kind="BEGIN_MANSTOP_ACTUAL_ROUTES120", arm=arm,
            before=before, mutations="ACTUAL_ROUTE_COMMITS_NOT_OUTER_ROLLBACK", permission_granted=False)))
        for step in manual_stop_twenty_plan():
            guarded()
            current.clear()
            scope, action = step["scope"], step["action"]
            path = ("/bot/symbol/" if scope == "bot" else "/scanner/symbol/") + action
            arguments = dict(symbol=symbol)
            if scope == "bot":
                arguments["strategy_code"] = strategy_code
            response = await endpoints[path](**arguments)
            if (response.status_code != 303 or pending
                    or [row["operation"] for row in current] != ["DELETE", "INSERT"]):
                raise ValueError("MANSTOP_exact_successful_DELETE_INSERT_route_pair_required")
            if read_stopped(scope) is not (action == "stop"):
                raise ValueError("MANSTOP_actual_committed_route_readback_mismatch")
            samples.append(sum(row["duration_ns"] for row in current))
            row = dict(step, cursor_pair_ns=samples[-1], committed_route_readback=True)
            stages.append(row)
            lease.journal(canonical(dict(kind="MANSTOP_COMMITTED_ROUTE_STAGE", arm=arm, stage=row)))
        after = read_projections()
        if after != before or len(repository.redis.events) != 120:
            raise ValueError("MANSTOP_L12_rows_changed_or_runtime_event_count_mismatch")
        refused = None
        if arm == "UPGRADED_SOURCE21_PLUS2":
            guarded()
            with factory() as session:
                try:
                    parent = session.scalar(text("SELECT r.attempt_id FROM brokerdecides_dispatch_retirements r "
                        "JOIN dashboard_snapshots d ON d.id=r.attempt_id "
                        "WHERE d.snapshot_type='v2_wait_dispatch' ORDER BY r.attempt_id LIMIT 1"))
                    if parent is None:
                        raise ValueError("MANSTOP_actual_referenced_v2_wait_dispatch_parent_missing")
                    try:
                        session.execute(text("DELETE FROM dashboard_snapshots WHERE id=:id"), dict(id=parent))
                    except DBAPIError as exc:
                        refused = getattr(exc.orig, "sqlstate", None) == "23503"
                        if not refused:
                            raise
                    else:
                        raise ValueError("MANSTOP_referenced_parent_DELETE_not_refused")
                finally:
                    session.rollback()
            if read_projections() != before:
                raise ValueError("MANSTOP_23503_rollback_projection_changed")
        result = dict(status="MANSTOP120_ROUTE_STAGES_OBSERVED_NOT_INSTALL_PERMISSION", arm=arm,
            stages=stages, cursor_pair_ns=samples, p95_cursor_pair_ns=manual_stop_pair_p95_ns(samples),
            before=before, after=after, L12_unchanged=True, referenced_parent_23503=refused,
            transport="INJECTED_RUNTIME_CONTROLS_NO_NETWORK", permission_granted=False,
            quiet_host="NOT_ESTABLISHED_BY_FUNCTIONAL_CALLER")
    except BaseException as exc:
        primary = exc
        try:
            lease.journal(canonical(dict(kind="MANSTOP_PARTIAL_OUTCOME_UNKNOWN", arm=arm,
                completed_route_stages=len(stages), failure_type=type(exc).__name__,
                route_commits_may_have_happened=True, no_automatic_retry=True)))
        except BaseException as cleanup:
            exc.add_note("manual_stop_partial_receipt_failure_type=" + type(cleanup).__name__)
        raise
    finally:
        cleanup_error = None
        for name, listener in reversed(installed):
            try:
                event.remove(engine, name, listener)
            except BaseException as exc:
                if primary is not None:
                    primary.add_note("manual_stop_listener_cleanup_type=" + type(exc).__name__)
                elif cleanup_error is None:
                    cleanup_error = exc
        if primary is None and cleanup_error is not None:
            raise cleanup_error
    lease.journal(canonical(dict(kind="END_MANSTOP_ACTUAL_ROUTES120", result=result)))
    return result


async def replay_service_boot(case, factory, *, packet, blobs, stage_operands,
                              expected_channel, restore_lines, startup_profile="COMBINED_L11_L12",
                              v2_review=None, v2_copy_guard=None, target_capability=None,
                              timeline_driver=None, scanner_roll=None):
    """case is the reviewed offline LineRestoreCase, with explicit actual settings.

    restore_lines must consume hash-bound exported bars/coverage/notes through
    real callbacks. No generic decoder or permission is inferred from it here.
    A missing callback refuses instead of labelling a probe a complete replay.
    The explicit V2 paths revalidate raw roles and use their decoded line
    callback; it never constructs V1-complete projections or accepts a substitute
    restore callback. Combined mode requires its explicit 06936 source/copy
    review and original nonce, never a relabelled V9 or rewritten capability.
    v2_copy_guard(binding, factory) must independently establish the actual
    committed/read-back copy journal, target identity and held writer exclusion;
    a caller-supplied True in CONTROLLED tests is not that evidence.
    """
    global _RUNNING
    if scanner_roll is not None and (type(scanner_roll) is not dict
            or set(scanner_roll) != {"scanner", "writer_guard", "expected_session_start_ms"}
            or target_capability is None or startup_profile != "COMBINED_L11_L12"
            or v2_review is None):
        raise ValueError("explicit_combined_nonce_scanner_roll_operands_required")
    if scanner_roll is not None and timeline_driver is None:
        raise ValueError("scanner_04_roll_requires_reviewed_BOOTONLY_timeline_clock_port")
    if startup_profile not in {"COMBINED_L11_L12", "L12_ONLY"}:
        raise ValueError("explicit_reviewed_boot_profile_required")
    if startup_profile == "L12_ONLY" and (
            getattr(case.settings, "strategy_schwab_1m_v2_keepwatch1_enabled", False) is not False
            or hasattr(case.bot, "_keepwatch_publication_fence")):
        raise ValueError("l12_only_boot_requires_actual_no_l11_source_profile")
    v2_mode = v2_review is not None
    from tools.l12_composed_execution_binding import (
        COMBINED_BOOT_VERSION, CURRENT_359_BOOT_VERSION, CURRENT_F_BOOT_VERSION, CORRECTED_L11_BOOT_VERSION,
        BOUNDED_L11_BOOT_VERSION)
    role_review = v2_review.get("role_review") if isinstance(v2_review, dict) else None
    approval = role_review.get("execution_approval") if isinstance(role_review, dict) else None
    combined_v2 = isinstance(approval, dict) and approval.get("binding_version") in {
        COMBINED_BOOT_VERSION, CURRENT_359_BOOT_VERSION, CURRENT_F_BOOT_VERSION, CORRECTED_L11_BOOT_VERSION,
        BOUNDED_L11_BOOT_VERSION}
    if combined_v2 and approval["binding_version"] == BOUNDED_L11_BOOT_VERSION:
        from tools.l12_recorded_boot_driver import require_4f_boot_driver
        require_4f_boot_driver(timeline_driver)
    if timeline_driver is not None:
        from tools.l12_recorded_boot_driver import RecordedBootDriver
        if type(timeline_driver) is not RecordedBootDriver or not combined_v2:
            raise ValueError("recorded_timeline_requires_explicit_combined_raw_role_mode")
        from tools.l12_close_asof_contract import is_sha
        members = {"tools/l12_recorded_boot_driver.py", "tools/l12_recorded_boot_timeline.py",
            "tools/l12_boot_owner_clock.py"}
        pins = approval["reviewed_pins"]
        if any(not is_sha(pins.get(name)) or
                sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest() != pins[name]
                for name in members):
            raise ValueError("recorded_timeline_complete_source_pins_required")
    if combined_v2 and (startup_profile != "COMBINED_L11_L12" or target_capability is None
            or getattr(case.settings, "strategy_schwab_1m_v2_keepwatch1_enabled", False) is not True):
        raise ValueError("combined_v2_boot_requires_actual_keepwatch_ON_and_original_nonce")
    if target_capability is not None:
        if not v2_mode:
            raise ValueError("nonce_target_requires_explicit_raw_role_boot_not_legacy_V1_relabel")
        from tools.l12_private_full23_lifecycle import CheckedFactory
        from tools.l12_main_nonce_target import MainNonceTarget
        if (type(factory) is not CheckedFactory or type(target_capability) is not MainNonceTarget
                or type(factory.target_capability) is not MainNonceTarget
                or factory.target_capability.reviewed_sha256 != target_capability.reviewed_sha256):
            raise ValueError("nonce_boot_requires_exact_per_session_checked_factory")
    if v2_mode:
        if ((startup_profile != "L12_ONLY" and not combined_v2)
                or stage_operands is not None or restore_lines is not None):
            raise ValueError("v2_boot_requires_l12_only_no_v1_stage_or_substitute_line_callback")
        packet, blobs, v2_review = deepcopy(packet), dict(blobs), deepcopy(v2_review)
        stage, restore_lines = _bind_v2_boot_inputs(packet, blobs, v2_review)
        stage_ready = stage["status"] == "V2_PRIVATE_BOOT_INPUTS_BOUND_NOT_STARTED"
    else:
        if v2_copy_guard is not None:
            raise ValueError("v2_copy_guard_cannot_relabel_historical_v1_boot")
        stage = stage_replay(packet, blobs, **stage_operands)
        stage_ready = stage["status"] == "INPUT_STAGE_BOUND_NOT_BOOT_EXECUTED"
    report = {"status": "BOOT_INPUTS_UNKNOWN", "stage": stage,
        "input_protocol": ("V2_COMBINED_4F0150_RAW_ROLES" if combined_v2
            and approval["binding_version"] == BOUNDED_L11_BOOT_VERSION else
            "V2_COMBINED_FF41_RAW_ROLES" if combined_v2
            and approval["binding_version"] == CORRECTED_L11_BOOT_VERSION else
            "V2_COMBINED_F278_RAW_ROLES" if combined_v2
            and approval["binding_version"] == CURRENT_F_BOOT_VERSION else
            "V2_COMBINED_359B_RAW_ROLES" if combined_v2
            and approval["binding_version"] == CURRENT_359_BOOT_VERSION else
            "V2_COMBINED_06936_RAW_ROLES" if combined_v2 else
            "V2_V9_RAW_ROLES" if v2_mode else "HISTORICAL_V1"),
        "startup_profile": startup_profile,
        "actual_close_boot": "UNRUN", "native_completeness": "UNQUALIFIED",
        "permission_granted": False, "store_calls": [], "heartbeats": []}
    controlled = stage.get("native_evidence_mode") == C11929_CONTROLLED_VERSION
    if controlled:
        report.update(native_evidence_mode=C11929_CONTROLLED_VERSION,
            native_entry_permission="UNKNOWN", line_owner_export="NOT_DELIVERED_NOT_CERTIFIED",
            controlled_variant=stage["controlled_variant"])
        factory.nonce_lease.require_postcopy("boot")
        factory.nonce_lease.require_boot_arm(packet["variant"])
        if timeline_driver is None or timeline_driver.initial_carried_review is None:
            raise ValueError("C11929_actual_reviewed_timeline_and_carried_boot_callback_required")
        if timeline_driver.publish_control is None:
            raise ValueError("C12309_explicit_publish_boundary_budget_and_ruling_required")
        if (getattr(case.bot, "_l12_controlled_arm_consumed", False)
                or case.bot._stop_event.is_set()):
            raise ValueError("sequential_boot_requires_fresh_case_and_service_no_retry")
        case.bot._l12_controlled_arm_consumed = True
        if case.bot._watchlist:
            raise ValueError("C11929_case_factory_must_not_force_initial_subscription")
        factory_path = Path(inspect.getfile(type(case))).resolve()
        accepted_factory = v2_review["controlled_native_review"]["case_factory_sha256"]
        if (factory_path != Path(__file__).resolve().parents[1] / "tests/line_restore_acceptance_factory.py"
                or sha256(factory_path.read_bytes()).hexdigest() != accepted_factory):
            raise ValueError("C11929_exact_reviewed_unsubscribed_case_factory_required")
    if not stage_ready or not callable(restore_lines):
        return report
    copy_binding = None
    if v2_mode:
        approval = v2_review["role_review"]["execution_approval"]
        pins = json.dumps(approval["reviewed_pins"], sort_keys=True, separators=(",", ":")).encode()
        copy_binding = dict(manifest_sha256=stage["scope_manifest_sha256"],
            source_schema_sha256=stage["source_schema_sha256"],
            source_review_sha256=approval["review_sha256"], execution_pins_sha256=sha256(pins).hexdigest(),
            execution_root=stage["execution_binding"]["execution_root"],
            snapshot_id=packet["snapshot_id"], publisher_epoch=packet["publisher_epoch"],
            reviewed_boot_at_ms=stage["reviewed_boot_at_ms"], line_raw_sha256=stage["line_raw_sha256"])
        if target_capability is not None:
            if combined_v2:
                from tools.l12_main_nonce_target import validate_execution_binding
                consumer = validate_execution_binding(approval["consumer_binding"], target_capability,
                    inventory=approval["reviewed_pins"])
                if consumer != copy_binding["execution_root"]:
                    raise ValueError("combined_boot_actual_consumer_head_changed")
                copy_binding.update(creation_source_head=target_capability.values["source_head"],
                    copy_receipt_sha256=approval["copy_receipt_sha256"],
                    schema_review_sha256=approval["schema_review_sha256"],
                    consumer_binding_sha256=sha256(json.dumps(approval["consumer_binding"],
                        sort_keys=True, separators=(",", ":")).encode()).hexdigest())
            elif target_capability.values["source_head"] != copy_binding["execution_root"]:
                raise ValueError("nonce_boot_target_and_actual_execution_root_disagree")
            copy_binding["target_capability_sha256"] = target_capability.reviewed_sha256
            report["target_capability_sha256"] = target_capability.reviewed_sha256
        report["copy_binding"] = copy_binding
        if not callable(v2_copy_guard) or v2_copy_guard(deepcopy(copy_binding), factory) is not True:
            report["status"] = "V2_PRIVATE_COPY_JOURNAL_OR_WRITER_FENCE_UNKNOWN_NO_BOOT"
            return report
    if target_capability is None:
        require_private_factory(factory)
    else:
        require_private_factory(factory, target_capability=target_capability)
    with factory() as private_check:
        try:
            if target_capability is None:
                verify_private_session(private_check)
            else:
                verify_private_session(private_check, target_capability=target_capability)
        finally:
            private_check.rollback()
    if (case.settings.strategy_schwab_1m_v2_tick_capture_enabled
            and factory.kw["bind"].dialect.name != "postgresql"):
        raise ValueError("tick_writer_profile_requires_private_postgresql")
    if _RUNNING:
        raise RuntimeError("offline_boot_patch_scope_already_owned")
    if (case.now_ms != stage["reviewed_boot_at_ms"] or not case.bot.enabled
            or case.settings.strategy_schwab_1m_v2_brokerdecides1_enabled is not True):
        raise ValueError("explicit_boot_clock_and_enabled_profile_required")
    record = packet["artifacts"]["admission_snapshot"]
    if record["pointer" if v2_mode else "record_pointer"] != "":
        return report
    raw = blobs[record["reference"]]
    try:
        captured = decode(raw)
        if (captured.epoch != packet["publisher_epoch"]
                or {book.account for book in captured.books} != set(packet["accounts"].values())):
            return report
    except (ValueError, TypeError, UnicodeError):
        return report
    bot = case.bot
    bot.session_factory = factory

    class RecordedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromtimestamp(case.now_ms / 1000, UTC)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    class Transport:
        closed = False

        async def get(self, key):
            if key != expected_channel:
                raise ValueError("unexported_redis_key")
            return raw

        async def xrevrange(self, key, **kwargs):
            # Stream cursor only, never ownership/native-flat evidence.
            if key != f"{case.settings.redis_stream_prefix}:order-events":
                raise ValueError("unexported_stream_cursor")
            return []

        async def xadd(self, *args, **kwargs):
            report["offline_order_publication_attempts"] = report.get("offline_order_publication_attempts", 0) + 1
            raise RuntimeError("offline_boot_order_publication_forbidden")

        async def aclose(self):
            self.closed = True

    class Endpoint:
        configured = connected = False

        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            await bot._stop_event.wait()

        async def stop(self):
            pass

        def set_desired_symbols(self, symbols):
            # Subscription output only; does not claim any bars were delivered.
            self.desired_symbols = tuple(symbols)

        def fetch_session_history(self, *args, **kwargs):
            raise RuntimeError("native_history_not_exported")

        def _set_line_source_state(self, symbol, state):
            self.line_source_state = (symbol, state)

    transport = Transport()

    async def offline_phone(*args, **kwargs):
        report["offline_phone_requests"] = report.get("offline_phone_requests", 0) + 1
        return False  # Retain unknown delivery; never send or claim a phone ACK.

    async def idle():
        await bot._stop_event.wait()

    async def no_provider():
        raise RuntimeError("fresh_native_boot_positions_not_exported")

    async def heartbeat(status=None):
        report["heartbeats"].append(status)
        if status == "starting":
            await refresh_consumer(bot)
            await restore_lines(case)
            if case.now_ms != stage["reviewed_boot_at_ms"]:
                raise ValueError("line_callback_changed_reviewed_boot_clock")
            if not controlled:
                report["line_snapshot"] = case.snapshot()
            else:
                report["line_snapshot"] = "NO_EXPORTED_LINE_PREFIX_CLAIM_REAL_TIMELINE_CALLBACKS_REQUIRED"
            if combined_v2:
                report["combined_seed_sample"] = observe_combined_seed_state(case)
                report["combined_seed_timeline_gate"] = "UNRUN_SINGLE_STARTING_HEARTBEAT_NOT_0400_TO_FIRST_FLIP"
                if timeline_driver is not None:
                    report["recorded_timeline"] = await timeline_driver.run(case,
                        copy_guard=lambda: v2_copy_guard(deepcopy(copy_binding), factory))
                    report["recorded_timeline"]["MANSTOP_0400"] = report.get(
                        "MANSTOP_0400", "UNKNOWN_PENDING_ACTUAL_ROLL_OBSERVATION")
                    report["combined_seed_timeline_gate"] = "CALLBACK_TIMELINE_EXECUTED_REVIEW_FOUR_OUTPUTS_NOT_AUTOMATIC_PASS"
            bot._stop_event.set()

    def wrap_sync(name, original):
        def delegated(*args, **kwargs):
            report["store_calls"].append(name)
            return original(*args, **kwargs)
        return delegated

    def wrap_async(name, original):
        async def delegated(*args, **kwargs):
            report["store_calls"].append(name)
            return await original(*args, **kwargs)
        return delegated

    _RUNNING = True
    try:
        with ExitStack() as stack, case.clock():
            if scanner_roll is not None:
                scanner = scanner_roll["scanner"]
                if controlled:
                    if getattr(scanner, "_l12_controlled_arm_consumed", False):
                        raise ValueError("sequential_boot_requires_fresh_scanner")
                    scanner._l12_controlled_arm_consumed = True
                report["scanner_clock_port"] = stack.enter_context(scoped_existing_scanner_boot_clock(
                    scanner, factory, lambda: RecordedDateTime.now(UTC)))
                report["scanner_clock_port"]["mapping"] = timeline_driver.mapping.receipt()
            for name in CLOCK_MODULES:
                stack.enter_context(patch(f"project_mai_tai.{name}.datetime", RecordedDateTime))
            stack.enter_context(patch.object(module.Redis, "from_url", return_value=transport))
            stack.enter_context(patch.object(module, "SchwabV2RestClient", Endpoint))
            stack.enter_context(patch.object(module, "SchwabV2Streamer", Endpoint))
            stack.enter_context(patch.object(asyncio.get_running_loop(), "add_signal_handler"))
            stack.enter_context(patch.object(bot, "_publish_heartbeat", heartbeat))
            stack.enter_context(patch.object(bot, "_soft_rest_boot_positions", no_provider))
            if combined_v2:
                stack.enter_context(patch.object(bot, "_brokerdecides1_owner_phone", offline_phone))
            for name in L12_ONLY_LOOPS if startup_profile == "L12_ONLY" else LOOPS:
                stack.enter_context(patch.object(bot, name, idle))
            for name in STORES:
                original = getattr(bot, name)
                wrapper = wrap_async if inspect.iscoroutinefunction(original) else wrap_sync
                stack.enter_context(patch.object(bot, name, wrapper(name, original)))
            try:
                if v2_mode and v2_copy_guard(deepcopy(copy_binding), factory) is not True:
                    raise ValueError("v2_private_copy_fence_changed_before_service")
                if scanner_roll is not None:
                    if scanner_roll["expected_session_start_ms"] != case.now_ms:
                        raise ValueError("scanner_roll_requires_reviewed_actual_04_boot_clock")
                    report["MANSTOP_0400"] = replay_existing_scanner_manual_stop_roll(
                        scanner_roll["scanner"], factory, target_capability=target_capability,
                        expected_session_start_ms=scanner_roll["expected_session_start_ms"],
                        copy_binding=copy_binding, copy_guard=v2_copy_guard,
                        writer_guard=scanner_roll["writer_guard"])
                    if report["MANSTOP_0400"]["status"] != "OBSERVED_MANSTOP_04_ROLL_EMPTY_NOT_FULL_BOOT":
                        raise ValueError("actual_MANSTOP_04_roll_readback_not_empty_or_UNKNOWN")
                await module.SchwabV2BotService.run(bot)
                if v2_mode and v2_copy_guard(deepcopy(copy_binding), factory) is not True:
                    raise ValueError("v2_private_copy_fence_changed_after_service")
                if bot.tick_writer is not None:
                    # The real run's bounded test tasks may be cancelled before
                    # its observer drain. Flush the real writer into this copy.
                    while bot.tick_writer.stats()["buffered"]:
                        await bot.tick_writer._flush()
            finally:
                primary_before_cleanup = sys.exc_info()[1]
                alert_cleanup_failure = None
                bot._stop_event.set()
                tasks = tuple(getattr(bot, "_tasks", {}).values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if getattr(bot, "tick_writer", None) is not None:
                    await bot.tick_writer.stop()
                if startup_profile == "COMBINED_L11_L12":
                    await bot._stop_keepwatch_bar_owners()
                if combined_v2:
                    from project_mai_tai.brokerdecides1_alert_transport import stop_alert_transport
                    try:
                        if await stop_alert_transport(bot) != "STOPPED":
                            alert_cleanup_failure = ValueError("combined_boot_owned_alert_cleanup_UNKNOWN")
                    except BaseException as exc:
                        alert_cleanup_failure = exc
                emits = tuple(bot._eh_stream_emit_tasks)
                for task in emits:
                    task.cancel()
                await asyncio.gather(*emits, return_exceptions=True)
                if bot._restart_checkpoint_writer is not None:
                    await bot._restart_checkpoint_writer.drain(timeout=3.0)
                if bot._country_router is not None:
                    await bot._country_router.close()
                await transport.aclose()
                if alert_cleanup_failure is not None:
                    if primary_before_cleanup is not None:
                        primary_before_cleanup.add_note("combined_alert_cleanup_failure_type=" + type(alert_cleanup_failure).__name__)
                    else:
                        raise alert_cleanup_failure
        if scanner_roll is not None:
            if report["scanner_clock_port"]["original_port_restored"] is not True:
                raise ValueError("original_scanner_clock_port_cleanup_UNKNOWN")
        report.update(status="SERVICE_LIFECYCLE_AND_LINE_CALLBACK_EXECUTED_NOT_NATIVE",
            transport_closed=transport.closed,
            pending_owned_tasks=sum(not task.done() for task in bot._tasks.values()),
            owner_restore_readable=bot.strategy._flip_owner_restore_readable,
            tick_writer_stats=bot.tick_writer.stats() if bot.tick_writer is not None else None,
            pending_owner_cache_symbols=sorted(bot.strategy._restored_flip_owners),
            owner_states={symbol: dict(phase=state.flip_owner_phase,
                opportunity_id=state.flip_owner_opportunity_id,
                first_rest_placed=state.flip_owner_first_rest_placed,
                retry_segment=state.retry_one_segment_id,
                retry_closes=state.retry_one_closes_in_segment,
                retry_readable=state.retry_one_budget_readable)
                for symbol, state in bot.strategy._symbol_states.items()})
        if controlled:
            report.update(status="CONTROLLED_NATIVE_REAL_STORE_LOADERS_AND_TIMELINE_NOT_NATIVE_PERMISSION",
                fail_closed_order_publication=report.get("offline_order_publication_attempts", 0) == 0,
                native_entry_permission="UNKNOWN", modeled_clock_is_not_wire_freshness=True)
            if report.get("offline_order_publication_attempts", 0):
                raise ValueError("C11929_UNKNOWN_native_must_not_publish_orders")
            recorded = report.get("recorded_timeline", {}).get("first_BUY", {})
            if any(isinstance(row, dict) and (row.get("returned_BUY_draft") is True
                    or any(item.get("allowed") is True for item in row.get("actual_strategy_admissions", [])))
                    for row in recorded.values()):
                raise ValueError("C11929_UNKNOWN_native_must_not_admit_BUY")
            report["controlled_working_BUY_variant"] = dict(
                working_provider=packet["working_provider"], working_symbol=packet["working_symbol"],
                modeled_role_contains_working_BUY=packet["variant"] == "ONE_WORKING_BUY",
                actual_first_BUY=recorded.get(packet["working_symbol"], "UNKNOWN_NO_BOUND_BUY_FLIP"),
                no_order_publication=report.get("offline_order_publication_attempts", 0) == 0,
                native_permission="UNKNOWN")
        if combined_v2:
            report.update(keepwatch_enabled=True, offline_phone_delivery="NOT_EXECUTED_NO_ACK",
                pending_keepwatch_tasks=sum(not task.done() for task in bot._keepwatch_bar_tasks.values()),
                pending_owner_phone_task=getattr(bot, "_brokerdecides1_alert_task", None) is not None)
        return report
    except BaseException:
        if controlled:
            factory.nonce_lease.fail_boot_arm()
        raise
    finally:
        _RUNNING = False


def _bind_v2_boot_inputs(packet, blobs, review):
    """Revalidate V9 + raw V2 roles on every call, before any target access.

    The actual decoder supplies the line callback. No caller-provided report,
    dataclass, source-absence declaration or arbitrary callback is a shortcut.
    The returned public stage omits executable callbacks and snapshot objects.
    """
    from tools.l12_v2_native_semantics import normalize_v2_native

    if isinstance(review, dict) and set(review) == {"role_review", "controlled_native_review"}:
        return _bind_c11929_controlled_boot_inputs(packet, blobs, review)

    stage = {"status": "V2_PRIVATE_BOOT_INPUTS_UNKNOWN", "actual_close_boot": "UNRUN",
             "permission_granted": False}
    fields = {"role_review", "reviewed_native_accounts", "reviewed_schwab_scope",
              "reviewed_schwab_window_days", "http_statuses", "reviewed_webull_order_request_account"}
    if (not isinstance(review, dict) or set(review) != fields
            or not isinstance(review["role_review"], dict)
            or not isinstance(review["role_review"].get("execution_approval"), dict)
            or review["role_review"]["execution_approval"].get("binding_version") not in {
                "L12_WITH_L14_SCHEMA_V9", "L12_COMBINED_06936_GRAPHV2_BOOT_V1",
                "L12_COMBINED_359B_GRAPHV2_BOOT_V2", "L12_COMBINED_F278_GRAPHV2_BOOT_V3",
                "L12_COMBINED_FF41_GRAPHV2_BOOT_V4", "L12_COMBINED_4F0150_GRAPHV2_BOOT_V5"}):
        stage["failure_code"] = "EXPLICIT_V9_RAW_ROLE_REVIEW_REQUIRED"
        return stage, None
    normalized = normalize_v2_native(packet, blobs.__getitem__, **review)
    if normalized["status"] != "V2_NATIVE_SEMANTICS_BOUND_NOT_BOOT_OR_PERMISSION":
        stage.update({key: normalized[key] for key in ("failure_type", "failure_stage", "failure_code")
                      if key in normalized})
        return stage, None
    bound = normalized["role_boundary"]
    stage.update(status="V2_PRIVATE_BOOT_INPUTS_BOUND_NOT_STARTED",
        reviewed_boot_at_ms=review["role_review"]["reviewed_boot_ms"],
        scope_manifest_sha256=bound["scope_manifest_sha256"],
        source_schema_sha256=bound["source_schema_sha256"],
        execution_binding=bound["execution_binding"], epoch=bound["epoch"],
        roles=bound["roles"], admission_origin=bound["admission_origin"],
        line_raw_sha256=bound["line_restore"]["raw_sha256"],
        line_source_ordinals=bound["line_restore"]["source_ordinals"],
        line_coverage_supplied=bound["line_restore"]["coverage_supplied"],
        future_boot_freshness="NOT_ESTABLISHED_OR_RETIMED",
        local_evidence="NOT_ESTABLISHED_BY_NATIVE_BOOK")
    return stage, bound["line_restore"]["restore"]


def _bind_c11929_controlled_boot_inputs(packet, blobs, review):
    """C11929 only; actual durable chunks, explicit modeled native UNKNOWN.

    No exported line prefix is invented. Real boot store loaders and the
    reviewed recorded timeline remain the local-state reconstruction boundary.
    """
    from tools.l12_close_asof_contract import DURABLE, NATIVE, ROLES, is_sha
    from tools.l12_composed_execution_binding import (
        CURRENT_359_BOOT_VERSION, CURRENT_F_BOOT_VERSION, CORRECTED_L11_BOOT_VERSION,
        BOUNDED_L11_BOOT_VERSION, bind_composed_execution)
    from tools.l12_scoped_chunks_v2 import canonical, validate_v2
    from tools.l12_scoped_role_packet_v2 import TABLE_ROLE
    fields = {"manifest_raw", "metadata_raw", "chunk_review", "execution_approval",
        "observed_execution_root", "observed_component_pins", "reviewed_boot_ms"}
    acceptance_fields = {"version", "c1_ruling_sha256", "review_sha256", "manifest_sha256",
        "controlled_artifacts_sha256", "local_line_mode", "case_factory_sha256"}
    role = review["role_review"]
    accepted = review["controlled_native_review"]
    if (type(role) is not dict or set(role) != fields or type(accepted) is not dict
            or set(accepted) != acceptance_fields or accepted["version"] != C11929_CONTROLLED_VERSION
            or accepted["local_line_mode"] != "REAL_RESTORED_STORE_LOADERS_AND_RECORDED_CALLBACKS_NOT_EXPORTED_PREFIX"
            or any(not is_sha(accepted[key]) or accepted[key] == "0" * 64
                for key in ("c1_ruling_sha256", "review_sha256", "manifest_sha256", "controlled_artifacts_sha256",
                    "case_factory_sha256"))
            or type(role["execution_approval"]) is not dict
            or role["execution_approval"].get("binding_version") not in {
                CURRENT_359_BOOT_VERSION, CURRENT_F_BOOT_VERSION, CORRECTED_L11_BOOT_VERSION,
                BOUNDED_L11_BOOT_VERSION}):
        raise ValueError("C11929_exact_independent_controlled_boot_acceptance_required")
    execution = bind_composed_execution(role["execution_approval"], observed_root=role["observed_execution_root"],
        observed_pins=role["observed_component_pins"])
    plan = validate_v2(role["manifest_raw"], role["metadata_raw"], blobs.__getitem__, **role["chunk_review"])
    if accepted["manifest_sha256"] != plan["manifest_sha256"]:
        raise ValueError("C11929_actual_durable_manifest_mismatch")
    packet_fields = {"version", "purpose", "capture_head", "snapshot_id", "asof_ms", "source_schema_sha256",
        "execution_root", "accounts", "modeled_boot_ms", "variant", "working_provider", "working_symbol",
        "publisher_epoch", "artifacts"}
    if (type(packet) is not dict or set(packet) != packet_fields or packet["version"] != 2
            or packet["purpose"] != C11929_CONTROLLED_VERSION
            or packet["capture_head"] != role["chunk_review"]["expected_capture_head"]
            or packet["snapshot_id"] != plan["snapshot_id"] or packet["asof_ms"] != plan["asof_ms"]
            or packet["source_schema_sha256"] != plan["schema_sha256"]
            or packet["execution_root"] != execution["execution_root"]
            or type(role["reviewed_boot_ms"]) is not int or role["reviewed_boot_ms"] <= plan["asof_ms"]
            or packet["modeled_boot_ms"] != role["reviewed_boot_ms"]):
        raise ValueError("C11929_original_copy_separate_reviewed_model_clock_required")
    model = prepare_c11929_controlled_native(accounts=packet["accounts"], modeled_boot_ms=packet["modeled_boot_ms"],
        variant=packet["variant"], working_provider=packet["working_provider"], working_symbol=packet["working_symbol"])
    if (packet["artifacts"] != model["artifacts"] or packet["publisher_epoch"] != model["publisher_epoch"]
            or sha256(canonical(packet["artifacts"])).hexdigest() != accepted["controlled_artifacts_sha256"]
            or any(blobs.get(name) != raw for name, raw in model["blobs"].items())):
        raise ValueError("C11929_controlled_fixture_bytes_or_explicit_origin_mismatch")
    roles = {name: "CONTROLLED_UNKNOWN_NOT_NATIVE_COMPLETENESS" for name in NATIVE}
    for name in DURABLE:
        table = TABLE_ROLE.get(name)
        roles[name] = ("SOURCE_ABSENT_PRIVATE_CONSTRUCTION_NOT_SOURCE_COMPLETE" if table
            and plan["tables"][table]["source_state"] == "ABSENT_CANDIDATE_PROJECTION" else
            "ACTUAL_FULL_PK_MEMBERSHIP_BOUND_NOT_NATIVE_PROOF")
    roles.update(admission_snapshot="CONTROLLED_UNKNOWN_NOT_HISTORICAL_PUBLISHER",
        strategy_line_owner_notes="REAL_STORE_LOADERS_LINE_PREFIX_UNKNOWN_UNTIL_CALLBACK_OBSERVATION")
    if set(roles) != set(ROLES):
        raise ValueError("C11929_all_role_states_required_no_favorable_omission")

    async def restored_loader_boundary(case):
        if case.now_ms != role["reviewed_boot_ms"]:
            raise ValueError("C11929_reviewed_model_boot_clock_changed")
        # run() has just delegated the original STORES. Do not feed invented
        # line bars/coverage or set readiness; actual FILE callbacks follow.
        return None

    return dict(status="V2_PRIVATE_BOOT_INPUTS_BOUND_NOT_STARTED",
        native_evidence_mode=C11929_CONTROLLED_VERSION, controlled_variant=packet["variant"],
        reviewed_boot_at_ms=role["reviewed_boot_ms"], scope_manifest_sha256=plan["manifest_sha256"],
        source_schema_sha256=plan["schema_sha256"], execution_binding=execution, roles=roles,
        epoch=dict(scope="ACTUAL_DB_SNAPSHOT_ONLY_CONTROLLED_NATIVE_EPOCH_NOT_OBSERVED", snapshot_id=plan["snapshot_id"]),
        admission_origin="C11929_CONTROLLED_UNKNOWN_NOT_NATIVE",
        line_raw_sha256=None, line_source_ordinals=(), line_coverage_supplied=False,
        future_boot_freshness="UNKNOWN_NO_NATIVE_CLOCK_CLAIM", permission_granted=False), restored_loader_boundary
