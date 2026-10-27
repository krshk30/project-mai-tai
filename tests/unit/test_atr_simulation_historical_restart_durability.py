"""UNRUN CONTROLLED SQLite transaction-boundary tests, NOT actual173/PG proof.

The fixture is a manually typed prepared-publication shape, not an authorized
producer receipt or an engine result. No model or preparation/replay is called.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date
import hashlib
import json
from threading import Event

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from project_mai_tai.atr_simulation.firstbar_read_model import firstbar_display_view
from project_mai_tai.atr_simulation.firstbar_study_publication import (
    CHUNK_BYTES, VERSION, HistoricalPublication, publish_historical_publication,
)
from project_mai_tai.atr_simulation.models import consumers, inputs, metadata, rows, runs
from project_mai_tai.atr_simulation.repository import SimulationRepository, SourceConflict
from project_mai_tai.atr_simulation.store_schema_20261023 import FIRSTBAR_MODEL


DAY = date(2026, 10, 23)
CONTROLLED = "CONTROLLED_METHOD_BOUNDARY_FIXTURE_NOT_AUTHORIZED_PRODUCER"

# Independent literal CONTROLLED-byte goldens, not repository/output digests.
RAW_FIXTURE_SHA256 = "63effca1b39ba9b0b3def7dea9622a3768e91d38035c83ea924d7883dc315dcc"
CAPTURE_GOLDENS = {
    "0": {
        "event_key": "273a06365120de108f9ad9a038204d3a937d80f39fa6a9a621e1b3741576b20d",
        "payload_sha256": "6cdeaf7999602888c5f385d317c2ae7bafe1ab48820524087162f1300a207a20",
    },
    "1": {
        "event_key": "dd14fbb83050237bc51f8898c0efec6ac80fd31fafb2a4f55e645ff7c73139a7",
        "payload_sha256": "dc7cc2a42582688352f13ea70cb9fe55420d0076937efe13949151be5e022cd4",
    },
}
RESULT_ROWS_SHA256 = "c366d6eec4bb9be310ab72872d4deb0d8fb428dc5511041cb15a38912dfcc61c"
ROW_GOLDENS = {
    "FIRSTBAR": {
        "payload_sha256": "c0f34b20c06491e87503fbfc3e5023cefb48df4a74e015515530d45fe487a7c5",
        "row_key": "f8beb468435a141c1be07b6ca4306d059f1d534d0d4a7b247e1e6e822fb6ef67",
    },
    "FIRSTBAR_X3": {
        "payload_sha256": "4a1e2e3016ef82b4e91da01949cf2451932c7ff039fdc35264bede01e08b5444",
        "row_key": "277e4bf3529349e319db24621283e7ca1c611f376046543b546c89e49bb510b4",
    },
}


class ControlledACKLost(RuntimeError):
    pass


class ControlledPublishFailed(RuntimeError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def prepared_fixture():
    # Two chunks, original CRLF/spacing/UTF-8 retained rather than reserialized.
    raw = (b'{\r\n  "CONTROLLED": "NOT_A_REAL_173_RECEIPT", "padding": "'
        + b"x" * (CHUNK_BYTES + 17) + b'", "utf8": "\xc3\xa9"\r\n}\r\n')
    checksum = hashlib.sha256(raw).hexdigest()
    assert CHUNK_BYTES == 1048576 and len(raw) == 1048670
    assert checksum == RAW_FIXTURE_SHA256
    assert FIRSTBAR_MODEL == "MODELED_STOP_AT_OBSERVED_PRINT_VOLUME"
    identity = {"trading_day": DAY.isoformat(), "symbol": "CONTROLLED",
        "broker": "CONTROLLED", "account_id": "CONTROLLED-account", "strategy": "CONTROLLED-ATR",
        "flip_id": "CONTROLLED-flip", "segment_id": "CONTROLLED-segment"}
    position = {"first_fill_ts": None, "fills": [], "vwap": None,
        "filled_qty": 0, "remainder_cancel_reason": None}
    canonical_rows = [{"identity": deepcopy(identity), "mode": world, "model": FIRSTBAR_MODEL,
        "decision": "UNKNOWN", "gross_pnl": None, "exits": [],
        "issues": [CONTROLLED], "position": deepcopy(position), **deepcopy(position)}
        for world in ("FIRSTBAR", "FIRSTBAR_X3")]
    document = {"version": VERSION, "source_id": "firstbar-study:" + checksum,
        "input_sha256": checksum, "policy_version": CONTROLLED,
        "source_refs": [{"artifact": "CONTROLLED-memory-fixture", "sha256": checksum,
            "pointer": "$", "kind": "CONTROLLED"}],
        "coverage": {"complete": None, "native_asof": "UNKNOWN", "gaps": [CONTROLLED],
            "qualification": "UNQUALIFIED_REPLAY", "historical_study": {
                "selected_cases": [{"CONTROLLED": True}], "reference_exclusions": []}},
        "result_rows": canonical_rows}
    publication = encoded(document)
    return HistoricalPublication(raw, checksum, publication, hashlib.sha256(publication).hexdigest())


def document_of(prepared):
    return json.loads(prepared.publication_raw)


@contextmanager
def opened(path, repository_type=SimulationRepository):
    # Every invocation uses fresh connections; no in-memory DB or shared session.
    engine = sa.create_engine(sa.URL.create("sqlite+pysqlite", database=str(path)),
        poolclass=NullPool, connect_args={"timeout": 2})
    try:
        yield repository_type(engine)
    finally:
        engine.dispose()


def new_database(tmp_path):
    path = tmp_path / "CONTROLLED-atrsim-restart.sqlite3"
    assert not path.exists()
    with opened(path) as repository:
        metadata.create_all(repository.engine)
    return path


def state(repository, prepared):
    source_id = document_of(prepared)["source_id"]
    with repository.engine.connect() as connection:
        raw_rows = [dict(row) for row in connection.execute(sa.select(inputs)).mappings()]
        run_rows = [dict(row) for row in connection.execute(sa.select(runs)).mappings()]
        result_rows = [dict(row) for row in connection.execute(sa.select(rows)
            .order_by(rows.c.row_key)).mappings()]
        checkpoints = [dict(row) for row in connection.execute(sa.select(consumers)).mappings()]
    assert all(item["source_id"] == source_id for item in run_rows + result_rows)
    return {"inputs": sorted(raw_rows, key=lambda item: item["fields"]["offset"]),
        "runs": run_rows, "rows": result_rows, "consumers": checkpoints}


def assert_original_capture(snapshot, prepared):
    raw_rows = snapshot["inputs"]
    assert len(raw_rows) == (len(prepared.receipt_raw) + CHUNK_BYTES - 1) // CHUNK_BYTES
    assert [row["fields"]["offset"] for row in raw_rows] == list(range(0, len(prepared.receipt_raw), CHUNK_BYTES))
    rebuilt = b"".join(base64.b64decode(row["fields"]["bytes_base64"], validate=True) for row in raw_rows)
    assert rebuilt == prepared.receipt_raw and hashlib.sha256(rebuilt).hexdigest() == prepared.receipt_sha256
    for row in raw_rows:
        assert row["stream"] == "atrsim-study-raw:" + prepared.receipt_sha256
        assert row["event_key"] == CAPTURE_GOLDENS[row["stream_id"]]["event_key"]
        assert row["payload_sha256"] == CAPTURE_GOLDENS[row["stream_id"]]["payload_sha256"]
    checkpoint, = snapshot["consumers"]
    assert checkpoint["status"] == "CAPTURING_ASSEMBLER_UNBOUND"
    assert checkpoint["issues"] == ["HISTORICAL_STUDY_NOT_NATIVE_CAPTURE"]
    assert checkpoint["cursors"] == {"atrsim-study-raw:" + prepared.receipt_sha256: prepared.receipt_sha256}


def assert_published(snapshot, prepared):
    assert_original_capture(snapshot, prepared)
    expected = document_of(prepared)
    run, = snapshot["runs"]
    assert run["source_id"] == expected["source_id"] and run["input_sha256"] == prepared.receipt_sha256
    assert run["policy_version"] == expected["policy_version"]
    assert run["source_refs"] == expected["source_refs"] and run["coverage"] == expected["coverage"]
    assert run["result_sha256"] == RESULT_ROWS_SHA256
    assert len(snapshot["rows"]) == 2
    payloads = {row["payload"]["mode"]: row["payload"] for row in snapshot["rows"]}
    assert payloads == {row["mode"]: row for row in expected["result_rows"]}
    for row in snapshot["rows"]:
        assert row["payload_sha256"] == ROW_GOLDENS[row["mode"]]["payload_sha256"]
        assert row["row_key"] == ROW_GOLDENS[row["mode"]]["row_key"]
    assert all(row["payload"]["gross_pnl"] is None for row in snapshot["rows"])


def assert_unknown_view(repository, prepared):
    source_id = document_of(prepared)["source_id"]
    stored = repository.read(source_id, DAY, DAY, limit=10000)
    assert stored["run"] is None and stored["rows"] == []
    view = firstbar_display_view(stored, source_id=source_id)
    assert view["coverage_status"] == "UNKNOWN" and view["outcomes"] == []
    assert view["source"]["engine_dependency_head"] == "UNKNOWN"
    assert all(value["known_pnl_label"] == "UNKNOWN" and value["complete_requested_window"] is False
        for value in view["totals"].values())


class CaptureCommittedACKLost(SimulationRepository):
    def capture(self, *args, **kwargs):
        super().capture(*args, **kwargs)
        raise ControlledACKLost("CONTROLLED raw commit completed; caller received no ACK")


class FailBeforePublish(SimulationRepository):
    def publish(self, **kwargs):
        raise ControlledPublishFailed("CONTROLLED publication not started after raw commit")


class PublishCommittedACKLost(SimulationRepository):
    def publish(self, **kwargs):
        assert super().publish(**kwargs) is True
        raise ControlledACKLost("CONTROLLED publication committed; caller received no ACK")


@pytest.mark.parametrize("repository_type,error", [
    (CaptureCommittedACKLost, ControlledACKLost), (FailBeforePublish, ControlledPublishFailed)])
def test_committed_raw_survives_close_reopen_before_publication_and_retry(tmp_path, repository_type, error):
    prepared, path = prepared_fixture(), new_database(tmp_path)
    with opened(path, repository_type) as repository:
        with pytest.raises(error):
            publish_historical_publication(repository, prepared)
    with opened(path) as repository:
        interrupted = state(repository, prepared)
        assert_original_capture(interrupted, prepared)
        assert interrupted["runs"] == interrupted["rows"] == []
        assert_unknown_view(repository, prepared)
        acknowledgement = publish_historical_publication(repository, prepared)
        assert acknowledgement["inserted"] is True
        assert acknowledgement["fresh_database_readback"] == "UNRUN"
    with opened(path) as repository:
        recovered = state(repository, prepared)
        assert_published(recovered, prepared)
        assert recovered["inputs"] == interrupted["inputs"]
        assert recovered["consumers"][0]["revision"] == interrupted["consumers"][0]["revision"] + 1


def test_publish_commit_before_ack_loss_reopens_durably_then_retries_without_replacing_rows(tmp_path):
    prepared, path = prepared_fixture(), new_database(tmp_path)
    with opened(path, PublishCommittedACKLost) as repository:
        with pytest.raises(ControlledACKLost):
            publish_historical_publication(repository, prepared)
    with opened(path) as repository:
        committed = state(repository, prepared)
        assert_published(committed, prepared)
        acknowledgement = publish_historical_publication(repository, prepared)
        assert acknowledgement["inserted"] is False
        assert acknowledgement["fresh_database_readback"] == "UNRUN"
        assert acknowledgement["global_pnl_qualified"] is False
    with opened(path) as repository:
        retried = state(repository, prepared)
        assert_published(retried, prepared)
        for key in ("inputs", "runs", "rows"):
            assert retried[key] == committed[key]
        assert retried["consumers"][0]["revision"] == committed["consumers"][0]["revision"] + 1


def test_failure_after_actual_first_row_insert_rolls_back_publication_not_prior_raw_commit(tmp_path):
    prepared, path = prepared_fixture(), new_database(tmp_path)
    with opened(path) as repository:
        fired = []

        def fail_after_row(connection, cursor, statement, parameters, context, executemany):
            compiled = getattr(context, "compiled", None)
            node = getattr(compiled, "statement", None)
            if getattr(node, "is_insert", False) and node.table.name == rows.name:
                fired.append(True)
                raise ControlledPublishFailed("CONTROLLED after actual row insert, before commit")

        sa.event.listen(repository.engine, "after_cursor_execute", fail_after_row)
        try:
            with pytest.raises(ControlledPublishFailed):
                publish_historical_publication(repository, prepared)
        finally:
            sa.event.remove(repository.engine, "after_cursor_execute", fail_after_row)
        assert fired == [True]
    with opened(path) as repository:
        interrupted = state(repository, prepared)
        assert_original_capture(interrupted, prepared)
        assert interrupted["runs"] == interrupted["rows"] == []
        assert_unknown_view(repository, prepared)
        assert publish_historical_publication(repository, prepared)["inserted"] is True
    with opened(path) as repository:
        recovered = state(repository, prepared)
        assert_published(recovered, prepared)
        assert recovered["inputs"] == interrupted["inputs"]


@pytest.mark.parametrize("boundary", ["before_cursor_execute", "after_cursor_execute"])
def test_concurrent_separate_reader_sees_unknown_not_uncommitted_run_or_partial_rows(tmp_path, boundary):
    prepared, path = prepared_fixture(), new_database(tmp_path)
    entered, release = Event(), Event()
    with opened(path) as writer:
        first = []

        def pause_first_row(connection, cursor, statement, parameters, context, executemany):
            compiled = getattr(context, "compiled", None)
            node = getattr(compiled, "statement", None)
            if getattr(node, "is_insert", False) and node.table.name == rows.name and not first:
                first.append(True)
                entered.set()
                assert release.wait(5), "CONTROLLED boundary not released"

        sa.event.listen(writer.engine, boundary, pause_first_row)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(publish_historical_publication, writer, prepared)
                try:
                    assert entered.wait(5), "CONTROLLED writer did not reach row boundary"
                    with opened(path) as reader:
                        observed = state(reader, prepared)
                        assert_original_capture(observed, prepared)
                        assert observed["runs"] == observed["rows"] == []
                        assert_unknown_view(reader, prepared)
                    assert not pending.done()
                finally:
                    release.set()
                assert pending.result(timeout=5)["inserted"] is True
        finally:
            release.set()
            sa.event.remove(writer.engine, boundary, pause_first_row)
    with opened(path) as reader:
        assert_published(state(reader, prepared), prepared)


@pytest.mark.parametrize("change", ["coverage", "canonical_rows", "source_refs"])
def test_changed_retry_refuses_without_replacing_original_committed_evidence(tmp_path, change):
    prepared, path = prepared_fixture(), new_database(tmp_path)
    with opened(path) as repository:
        assert publish_historical_publication(repository, prepared)["inserted"] is True
    document = document_of(prepared)
    if change == "coverage":
        document["coverage"]["gaps"].append("CONTROLLED changed coverage")
    elif change == "canonical_rows":
        document["result_rows"][0]["issues"].append("CONTROLLED changed canonical row")
    else:
        document["source_refs"][0]["artifact"] = "CONTROLLED changed source"
    raw = encoded(document)
    changed = replace(prepared, publication_raw=raw, publication_sha256=hashlib.sha256(raw).hexdigest())
    with opened(path) as repository:
        before = state(repository, prepared)
        with pytest.raises(SourceConflict, match="sealed_source_replay_changed"):
            publish_historical_publication(repository, changed)
    with opened(path) as repository:
        after = state(repository, prepared)
        assert_published(after, prepared)
        for key in ("inputs", "runs", "rows"):
            assert after[key] == before[key]


def test_changed_raw_position_is_refused_and_capture_checkpoint_rolls_back(tmp_path):
    prepared, path = prepared_fixture(), new_database(tmp_path)
    with opened(path) as repository:
        assert publish_historical_publication(repository, prepared)["inserted"] is True
    with opened(path) as repository:
        before = state(repository, prepared)
        prior, = before["consumers"]
        original = before["inputs"][0]
        fields = deepcopy(original["fields"])
        fields["bytes_base64"] = base64.b64encode(b"CONTROLLED conflicting raw bytes").decode()
        with pytest.raises(SourceConflict, match="same_source_position_changed_payload"):
            repository.capture(prior["consumer_id"], prior["revision"], [{
                "stream": original["stream"], "stream_id": original["stream_id"], "fields": fields}],
                prior["cursors"], issues=["CONTROLLED conflict must not replace original issues"])
    with opened(path) as repository:
        assert state(repository, prepared) == before
