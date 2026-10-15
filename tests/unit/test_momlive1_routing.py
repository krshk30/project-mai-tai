import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace

import pytest

from project_mai_tai.momentum_live_routing import (
    FOREIGN_POLICY_TYPES,
    MassiveTickerPages,
    RoutingSnapshot,
    RoutingStore,
    TickerPage,
    TypeBootstrapError,
    prepare_type_cache,
)

DAY = date(2026, 10, 14)
REASON = "Opening transactions for this security must be placed with a broker"


def clock(hour, minute=0, day=14):
    return datetime(2026, 10, day, hour + 4, minute, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return RoutingStore(tmp_path / "momentum-only.sqlite")


def prepare(store, rows, day=DAY):
    return asyncio.run(prepare_type_cache(store, lambda cursor: TickerPage(tuple(rows)), day))


@pytest.mark.parametrize(
    "hour,minute,broker,category",
    [
        (3, 59, None, "refused_hours"),
        (4, 0, "webull", "webull_before_07"),
        (6, 59, "webull", "webull_before_07"),
        (7, 0, "schwab", "schwab"),
        (15, 59, "schwab", "schwab"),
        (16, 0, None, "refused_hours"),
    ],
)
def test_exact_hours_missing_type_is_policy_not_country(store, hour, minute, broker, category):
    result = store.snapshot(DAY).route("SCNI", clock(hour, minute))
    assert (result.broker, result.category) == (broker, category)
    assert result.issuer_country == result.type_source == "UNKNOWN"


@pytest.mark.parametrize("code", sorted(FOREIGN_POLICY_TYPES))
def test_exact_approved_types(store, code):
    snapshot = prepare(store, [{"ticker": "FOREIGN", "type": code}])
    assert snapshot.route("FOREIGN", clock(7)).category == "webull_type"
    assert snapshot.route("FOREIGN", clock(6)).category == "webull_before_07"
    assert snapshot.route("FOREIGN", clock(7)).issuer_country == "UNKNOWN"


@pytest.mark.parametrize("code", ["CS", "ETF", "GDR", "ADRW", "OTHER", None])
def test_no_invented_broad_allowlist(store, code):
    snapshot = prepare(store, [{"ticker": "NAME", "type": code}])
    assert snapshot.route("NAME", clock(8)).category == "schwab"


def test_first_seen_restart_cross_day_no_retroactive_routing(store):
    assert store.record_ineligible("TDIC", clock(12, 1), REASON)
    assert not store.record_ineligible("TDIC", clock(13), REASON)
    restarted = RoutingStore(store.path)
    assert restarted.snapshot(DAY).route("TDIC", clock(11)).broker == "schwab"
    assert restarted.snapshot(DAY).route("TDIC", clock(12, 1)).category == "webull_ineligible"
    tomorrow = DAY + timedelta(days=1)
    assert (
        restarted.snapshot(tomorrow).route("TDIC", clock(8, day=15)).category == "webull_ineligible"
    )
    assert restarted.snapshot(DAY).ineligible_first_seen["TDIC"] == clock(12, 1)


@pytest.mark.parametrize(
    "reason",
    ["timeout", "rate limit", "insufficient funds", "unknown fill", "", "API key SECRET failed"],
)
def test_only_definitive_shared_classifier_persists(store, reason):
    assert not store.record_ineligible("TDIC", clock(12), reason)
    assert not store.snapshot(DAY).ineligible_first_seen


def test_ineligible_precedes_type_and_keeps_earliest_under_concurrency(store):
    prepare(store, [{"ticker": "TDIC", "type": "ADRC"}])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda minute: store.record_ineligible("TDIC", clock(12, minute), REASON),
                reversed(range(8)),
            )
        )
    assert store.snapshot(DAY).ineligible_first_seen["TDIC"] == clock(12)
    assert store.snapshot(DAY).route("TDIC", clock(13)).category == "webull_ineligible"


def test_route_is_immutable_and_never_opens_db(store, monkeypatch):
    snapshot = prepare(store, [{"ticker": "F", "type": "OS"}])
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("hot-path I/O"))
    assert snapshot.route("F", clock(8)).broker == "webull"
    with pytest.raises(TypeError):
        snapshot.types["F"] = "CS"


def test_type_conflict_not_country_guess_and_old_day_not_reused(store):
    snapshot = prepare(store, [{"ticker": "F", "type": "ADRC"}, {"ticker": "F", "type": "CS"}])
    decision = snapshot.route("F", clock(8))
    assert decision.broker == "schwab" and decision.type_source == "CONFLICT"
    assert decision.issuer_country == "UNKNOWN"
    stale = prepare(
        RoutingStore(store.path.parent / "other.sqlite"), [{"ticker": "F", "type": "ADRC"}]
    )
    assert stale.route("F", clock(8, day=15)).type_source == "UNKNOWN"
    assert stale.route("F", clock(8, day=15)).broker == "schwab"


def test_paginated_prepare_once_across_restart(store):
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        return TickerPage(
            ({"ticker": "A" if cursor is None else "B", "type": "OS"},),
            "next" if cursor is None else None,
        )

    snapshot = asyncio.run(prepare_type_cache(store, fetch, DAY))
    assert calls == [None, "next"] and snapshot.type_status == "READY"
    asyncio.run(prepare_type_cache(RoutingStore(store.path), fetch, DAY))
    assert calls == [None, "next"]


def test_concurrent_prepare_only_one_logical_pull(store):
    calls = []

    async def run():
        def fetch(cursor):
            calls.append(cursor)
            return TickerPage(({"ticker": "A", "type": "CS"},))

        await asyncio.gather(*(prepare_type_cache(store, fetch, DAY) for _ in range(8)))

    asyncio.run(run())
    assert calls == [None] and store.snapshot(DAY).type_status == "READY"


def test_provider_is_off_event_loop(store):
    def fetch(cursor):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return TickerPage(())

    assert asyncio.run(prepare_type_cache(store, fetch, DAY)).type_status == "READY"


@pytest.mark.parametrize(
    "mode", ["pages", "rows", "cycle", "bad_symbol", "bad_type", "secret_exception", "page_rows"]
)
def test_bounded_failure_never_saves_partial_or_retries_same_day(store, mode):
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        if mode == "secret_exception":
            raise RuntimeError("API_KEY_SECRET")
        rows = ({"ticker": "A", "type": "CS"},)
        if mode == "bad_symbol":
            rows = ({"ticker": "apikey=SECRET", "type": "CS"},)
        if mode == "bad_type":
            rows = ({"ticker": "A", "type": "secret=KEY"},)
        if mode == "rows":
            rows *= 2
        if mode == "page_rows":
            rows *= 1001
        return TickerPage(rows, "loop" if mode in {"pages", "cycle"} else None)

    with pytest.raises(TypeBootstrapError) as error:
        asyncio.run(
            prepare_type_cache(
                store, fetch, DAY, max_pages=2, max_rows=1 if mode == "rows" else 2000
            )
        )
    assert "SECRET" not in str(error.value) and "KEY" not in str(error.value)
    snapshot = store.snapshot(DAY)
    assert snapshot.type_status == "FAILED" and not snapshot.types
    prior = len(calls)
    asyncio.run(prepare_type_cache(store, fetch, DAY))
    assert len(calls) == prior


def test_failed_day_does_not_poison_next_day(store):
    with pytest.raises(TypeBootstrapError):
        asyncio.run(
            prepare_type_cache(store, lambda cursor: (_ for _ in ()).throw(ValueError("fail")), DAY)
        )
    assert (
        prepare(store, [{"ticker": "F", "type": "ADRC"}], DAY + timedelta(days=1)).type_status
        == "READY"
    )


def test_massive_adapter_uses_bulk_raw_and_preserves_only_cursor():
    calls = []

    class Client:
        def list_tickers(self, **kwargs):
            calls.append(kwargs)
            return {
                "status": "OK",
                "results": [{"ticker": "TM", "type": "ADRC"}],
                "next_url": "https://api.massive.com/v3/reference/tickers?cursor=next&apiKey=SECRET",
            }

    page = MassiveTickerPages(Client(), DAY)(None)
    assert page.next_cursor == "next"
    MassiveTickerPages(Client(), DAY)(page.next_cursor)
    assert calls[0]["raw"] and calls[0]["market"] == "stocks" and calls[0]["limit"] == 1000
    assert calls[1]["params"] == {"cursor": "next"}
    assert "SECRET" not in repr(page)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.massive.com/v3/reference/tickers?cursor=a",
        "https://evil.test/v3/reference/tickers?cursor=a",
        "https://api.massive.com/orders?cursor=a",
        "https://api.massive.com/v3/reference/tickers?apiKey=SECRET",
    ],
)
def test_adapter_never_follows_untrusted_or_credential_only_url(url):
    class Client:
        def list_tickers(self, **kwargs):
            return {"status": "OK", "results": [], "next_url": url}

    with pytest.raises(TypeBootstrapError) as error:
        MassiveTickerPages(Client(), DAY)(None)
    assert "SECRET" not in str(error.value)


def test_adapter_sanitizes_provider_errors():
    class Client:
        def list_tickers(self, **kwargs):
            raise RuntimeError("SECRET apiKey")

    with pytest.raises(TypeBootstrapError, match="^type_page_unavailable$"):
        MassiveTickerPages(Client(), DAY)(None)


def test_reports_replace_fallback_idempotent_persistent_and_bounded(store):
    assert store.record_route("episode", DAY, "schwab")
    assert not store.record_route("episode", DAY, "schwab")
    assert store.record_route("episode", DAY, "schwab_refused_then_webull")
    for index, category in enumerate(
        ("webull_type", "webull_ineligible", "webull_before_07", "refused_hours")
    ):
        store.record_route(str(index), DAY, category)
    report = RoutingStore(store.path).daily_report(DAY)
    assert report["schwab"] == 0 and report["schwab_refused_then_webull"] == 1
    assert sum(report.values()) == 5
    with pytest.raises(ValueError, match="capacity"):
        store.record_route("overflow", DAY, "schwab", max_episodes=5)
    with pytest.raises(ValueError, match="conflicting"):
        store.record_route("episode", DAY, "webull_type")


def test_invalid_naive_times_symbols_and_weekend(store):
    with pytest.raises(ValueError):
        store.snapshot(DAY).route("TDIC", datetime(2026, 10, 14, 8))
    with pytest.raises(ValueError):
        store.record_ineligible("lower", clock(8), REASON)
    assert store.snapshot(date(2026, 10, 17)).route("TDIC", clock(8, day=17)).broker is None


def test_snapshot_constructed_without_io():
    snapshot = RoutingSnapshot(
        DAY, MappingProxyType({}), frozenset(), MappingProxyType({}), "MISSING"
    )
    assert snapshot.route("MISSING", clock(8)).category == "schwab"


def test_store_refuses_other_strategy_file_without_creating_tables(tmp_path):
    path = tmp_path / "other-strategy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE atr (position TEXT)")
    with pytest.raises(ValueError, match="wrong_routing_store_owner"):
        RoutingStore(path)
    with sqlite3.connect(path) as db:
        assert list(db.execute("SELECT name FROM sqlite_master WHERE type='table'")) == [("atr",)]


def test_manual_snapshot_copies_input_and_holiday_is_closed():
    values = {"F": "ADRC"}
    snapshot = RoutingSnapshot(DAY, values, frozenset(), {}, "READY")
    values["F"] = "CS"
    assert snapshot.route("F", clock(8)).broker == "webull"
    holiday = datetime(2026, 12, 25, 13, tzinfo=timezone.utc)
    assert snapshot.route("F", holiday).category == "refused_hours"


def test_adapter_parses_raw_bytes_without_any_client_creation():
    class Client:
        def list_tickers(self, **kwargs):
            return SimpleNamespace(data=b'{"status":"OK","results":[{"ticker":"F","type":"OS"}]}')

    assert MassiveTickerPages(Client(), DAY)(None).rows == ({"ticker": "F", "type": "OS"},)


def test_atomic_type_persistence_failure_preserves_ineligible(store, monkeypatch):
    store.record_ineligible("TDIC", clock(8), REASON)
    original = store._save_types

    def fail(*args):
        with store._connect() as db:
            db.execute("INSERT INTO types VALUES (?,?,?,?)", (DAY.isoformat(), "BAD", "CS", 0))
            raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(store, "_save_types", fail)
    with pytest.raises(TypeBootstrapError):
        prepare(store, [{"ticker": "F", "type": "OS"}])
    assert not store.snapshot(DAY).types
    assert store.snapshot(DAY).ineligible_first_seen["TDIC"] == clock(8)
    monkeypatch.setattr(store, "_save_types", original)


@pytest.mark.parametrize(
    "row",
    [
        {"ticker": "F", "type": "OS", "market": "crypto"},
        {"ticker": "F", "type": "OS", "active": False},
    ],
)
def test_wrong_provider_market_or_inactive_is_not_cached(store, row):
    with pytest.raises(TypeBootstrapError):
        prepare(store, [row])
    assert not store.snapshot(DAY).types


def test_seed_original_first_date_account_reason_no_other_account_or_ttl_write(store):
    rows = [
        {
            "symbol": "TDIC",
            "first_seen_at": clock(12, 1).isoformat(),
            "broker_account_id": "atr-account",
            "reason_text": REASON,
        },
        {
            "symbol": "TDIC",
            "first_seen_at": clock(13).isoformat(),
            "broker_account_id": "atr-account",
            "reason_text": REASON,
        },
        {
            "symbol": "OTHER",
            "first_seen_at": clock(12),
            "broker_account_id": "other-account",
            "reason_text": REASON,
        },
    ]
    assert store.seed_existing_ineligible(rows, source_account_id="atr-account") == 1
    assert store.seed_existing_ineligible(rows, source_account_id="atr-account") == 0
    snapshot = RoutingStore(store.path).snapshot(DAY + timedelta(days=1))
    assert snapshot.ineligible_first_seen == {"TDIC": clock(12, 1)}
    assert snapshot.ineligible_provenance["TDIC"] == {
        "source_account_id": "atr-account",
        "reason": "must be placed with a broker",
        "source": "schwab_ineligible_today",
    }


def test_pending_preparation_survives_restart_without_second_pull(store):
    assert store._claim_preparation(DAY)
    snapshot = asyncio.run(
        prepare_type_cache(RoutingStore(store.path), lambda cursor: pytest.fail("second pull"), DAY)
    )
    assert snapshot.type_status == "PENDING" and not snapshot.types


def test_simultaneous_report_capacity_is_transactional(store):
    def record(index):
        try:
            return store.record_route(str(index), DAY, "schwab", max_episodes=2)
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(record, range(8))) == 2
    assert store.daily_report(DAY)["schwab"] == 2
