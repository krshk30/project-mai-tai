"""Controlled transaction lifecycle regression; no actual DB, tape or economics."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 28, 19, tzinfo=timezone.utc)


def controlled(monkeypatch, *, fail_capture=False, readonly="on"):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    collect = importlib.import_module("atrsimlive1_collect")
    coverage = importlib.import_module("atrsimlive1_data_coverage")
    symbols = ["CHSN", "ELPW", "POM"]
    rows = {kind: [] for kind in collect.queries.SOURCES}
    for index, symbol in enumerate(symbols):
        rows["bars"].append(dict(symbol=symbol, id=str(index)))
        rows["capture_bars"].append(dict(symbol=symbol, id=index, volume=100))
    rows["fills"] = [dict(symbol="ELPW", account_name=account, id=index)
                     for index, account in enumerate(("live:orb", "live:schwab_1m_v2"))]
    trades = {symbol: [dict(id=index * 10 + n, provider="massive", symbol=symbol,
        event_ts=NOW - timedelta(minutes=2, seconds=10-n), received_at=NOW,
        price=Decimal("2.00"), size=100, conditions="") for n in (1, 2)]
        for index, symbol in enumerate(symbols)}

    class Cursor:
        def __enter__(self):
            assert not connection.closed
            return self
        def __exit__(self, *args):
            return False
        def execute(self, sql, params=None):
            assert not connection.closed
            connection.commands.append(sql)
            result = []
            if sql == collect.queries.IDENTITY:
                result = [dict(source_asof=NOW, snapshot_id="CONTROLLED-SAME-RR",
                               read_only=readonly, isolation="repeatable read")]
            elif sql == collect.queries.ROSTER:
                result = [dict(symbol=symbol) for symbol in symbols]
            elif sql in collect.queries.SOURCES.values():
                kind = next(k for k, value in collect.queries.SOURCES.items() if value == sql)
                result = rows[kind]
            elif sql in collect.queries.CAPTURE_PAGES.values():
                if fail_capture:
                    raise RuntimeError("CONTROLLED source query failure")
                kind = next(k for k, value in collect.queries.CAPTURE_PAGES.items() if value == sql)
                if kind == "trades":
                    result = trades[params["symbol"]]
                    if params["after_ts"] is not None:
                        result = [row for row in result if (row["event_ts"], row["id"]) >
                                  (params["after_ts"], params["after_id"])]
                    result = result[:collect.queries.PAGE_SIZE + 1]
            self.description = [SimpleNamespace(name=key) for key in (result[0] if result else {})]
            self.result = [tuple(row[key.name] for key in self.description) for row in result]
        def fetchall(self):
            assert not connection.closed
            return self.result

    class Connection:
        def __init__(self):
            self.closed = False
            self.commands = []
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.closed = True
        def cursor(self):
            assert not self.closed
            return Cursor()

    connection = Connection()
    connects, logs = [], []
    def connect(dsn, **kwargs):
        assert dsn == "CONTROLLED-NOT-A-DSN"
        connects.append(kwargs)
        return connection
    def log(path, *, asof):
        assert connection.closed
        assert connection.commands[-1] == "ROLLBACK"
        logs.append((path, asof))
        return dict(status="UNKNOWN", issues=["CONTROLLED_NO_ACTUAL_LOG"])
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setattr(coverage, "capture_log", log)
    return collect, connection, connects, logs, rows, trades


def invoke(collect, processor):
    return collect.collect(dsn="CONTROLLED-NOT-A-DSN", processor=processor,
                           capture_log_path="CONTROLLED-NO-FILE", now=NOW)


def test_complete_population_one_snapshot_rollback_close_before_log_hash_and_processor(monkeypatch):
    collect, connection, connects, logs, rows, trades = controlled(monkeypatch)
    documents, query_counts = [], []
    def processor(data):
        assert connection.closed and connection.commands[-1] == "ROLLBACK"
        query_counts.append(len(connection.commands))
        documents.append(data)
        return data["symbols"][0]
    assert invoke(collect, processor) == ["CHSN", "ELPW", "POM"]
    assert len(connects) == 1 and connects[0] == dict(connect_timeout=5, autocommit=True)
    assert len(logs) == 1 and len(set(query_counts)) == 1
    assert all(data["snapshot_id"] == "CONTROLLED-SAME-RR" and data["source_asof"] == NOW for data in documents)
    assert all(data["rows"]["trades"] == trades[data["symbols"][0]] for data in documents)
    assert len(documents[1]["rows"]["fills"]) == 2  # Accounts share one symbol call.
    for data in documents:
        assert data["read_proof"]["full_read"] is True
        assert data["read_proof"]["truncated"] is False
        assert data["read_proof"]["row_count"] == 2
        assert data["read_proof"]["rows_sha256"] == collect.digest(data["rows"]["trades"])


def test_query_failure_rolls_back_closes_and_never_starts_processor(monkeypatch):
    collect, connection, _, logs, _, _ = controlled(monkeypatch, fail_capture=True)
    calls = []
    with pytest.raises(RuntimeError, match="CONTROLLED source query failure"):
        invoke(collect, lambda data: calls.append(data))
    assert connection.closed and connection.commands[-1] == "ROLLBACK"
    assert calls == logs == []


def test_total_materialized_cap_refuses_without_partial_processing(monkeypatch):
    collect, connection, _, logs, rows, trades = controlled(monkeypatch)
    base_bytes = sum(len(collect.encoded(value)) for value in rows.values())
    first_two = len(collect.encoded(trades["CHSN"])) + len(collect.encoded(trades["ELPW"]))
    monkeypatch.setattr(collect, "MAX_INPUT_BYTES", base_bytes + first_two - 1)
    calls = []
    with pytest.raises(ValueError, match="today_source_byte_cap"):
        invoke(collect, lambda data: calls.append(data))
    assert connection.closed and connection.commands[-1] == "ROLLBACK"
    assert calls == logs == []


def test_processor_failure_occurs_after_closed_transaction_and_no_query_reentry(monkeypatch):
    collect, connection, _, logs, _, _ = controlled(monkeypatch)
    last_commands = []
    def failed(data):
        assert connection.closed
        last_commands.extend(connection.commands)
        raise LookupError("CONTROLLED pure processing failure")
    with pytest.raises(LookupError, match="CONTROLLED pure processing failure"):
        invoke(collect, failed)
    assert connection.commands == last_commands and connection.commands[-1] == "ROLLBACK"
    assert len(logs) == 1


def test_paged_full_read_remains_complete_before_processing(monkeypatch):
    collect, connection, _, _, _, trades = controlled(monkeypatch)
    monkeypatch.setattr(collect.queries, "PAGE_SIZE", 1)
    documents = invoke(collect, lambda data: data)
    assert connection.closed and len(documents) == 3
    assert all(data["rows"]["trades"] == trades[data["symbols"][0]] for data in documents)
    assert all(data["read_proof"]["row_count"] == 2 for data in documents)


def test_non_readonly_identity_refuses_and_closes_without_processing(monkeypatch):
    collect, connection, _, logs, _, _ = controlled(monkeypatch, readonly="off")
    calls = []
    with pytest.raises(ValueError, match="actual_today_readonly_snapshot_required"):
        invoke(collect, lambda data: calls.append(data))
    assert connection.closed and connection.commands[-1] == "ROLLBACK"
    assert calls == logs == []


def test_schwab_bar_witness_survives_absent_massive_bars_without_cross_symbol_hash(monkeypatch):
    collect, connection, connects, _, rows, _ = controlled(monkeypatch)
    rows["capture_bars"].clear()  # Actual observed TODAY shape; never substitute zero coverage.
    rows["bars"] = [dict(symbol=symbol, id=f"{symbol}-{n}", volume=100 + n)
                    for symbol, count in (("CHSN", 2), ("ELPW", 3), ("POM", 1))
                    for n in range(count)]
    documents = invoke(collect, lambda data: data)
    assert connection.closed and len(connects) == 1
    assert [data["read_proof"]["bar_count"] for data in documents] == [2, 3, 1]
    for data in documents:
        proof = data["read_proof"]
        assert proof["bar_provider"] == "schwab"
        assert proof["bar_table"] == "strategy_bar_history"
        assert proof["bars_full_read"] is True and proof["bars_truncated"] is False
        assert proof["bars_sha256"] == collect.digest(data["rows"]["bars"])
        assert proof["bars_sha256"] != collect.digest(data["rows"]["capture_bars"])
        assert data["source_counts"]["capture_bars"] == 0
        assert {row["symbol"] for row in data["rows"]["bars"]} == set(data["symbols"])
