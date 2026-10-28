"""UNRUN: real read method with controlled DBAPI/results, no PG/model/provider.

These controls prove ordering/semantic contracts, NOT actual750ms/N4 success.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import json
import math
from threading import Event, Lock, Semaphore
from types import SimpleNamespace

from fastapi.responses import JSONResponse
from psycopg.pq import Format
from psycopg.types.json import JsonLoader
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from project_mai_tai.atr_simulation import firstbar_ui_reader as port
from project_mai_tai.atr_simulation.firstbar_binding import FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION
from project_mai_tai.atr_simulation.firstbar_read_model import firstbar_display_view
from project_mai_tai.services.atr_firstbar_routes import load_firstbar_display


SOURCE = "CONTROLLED-text-read-release"


def run_record():
    return {"source_id": SOURCE, "policy_version": FIRSTBAR_RESULT_VERSION,
        "coverage": {"engine_dependency_head": FIRSTBAR_ENGINE_HEAD,
            "engine_contract_version": FIRSTBAR_RESULT_VERSION, "complete": None,
            "reference_seed_binding": deepcopy(port.SEED_BINDING)},
        "source_refs": [{"sha256": value} for value in (
            port.CSV_SHA256, port.SPLIT_SHA256, port.SELECTOR_SHA256)]}


def skip_row():
    # Explicit CONTROLLED closed-world shape, not an engine invocation/result.
    return {"identity": {"trading_day": "2026-10-23", "symbol": "CONTROLLED",
        "broker": "schwab", "account_id": "controlled-account", "strategy": "ATR",
        "flip_id": "controlled-flip", "segment_id": "controlled-segment"},
        "mode": "FIRSTBAR", "model": "MODELED_STOP_AT_OBSERVED_PRINT_VOLUME",
        "decision": "SKIPPED_FALSE_FLIP", "outcome_kind": "SKIP",
        "outcome_id": "controlled-outcome", "issues": [], "entries": [], "fills": [], "exits": [],
        "gross_pnl": None, "first_fill_ts": None, "vwap": None, "filled_qty": 0,
        "remainder_cancel_reason": None, "position": None,
        "engine_dependency_head": FIRSTBAR_ENGINE_HEAD, "version": FIRSTBAR_RESULT_VERSION,
        "metadata_not_rendered": {"full_raw": ["CONTROLLED", {"one": 1}]}}


class ControlledResult:
    def __init__(self, connection, value, stage):
        self.connection, self.value, self.stage = connection, value, stage

    def mappings(self):
        return self

    def scalars(self):
        return self

    def one_or_none(self):
        return deepcopy(self.value)

    def all(self):
        assert self.connection.checked_out and not self.connection.rolled_back
        self.connection.engine.events.append(self.stage + "_all")
        return deepcopy(self.value)


class ControlledConnection:
    def __init__(self, engine):
        self.engine = engine
        self.checked_out = self.rolled_back = False
        self.dialect = SimpleNamespace(name="postgresql")
        self.executions = 0

        def loads(raw):
            assert self.rolled_back and not self.checked_out, "decode ran before rollback/checkin"
            assert isinstance(raw, bytes), "preserve native JSON loader bytes input"
            engine.events.append("decode")
            engine.decoded.append(raw)
            if engine.before_decode is not None:
                engine.before_decode()
            return engine.loads(raw)

        class CapturedJsonLoader(JsonLoader):
            _loads = staticmethod(loads)

        def get_loader(oid, format):
            assert self.checked_out and not self.rolled_back
            assert (oid, format) == (114, Format.TEXT)
            return CapturedJsonLoader

        raw = SimpleNamespace(adapters=SimpleNamespace(get_loader=get_loader), info=SimpleNamespace(encoding="utf-8"))
        raw.connection = raw
        self.connection = SimpleNamespace(driver_connection=raw)

    def __enter__(self):
        assert self.engine.slots.acquire(timeout=3), "controlled pool was not released"
        self.checked_out = True
        with self.engine.lock:
            self.engine.checked_out += 1
            self.engine.peak = max(self.engine.peak, self.engine.checked_out)
        self.engine.events.append("checkout")
        return self

    def __exit__(self, *unused):
        self.checked_out = False
        with self.engine.lock:
            self.engine.checked_out -= 1
        self.engine.slots.release()
        self.engine.events.append("checkin")

    def begin(self):
        return SimpleNamespace(rollback=self.rollback)

    def rollback(self):
        self.rolled_back = True
        self.engine.events.append("rollback")
        if self.engine.failure == "rollback":
            raise OSError("CONTROLLED rollback failure")

    def exec_driver_sql(self, sql):
        assert self.checked_out and not self.rolled_back
        self.engine.settings.append(sql)

    def execute(self, statement):
        assert self.checked_out and not self.rolled_back
        self.executions += 1
        compiled = statement.compile(dialect=postgresql.dialect())
        self.engine.statements.append((str(compiled), compiled.params))
        if self.engine.failure == self.executions:
            raise sa.exc.OperationalError(None, None, OSError("CONTROLLED statement refusal"))
        if self.executions == 1:
            return ControlledResult(self, self.engine.run, "run")
        if self.executions == 2:
            return ControlledResult(self, self.engine.texts, "payload")
        assert self.executions == 3, "unexpected fourth query"
        return ControlledResult(self, [], "workers")


class ControlledEngine:
    def __init__(self, texts, *, loads=json.loads, failure=None, before_decode=None):
        self.texts, self.loads, self.failure, self.before_decode = texts, loads, failure, before_decode
        self.run = run_record()
        self.events, self.decoded, self.settings, self.statements = [], [], [], []
        self.slots, self.lock = Semaphore(2), Lock()
        self.checked_out = self.peak = 0

    def connect(self):
        return ControlledConnection(self)


def reader(engine):
    return port.FirstBarSeedUIReader("postgresql+psycopg://CONTROLLED/unused",
        engine_factory=lambda *unused: engine)


def read(engine, *, limit=10000):
    return reader(engine).read(SOURCE, port.SEED_START, port.SEED_END, limit=limit)


def test_actual_reader_full_text_query_limits_and_release_before_decode():
    payload = skip_row()
    payload["metadata_not_rendered"]["full_raw"].append("M" * 10000)
    raw = json.dumps(payload, ensure_ascii=False)
    engine = ControlledEngine([raw])
    result = read(engine)
    assert result["read_status"] == "AVAILABLE" and result["rows"] == [payload]
    assert engine.events == ["checkout", "payload_all", "workers_all", "rollback", "checkin", "decode"]
    assert engine.settings == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        "SET LOCAL statement_timeout = '750ms'", "SET LOCAL lock_timeout = '250ms'"]
    sql, params = engine.statements[1]
    assert "CAST(atr_simulation_rows.payload AS TEXT)" in sql
    assert "ORDER BY atr_simulation_rows.day, atr_simulation_rows.row_key" in sql
    assert set(params.values()) == {SOURCE, port.SEED_START, port.SEED_END, 10001}
    assert engine.checked_out == 0 and len(engine.statements) == 3


def test_native_loader_and_api_bytes_parity_without_metadata_pruning():
    value = skip_row()
    value["metadata_not_rendered"]["full_raw"] += [9007199254740993, 1.25, None, False, "snowman \u2603"]
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    expected = JsonLoader(114).load(raw)
    result = read(ControlledEngine([raw.decode("utf-8")]))
    assert result["rows"] == [expected]
    expected_stored = {"run": run_record(), "rows": [expected], "truncated": False,
        "consumers": [], "workers_truncated": False, "read_status": "AVAILABLE", "read_issues": []}
    assert JSONResponse(result).body == JSONResponse(expected_stored).body
    # Independently stated public output of this one explicit SKIP fixture.
    # Neither side's expected value is derived by the production projector.
    expected_view = {
        "contract": "ATRSIM_FIRSTBAR_DISPLAY_V1",
        "source": {
            "source_id": "CONTROLLED-text-read-release",
            "policy_version": "ATRSIM_FIRSTBAR_REPLAY_V2_A2_CEILING_CAUSAL_STOP_PRINT",
            "engine_dependency_head": "e712a99800b9cfd716b406e027845f89567647fe",
            "population_label": "replay, in-sample; underlying capture completeness UNKNOWN",
            "qualification": "UNQUALIFIED_REPLAY",
        },
        "coverage_status": "UNKNOWN",
        "truncated": False,
        "outcomes": [{
            "mode": "FIRSTBAR",
            "model": "MODELED_STOP_AT_OBSERVED_PRINT_VOLUME",
            "population_label": "replay, in-sample; underlying capture completeness UNKNOWN",
            "identity": {
                "trading_day": "2026-10-23", "symbol": "CONTROLLED",
                "broker": "schwab", "account_id": "controlled-account", "strategy": "ATR",
                "flip_id": "controlled-flip", "segment_id": "controlled-segment",
            },
            "outcome_id": "controlled-outcome",
            "issues": [],
            "kind": "SKIP",
            "reason": "SKIPPED_FALSE_FLIP",
        }],
        "totals": {
            "FIRSTBAR": {"known_pnl_label": "UNKNOWN", "known_closed_trades": 0,
                "complete_requested_window": False},
            "FIRSTBAR_X3": {"known_pnl_label": "UNKNOWN", "known_closed_trades": 0,
                "complete_requested_window": False},
        },
    }
    expected_body = json.dumps(expected_view, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode("utf-8")
    projected = firstbar_display_view(result, source_id=SOURCE)
    native_projected = firstbar_display_view(expected_stored, source_id=SOURCE)
    assert projected == expected_view
    assert native_projected == expected_view
    assert JSONResponse(projected).body == expected_body
    assert JSONResponse(native_projected).body == expected_body
    assert result["rows"][0]["metadata_not_rendered"] == value["metadata_not_rendered"]


def test_connection_specific_json_loads_hook_not_replaced_by_builtin_policy():
    raw = '{"decimal": 1.2500, "big": 9007199254740993, "null": null, "keys": {"1": "one"}}'
    def configured_loads(value):
        return json.loads(value, parse_float=Decimal)
    result = read(ControlledEngine([raw], loads=configured_loads))
    assert result["rows"] == [{"decimal": Decimal("1.2500"), "big": 9007199254740993,
        "null": None, "keys": {"1": "one"}}]
    assert str(result["rows"][0]["decimal"]) == "1.2500"


@pytest.mark.parametrize("wire", [
    '{"duplicate":1,"duplicate":2}',
    '{"negative_zero":-0.0,"fraction":1.2500,"big":9007199254740993}',
    '{"unicode":"snowman \\u2603","keys":{"1":"one","false":"key"},"null":null}',
])
def test_key_number_and_unicode_semantics_match_existing_native_json_loader(wire):
    native = JsonLoader(114).load(wire.encode("utf-8"))
    result = read(ControlledEngine([wire]))
    assert result["rows"] == [native]
    assert JSONResponse(result["rows"]).body == JSONResponse([native]).body


@pytest.mark.parametrize("wire", [b'{"n":1}', bytearray(b'{"n":1}'), None])
def test_text_bytes_and_sql_null_keep_native_value_semantics(wire):
    expected = None if wire is None else JsonLoader(114).load(wire)
    result = read(ControlledEngine([wire]))
    assert result["rows"] == [expected]


def test_overflow_row_is_also_decoded_before_truncation_not_ignored():
    engine = ControlledEngine(['{"kept":1}', '{"overflow":2}'])
    result = read(engine, limit=1)
    assert result["rows"] == [{"kept": 1}] and result["truncated"] is True
    assert len(engine.decoded) == 2 and engine.events.index("checkin") < engine.events.index("decode")


def test_malformed_overflow_refuses_whole_page_and_releases_connection():
    engine = ControlledEngine(['{"kept":1}', '{"bad":'])
    result = read(engine, limit=1)
    assert result["read_status"] == "UNAVAILABLE" and result["rows"] == []
    assert result["read_issues"] == ["FIRSTBAR_STORAGE_UNAVAILABLE"]
    assert len(engine.decoded) == 2 and engine.checked_out == 0


@pytest.mark.parametrize("wire", ['{"bad":', b'{"bad":"\xff"}', '"\ud800"', "1" * 5000])
def test_json_utf8_and_integer_overflow_refusals_are_not_retried_or_completed(wire):
    engine = ControlledEngine([wire])
    result = read(engine)
    assert result["read_status"] == "UNAVAILABLE" and result["rows"] == []
    assert engine.checked_out == 0 and len(engine.statements) == 3
    assert engine.events.index("checkin") <= len(engine.events) - 1


def test_float_overflow_in_omitted_metadata_still_fails_fulltree_projection():
    value = skip_row()
    value["metadata_not_rendered"] = {"not_a_display_column": float("inf")}
    engine = ControlledEngine([json.dumps(value)])
    result = read(engine)
    assert math.isinf(result["rows"][0]["metadata_not_rendered"]["not_a_display_column"])
    with pytest.raises(ValueError):
        firstbar_display_view(result, source_id=SOURCE)
    context = asyncio.run(load_firstbar_display(reader(engine), source_id=SOURCE))
    assert context["read_status"] == "UNAVAILABLE" and context["view"]["outcomes"] == []


@pytest.mark.parametrize("failure", [2, 3, "rollback"])
def test_statement_workers_and_rollback_failures_keep_unknown_and_release(failure):
    engine = ControlledEngine(['{"not_decoded":1}'], failure=failure)
    result = read(engine)
    assert result["read_status"] == "UNAVAILABLE" and result["rows"] == []
    assert engine.decoded == [] and engine.checked_out == 0


def test_unbound_seed_never_fetches_or_decodes_payload():
    engine = ControlledEngine(['{"not_decoded":1}'])
    engine.run["coverage"]["reference_seed_binding"]["selected_signals"] = 174
    result = read(engine)
    assert result["read_issues"] == ["FIRSTBAR_REVIEWED_SEED_BINDING_UNKNOWN"]
    assert len(engine.statements) == 1 and not engine.decoded and engine.checked_out == 0


def test_empty_bound_page_preserves_no_rows_unknown_not_complete_zero():
    engine = ControlledEngine([])
    result = read(engine)
    assert result["read_status"] == "NO_ROWS_UNKNOWN" and result["rows"] == []
    assert result["read_issues"] == ["NO_ROWS_UNKNOWN"] and not engine.decoded


def test_four_held_decoders_do_not_retain_two_slot_checkout():
    started, release, lock = Event(), Event(), Lock()
    count = 0
    def held():
        nonlocal count
        with lock:
            count += 1
            if count == 4:
                started.set()
        assert release.wait(4), "controlled decode was not released"
    engine = ControlledEngine(['{"controlled":1}'], before_decode=held)
    shared = reader(engine)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(shared.read, SOURCE, port.SEED_START, port.SEED_END) for _ in range(4)]
        try:
            assert started.wait(3), "all four decoders must start despite two checkout slots"
            assert engine.checked_out == 0 and engine.peak <= 2
            assert all(not task.done() for task in pending)
        finally:
            release.set()
            results = [task.result(timeout=4) for task in pending]
    assert all(item["read_status"] == "AVAILABLE" for item in results)
    assert count == 4 and engine.checked_out == 0
