from __future__ import annotations

import json
import shlex
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import prune_market_ticks as mod


NOW = datetime(2026, 10, 8, 21, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.real = conn.db.cursor()
        self.answer = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.real.close()

    def execute(self, sql, params=()):
        self.conn.calls.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            self.answer = (self.conn.lock_available,)
        elif "pg_advisory_unlock" in sql:
            self.answer = (True,)
        elif "pg_total_relation_size" in sql:
            self.answer = (12345,)
        elif sql.startswith("VACUUM"):
            self.answer = None
        else:
            bound = tuple(value.isoformat() if isinstance(value, datetime) else value for value in params)
            self.real.execute(sql.replace("%s", "?"), bound)
            self.rowcount = self.real.rowcount

    def fetchone(self):
        return self.answer if self.answer is not None else self.real.fetchone()


class Connection:
    """Execute delete/count SQL against real rows; mock only PostgreSQL-specific facilities."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.calls = []
        self.lock_available = True
        self.db.executescript("""
            CREATE TABLE reconciliation_runs (id TEXT PRIMARY KEY, started_at TEXT);
            CREATE TABLE reconciliation_findings (id TEXT PRIMARY KEY, created_at TEXT,
              reconciliation_run_id TEXT REFERENCES reconciliation_runs(id));
            CREATE TABLE momentum_paper_events (id TEXT PRIMARY KEY, observed_at TEXT, event_type TEXT);
            CREATE TABLE dashboard_snapshots (id TEXT PRIMARY KEY, created_at TEXT, snapshot_type TEXT);
            CREATE TABLE strategy_bar_history (id TEXT PRIMARY KEY, bar_time TEXT, created_at TEXT);
            CREATE TABLE scanner_confirmed_events (id INTEGER PRIMARY KEY, event_at TEXT);
        """)
        for name in (*mod.TABLES, "market_capture_trades", "market_capture_quotes", "market_capture_bars"):
            self.db.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, received_at TEXT)")

    def cursor(self):
        return Cursor(self)

    def add(self, table, rows):
        for row in rows:
            values = tuple(value.isoformat() if isinstance(value, datetime) else value for value in row)
            self.db.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)})", values)

    def ids(self, table):
        return {row[0] for row in self.db.execute(f"SELECT id FROM {table}")}


@pytest.fixture
def conn():
    connection = Connection()
    yield connection
    connection.db.close()


@pytest.mark.parametrize("name", [
    "fills", "broker_orders", "broker_order_events", "trade_intents", "risk_checks",
    "oms_managed_positions", "account_positions", "dashboard_snapshots",
    "market_trade_ticks;DROP TABLE fills", "", "unknown",
])
def test_protected_tables_and_injection_refused(name):
    with pytest.raises(SystemExit):
        mod.parse_args(["--tables", name, "--go"])


@pytest.mark.parametrize("name,target", mod.TARGETS.items())
def test_retention_floor_cannot_be_lowered(name, target):
    with pytest.raises(SystemExit):
        mod.parse_args(["--tables", name, "--keep-days", str(target.minimum_days - 1)])


@pytest.mark.parametrize("events", ["FILLED", "FINAL", "EXITED", "DETECTED", "NO_FILL",
    "UNANSWERABLE", "PATH_PRINT,FINAL", "PATH_PRINT,PATH_PRINT", "", "PATH_PRINT) OR 1=1"])
def test_paper_filter_refuses_all_non_plumbing_events(events):
    with pytest.raises(SystemExit):
        mod.parse_args(["--tables", "momentum_paper_events", "--event-types", events])


@pytest.mark.parametrize("argv", [
    ["--tables", "market_trade_ticks,market_trade_ticks"], ["--batch", "0"],
    ["--batch", "50001"], ["--max-rows", "0"], ["--max-seconds", "0"],
    ["--keep-days", "7", "--keep-days", "8"], ["--keep-days", "fills=7"],
    ["--keep-days", "market_trade_ticks=7", "--keep-days", "market_trade_ticks=8"],
    ["--keep-days", "bad"], ["--event-types", "PATH_PRINT"], ["--go", "--dry-run"],
])
def test_ambiguous_or_unbounded_cli_refused(argv):
    with pytest.raises(SystemExit):
        mod.parse_args(argv)


def test_per_table_days_and_fk_order():
    args = mod.parse_args(["--tables", "strategy_bar_history,reconciliation_runs,reconciliation_findings",
                           "--keep-days", "reconciliation_runs=8", "--keep-days", "strategy_bar_history=45"])
    assert args.tables == ("reconciliation_findings", "reconciliation_runs", "strategy_bar_history")
    assert args.keep_days == {"reconciliation_findings": 7, "reconciliation_runs": 8,
                              "strategy_bar_history": 45}
    assert args.go is False


def test_operator_retention_policy_is_pinned_independently_of_implementation():
    assert {name: target.minimum_days for name, target in mod.TARGETS.items()} == {
        "market_trade_ticks": 7, "market_quote_ticks": 7,
        "market_capture_trades": 7, "market_capture_quotes": 7, "market_capture_bars": 7,
        "reconciliation_findings": 7, "reconciliation_runs": 7,
        "scanner_cycle_history": 7, "scanner_confirmed_events": 7,
        "momentum_paper_events": 1, "strategy_bar_history": 45,
    }


def test_dry_run_is_default_no_delete_vacuum_or_writer_lock(conn, capsys):
    conn.add("market_trade_ticks", [(1, NOW - timedelta(days=8))])
    assert mod.run(conn, mod.parse_args([]), now=NOW) == 0
    assert conn.ids("market_trade_ticks") == {1}
    assert not any(sql.startswith(("DELETE", "VACUUM")) or "advisory" in sql for sql, _ in conn.calls)
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[0]["eligible"] == 1
    assert reports[0]["relation_bytes"] == 12345
    assert reports[-1]["dry_run"] is True


def test_paper_prune_preserves_every_other_event_and_exact_boundary(conn):
    old = NOW - timedelta(days=2)
    keep = ("FILLED", "EXITED", "FINAL", "DETECTED", "NO_FILL", "UNANSWERABLE",
            "SESSION_READY", "SESSION_CLOSED", "FUTURE_EVENT")
    conn.add("momentum_paper_events", [(kind, old, kind) for kind in keep])
    conn.add("momentum_paper_events", [(kind, old, kind) for kind in mod.PAPER_PLUMBING])
    conn.add("momentum_paper_events", [("boundary", NOW - timedelta(days=1), "PATH_PRINT"),
                                      ("recent", NOW, "FEED_GAP")])
    args = mod.parse_args(["--tables", "momentum_paper_events", "--go", "--batch", "1"])
    assert mod.run(conn, args, now=NOW) == 0
    assert conn.ids("momentum_paper_events") == set(keep) | {"boundary", "recent"}
    assert sum(sql.startswith("VACUUM") for sql, _ in conn.calls) == 1


def test_paper_event_subset_applies_to_count_and_delete(conn):
    conn.add("momentum_paper_events", [(kind, NOW - timedelta(days=2), kind) for kind in mod.PAPER_PLUMBING])
    args = mod.parse_args(["--tables", "momentum_paper_events", "--event-types", "FEED_GAP", "--go"])
    assert mod.run(conn, args, now=NOW) == 0
    assert conn.ids("momentum_paper_events") == {"PATH_PRINT"}


def test_scanner_history_alias_never_deletes_trade_state(conn):
    old = NOW - timedelta(days=8)
    kinds = ("atr_reprice_handoff", "oms_webull_mirror_retained_hold", "scanner_alert_engine_state",
             "scanner_confirmed_last_nonempty", "scanner_cycle_history_extra")
    conn.add("dashboard_snapshots", [(kind, old, kind) for kind in kinds])
    conn.add("dashboard_snapshots", [("old-scanner", old, "scanner_cycle_history"),
                                    ("current-scanner", NOW, "scanner_cycle_history")])
    assert mod.run(conn, mod.parse_args(["--tables", "scanner_cycle_history", "--go"]), now=NOW) == 0
    assert conn.ids("dashboard_snapshots") == set(kinds) | {"current-scanner"}


def test_findings_then_runs_and_recent_child_keeps_old_parent(conn):
    old = NOW - timedelta(days=8)
    conn.add("reconciliation_runs", [("released", old), ("held", old), ("current", NOW)])
    conn.add("reconciliation_findings", [("old-child", old, "released"), ("new-child", NOW, "held")])
    args = mod.parse_args(["--tables", "reconciliation_runs,reconciliation_findings", "--go"])
    assert mod.run(conn, args, now=NOW) == 0
    assert conn.ids("reconciliation_runs") == {"held", "current"}
    assert conn.ids("reconciliation_findings") == {"new-child"}
    deletes = [sql for sql, _ in conn.calls if sql.startswith("DELETE")]
    assert "reconciliation_findings AS r" in deletes[0]
    assert "NOT EXISTS" in deletes[-1]


def test_bars_age_on_bar_time_preserves_250_seed_bars(conn):
    rows = [(f"seed-{i}", NOW - timedelta(minutes=i), NOW - timedelta(days=50)) for i in range(250)]
    rows += [("old-backfill", NOW - timedelta(days=46), NOW),
             ("boundary", NOW - timedelta(days=45), NOW)]
    conn.add("strategy_bar_history", rows)
    assert mod.run(conn, mod.parse_args(["--tables", "strategy_bar_history", "--go"]), now=NOW) == 0
    assert conn.ids("strategy_bar_history") == {f"seed-{i}" for i in range(250)} | {"boundary"}


def test_session_scanner_events_age_on_event_time_not_write_time(conn):
    conn.add("scanner_confirmed_events", [(1, NOW - timedelta(days=8)), (2, NOW)])
    assert mod.run(conn, mod.parse_args(["--tables", "scanner_confirmed_events", "--go"]), now=NOW) == 0
    assert conn.ids("scanner_confirmed_events") == {2}


def test_shared_row_cap_bounds_all_tables_and_reports_incomplete(conn, capsys):
    old = NOW - timedelta(days=8)
    for table in mod.TABLES:
        conn.add(table, [(i, old) for i in range(5)])
    args = mod.parse_args(["--go", "--batch", "2", "--max-rows", "3"])
    assert mod.run(conn, args, now=NOW) == 3
    assert len(conn.ids("market_trade_ticks")) + len(conn.ids("market_quote_ticks")) == 7
    assert len([sql for sql, _ in conn.calls if sql.startswith("DELETE")]) == 2
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["stage"] == "bounded-incomplete"


def test_runtime_budget_stops_without_deleting_or_vacuuming(conn):
    conn.add("market_trade_ticks", [(1, NOW - timedelta(days=8))])
    turns = iter((0, 2, 2))
    args = mod.parse_args(["--go", "--max-seconds", "1"])
    assert mod.run(conn, args, now=NOW, monotonic=lambda: next(turns)) == 3
    assert conn.ids("market_trade_ticks") == {1}
    assert not any(sql.startswith(("DELETE", "VACUUM")) for sql, _ in conn.calls)


def test_writer_lock_refusal_precedes_any_delete(conn):
    conn.lock_available = False
    with pytest.raises(RuntimeError, match="another retention writer"):
        mod.run(conn, mod.parse_args(["--go"]), now=NOW)
    assert len(conn.calls) == 1


def test_exception_unlocks_and_never_claims_complete(conn, monkeypatch, capsys):
    def failure(*_):
        raise RuntimeError("count failed")
    monkeypatch.setattr(mod, "_count", failure)
    with pytest.raises(RuntimeError, match="count failed"):
        mod.run(conn, mod.parse_args(["--go"]), now=NOW)
    assert "pg_advisory_unlock" in conn.calls[-1][0]
    assert "complete" not in capsys.readouterr().out


def test_main_connects_readonly_by_default_and_go_uses_autocommit(monkeypatch):
    calls = []
    class FakeConnection:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
    def connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return FakeConnection()
    monkeypatch.setattr(mod.psycopg, "connect", connect)
    monkeypatch.setattr(mod, "run", lambda *_: 0)
    assert mod.main(["--dsn", "postgresql+psycopg://local"]) == 0
    assert "default_transaction_read_only=on" in calls[-1][1]["options"]
    assert mod.main(["--dsn", "postgresql://local", "--go"]) == 0
    assert "read_only" not in calls[-1][1]["options"]
    assert calls[-1][1]["autocommit"] is True
    assert "lock_timeout=1000" in calls[-1][1]["options"]
    assert "statement_timeout=60000" in calls[-1][1]["options"]


def test_unit_roles_cover_every_target_once_and_have_go_and_bounds():
    covered = []
    for name in ("prune-ticks", "prune-capture", "prune-retention-weekly"):
        source = (ROOT / "ops/systemd" / f"project-mai-tai-{name}.service").read_text()
        command = next(line.split("=", 1)[1] for line in source.splitlines() if line.startswith("ExecStart="))
        words = shlex.split(command)
        args = mod.parse_args(words[2:])
        assert args.go is True
        assert args.batch == 10000
        assert args.max_seconds == 3600
        assert "TimeoutStartSec=2h" in source
        covered.extend(args.tables)
    assert len(covered) == len(set(covered))
    assert set(covered) == set(mod.TARGETS)


@pytest.mark.parametrize("name,calendar", [
    ("prune-ticks", "*-*-* 05:00:00 America/New_York"),
    ("prune-capture", "Sun *-*-* 05:30:00 America/New_York"),
    ("prune-retention-weekly", "Sun *-*-* 05:45:00 America/New_York"),
])
def test_calendar_is_explicit_et_no_missed_run_catchup(name, calendar):
    source = (ROOT / "ops/systemd" / f"project-mai-tai-{name}.timer").read_text()
    assert f"OnCalendar={calendar}" in source
    assert "Persistent=false" in source
    assert source.count("OnCalendar=") == 1
