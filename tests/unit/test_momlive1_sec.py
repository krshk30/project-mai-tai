from __future__ import annotations

import asyncio
import json
import importlib.util
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.momlive_sec_country import MomliveSecCountry, MomliveSecRequestClock
from project_mai_tai.momentum_live_sec import (
    REQUEST_INTERVAL_NS,
    CountryRecord,
    CountryRouter,
    CountryStore,
    SecClient,
    SecError,
    classify_submission,
    parse_ticker_map,
)

DAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)  # 08:00 ET
NIGHT = datetime(2026, 10, 14, 22, tzinfo=UTC)
INDEX = "https://www.sec.gov/files/company_tickers.json"
CONTACT = "ControlledTests contact@example.invalid"


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'country.db'}")
    MomliveSecCountry.__table__.create(engine)
    MomliveSecRequestClock.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        session.add(MomliveSecRequestClock(id=1, last_request_ns=0))
    yield CountryStore(factory)
    engine.dispose()


def submission(*, business="CA", incorporation="DE", tickers=None, cik="1"):
    return {
        "cik": cik,
        "tickers": tickers or ["TEST"],
        "addresses": {"business": {"stateOrCountry": business}},
        "stateOfIncorporation": incorporation,
    }


def record(business="CA", incorporation="DE", symbol="TEST"):
    return classify_submission(
        symbol,
        "1",
        submission(business=business, incorporation=incorporation, tickers=[symbol]),
        as_of=NOW - timedelta(seconds=1),
        lookup_day=DAY,
    )


@pytest.mark.parametrize(
    "business,incorporation,expected",
    [
        ("CA", "DE", True),
        ("NY", "NY", True),
        ("DC", "DE", True),
        ("PR", "FL", True),
        ("F4", "E9", False),
        ("CN", "KY", False),
        ("CA", "E9", False),
        ("F4", "DE", False),
        ("CA", "DC", False),
        ("PR", "PR", False),
        ("Z4", "ZZ", False),
        (" ca ", "de", True),
        (None, "DE", None),
        ("CA", None, None),
        ("", "DE", None),
        ("New York", "DE", None),
    ],
)
def test_explicit_sec_field_policy(business, incorporation, expected):
    result = record(business, incorporation)
    assert result.is_us is expected
    assert result.status == ("INCOMPLETE" if expected is None else "READY")


@pytest.mark.parametrize(
    "body",
    [submission(cik="2"), submission(tickers=["OTHER"]), {"cik": True, "tickers": ["TEST"]}, []],
)
def test_submission_identity_refusal(body):
    with pytest.raises(SecError):
        classify_submission("TEST", "1", body, as_of=NOW, lookup_day=DAY)


def test_malformed_address_is_unknown_not_us():
    body = submission()
    body["addresses"] = []
    assert classify_submission("TEST", "1", body, as_of=NOW, lookup_day=DAY).is_us is None


def test_index_conflicts_and_cik_aliases():
    mapping, conflicts = parse_ticker_map(
        {
            "0": {"ticker": "TEST", "cik_str": 1},
            "1": {"ticker": "ALIAS", "cik_str": 1},
            "2": {"ticker": "TEST", "cik_str": 2},
        }
    )
    assert mapping == {"ALIAS": "0000000001"}
    assert conflicts == {"TEST"}


@pytest.mark.parametrize(
    "body", [[], {"0": []}, {"0": {"ticker": "TEST"}}, {"0": {"ticker": "TEST", "cik_str": 0}}]
)
def test_invalid_index(body):
    with pytest.raises(SecError):
        parse_ticker_map(body)


@pytest.mark.parametrize(
    "hour,kind,broker",
    [
        (8, "US", "schwab"),
        (8, "NON_US", "webull"),
        (8, "UNKNOWN", "webull"),
        (5, "US", "webull"),
        (7, "US", "schwab"),
        (4, "US", "webull"),
        (16, "US", None),
        (3, "US", None),
    ],
)
def test_routes_known_at_decision(store, hour, kind, broker):
    if kind != "UNKNOWN":
        store.put(replace(record("CA" if kind == "US" else "F4", "DE"), as_of=NOW.replace(hour=7)))
    router = CountryRouter(store)
    asyncio.run(router.start())
    result = router.route_for("TEST", NOW.replace(hour=hour + 4))
    assert result.broker == broker


def test_future_source_and_aware_time(store):
    store.put(replace(record(), as_of=NOW + timedelta(seconds=1)))
    router = CountryRouter(store)
    asyncio.run(router.start())
    result = router.route_for("TEST", NOW)
    assert result.broker == "webull" and result.is_us is None and result.status == "FUTURE"
    with pytest.raises(SecError, match="aware"):
        router.route_for("TEST", NOW.replace(tzinfo=None))


def test_unknown_logging_has_no_contact_and_is_bounded(store, caplog):
    router = CountryRouter(store, max_seen=1)
    with caplog.at_level("INFO"):
        router.route_for("TEST", NOW)
        router.route_for("TEST", NOW)
        router.route_for("OTHER", NOW)
    assert len(caplog.records) == 1
    assert "status=UNKNOWN" in caplog.text and CONTACT not in caplog.text


def test_country_store_restart_and_inconsistent_row(store):
    store.put(record())
    restarted = CountryStore(store.session_factory)
    assert restarted.get("TEST") == record()
    with pytest.raises(SecError, match="inconsistent"):
        store.put(replace(record(), is_us=False))
    with pytest.raises(SecError, match="classification"):
        store.put(replace(record(), status="PENDING"))


@pytest.mark.parametrize(
    "contact",
    [
        "",
        "someone@example.invalid",
        "Company <contact>",
        "Company contact@example.invalid\nX:bad",
        "Company contact@example.invalid\rbad",
    ],
)
def test_contact_is_required_and_not_in_error(store, contact):
    with pytest.raises(SecError) as error:
        SecClient(store, user_agent=contact)
    assert str(error.value) == "verified_sec_contact_required"


def fake_provider(tickers=None, body=None):
    calls = []
    tickers = tickers or {"0": {"ticker": "TEST", "cik_str": 1}}

    def get(url, headers):
        calls.append(url)
        assert headers["User-Agent"] == CONTACT
        return json.dumps(tickers if url == INDEX else body or submission()).encode()

    return get, calls


def test_lookup_once_per_day_restart(store):
    get, calls = fake_provider()
    client = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NOW)
    assert client.lookup("TEST").is_us is True
    assert client.lookup("TEST").is_us is True
    restarted = SecClient(
        CountryStore(store.session_factory), user_agent=CONTACT, http_get=get, clock=lambda: NOW
    )
    assert restarted.lookup("TEST").is_us is True
    assert len(calls) == 2


def test_no_cik_is_unknown_no_submission(store):
    get, calls = fake_provider()
    result = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NOW).lookup("MISSING")
    assert result.status == "NO_CIK" and result.is_us is None
    assert calls == [INDEX]


def test_failure_is_cached_no_contact_leak(store):
    def fail(url, headers):
        raise RuntimeError(headers["User-Agent"])

    result = SecClient(store, user_agent=CONTACT, http_get=fail, clock=lambda: NOW).lookup("TEST")
    assert result.status == "FETCH_FAILED" and result.is_us is None


def test_nightly_deduplicates_cik_and_reports_denominator(store):
    get, calls = fake_provider(
        {"0": {"ticker": "TEST", "cik_str": 1}, "1": {"ticker": "ALIAS", "cik_str": 1}},
        submission(tickers=["TEST", "ALIAS"]),
    )
    result = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NIGHT)._nightly(
        ["TEST", "ALIAS", "TEST", "MISSING"],
        lookup_day=DAY,
        universe_provenance="Controlled test subset",
    )
    assert len(calls) == result["requests"] == 2
    assert result["requested_symbols"] == result["completed_symbols"] == 3
    assert result["counts"] == {
        "US": 2,
        "NON_US": 0,
        "NO_CIK": 1,
        "UNKNOWN": 0,
        "INCOMPLETE": 0,
        "CIK_CONFLICT": 0,
        "FETCH_FAILED": 0,
    }
    assert result["coverage_scope"] == "supplied_cohort_attempt_coverage_not_all_listed_common"
    assert result["cohort"] == "CONTROLLED_TEST_SUBSET"


def test_partial_batch_cannot_route_prior_ready_as_current(store):
    store.put(record())
    get, calls = fake_provider()
    result = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NIGHT)._nightly(
        ["TEST"], lookup_day=DAY, universe_provenance="Controlled", max_requests=1
    )
    assert calls == [INDEX] and result["status"] == "PARTIAL" and result["pending_symbols"] == 1
    assert store.get("TEST").status == "PENDING" and store.get("TEST").is_us is None


def test_failed_submission_counts_one_request(store):
    get, calls = fake_provider(body=submission(cik="2"))
    result = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NIGHT)._nightly(
        ["TEST"], lookup_day=DAY, universe_provenance="Controlled"
    )
    assert result["requests"] == len(calls) == 2
    assert result["counts"]["UNKNOWN"] == 1


def test_nightly_daytime_refuses_before_any_request(store):
    get, calls = fake_provider()
    with pytest.raises(SecError, match="window_closed"):
        SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NOW)._nightly(
            ["TEST"], lookup_day=DAY, universe_provenance="Controlled"
        )
    assert not calls


def test_nightly_0355_boundary_stops_partial(store):
    ticks = iter(
        [
            NOW.replace(hour=7, minute=54),
            NOW.replace(hour=7, minute=54),
            NOW.replace(hour=7, minute=55),
        ]
    )
    get, calls = fake_provider()
    result = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: next(ticks))._nightly(
        ["TEST"], lookup_day=DAY, universe_provenance="Controlled"
    )
    assert calls == [INDEX] and result["status"] == "PARTIAL"


def test_shared_clock_oversleep_no_catchup_burst_and_failed_attempt(store):
    clock = [1_000_000_000]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += int(seconds * 1e9) + 50_000_000

    with store.sec_job(clock_ns=lambda: clock[0], sleep=sleep) as lease:
        lease.wait()
        clock[0] += 2_000_000_000  # scheduling or request delay
        lease.finished()
        lease.wait()
        second = clock[0]
    with CountryStore(store.session_factory).sec_job(
        clock_ns=lambda: clock[0], sleep=sleep
    ) as lease:
        lease.wait()
        assert clock[0] - second >= REQUEST_INTERVAL_NS
    assert len(sleeps) == 2 and all(value >= 0.13 for value in sleeps)


def test_two_store_jobs_cannot_overlap(store):
    with store.sec_job():
        with pytest.raises(SecError, match="busy"):
            with CountryStore(store.session_factory).sec_job(lock_timeout=0):
                pytest.fail("second process entered exclusive job")
    with store.sec_job():
        pass


@pytest.mark.parametrize("pending,seen", [(0, 1), (1, 0), (1025, 1), (1, 30001)])
def test_lookup_bounds(store, pending, seen):
    with pytest.raises(SecError, match="bounds"):
        CountryRouter(store, max_pending=pending, max_seen=seen)


def test_prefetch_does_not_await_and_close_joins_inflight(store):
    entered, release = threading.Event(), threading.Event()

    class Client:
        def lookup(self, symbol):
            entered.set()
            assert release.wait(3)
            return record(symbol=symbol)

    async def run():
        router = CountryRouter(store, Client(), max_pending=1)
        await router.start()
        assert router.prefetch("TEST")
        assert not router.prefetch("TEST")
        assert not router.prefetch("OTHER")
        assert router.route_for("TEST", NOW).broker == "webull"
        await asyncio.to_thread(entered.wait, 3)
        close = asyncio.create_task(router.close())
        await asyncio.sleep(0)
        assert not close.done()
        assert not router.prefetch("LATE")
        release.set()
        await close
        assert router.route_for("TEST", NOW).broker == "schwab"

    asyncio.run(run())


def test_no_broker_refusal_interface_or_old_routing_import():
    from project_mai_tai import momentum_live_sec

    assert not hasattr(CountryRouter, "record_refusal")
    assert "momentum_live_routing" not in momentum_live_sec.__dict__


def test_corrupt_ready_cache_is_not_trusted(store):
    store.put(record())
    with store.session_factory() as session, session.begin():
        session.get(MomliveSecCountry, "TEST").business_country = "F4"
    with pytest.raises(SecError, match="inconsistent"):
        store.load()


def test_nightly_pending_can_get_single_warning_lookup(store):
    store.put(CountryRecord("TEST", None, None, None, None, NOW, "PENDING", DAY))
    get, calls = fake_provider()
    client = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NOW)
    assert client.lookup("TEST").is_us is True
    assert len(calls) == 2


def test_new_day_refreshes_unknown_without_relabeling_refusal(store):
    get, calls = fake_provider()
    client = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NOW)
    assert client.lookup("MISSING").status == "NO_CIK"
    assert client.lookup("MISSING", lookup_day=DAY + timedelta(days=1)).status == "NO_CIK"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_requests": 30002},
        {"max_symbols": 30001},
        {"max_seconds": 14401},
        {"universe_provenance": ""},
    ],
)
def test_nightly_bounds_refuse_before_fetch(store, kwargs):
    get, calls = fake_provider()
    with pytest.raises(SecError, match="bounds"):
        SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NIGHT)._nightly(
            ["TEST"], lookup_day=DAY, **({"universe_provenance": "Controlled"} | kwargs)
        )
    assert not calls


def test_empty_universe_does_not_claim_full(store):
    get, calls = fake_provider()
    with pytest.raises(SecError, match="empty"):
        SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NIGHT)._nightly(
            [], lookup_day=DAY, universe_provenance="Controlled"
        )
    assert not calls


def test_second_process_uses_same_job_lock(store):
    code = """
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from project_mai_tai.momentum_live_sec import CountryStore, SecError
store = CountryStore(sessionmaker(create_engine(sys.argv[1])))
try:
    with store.sec_job(lock_timeout=0):
        pass
except SecError as error:
    print(str(error))
    raise SystemExit(2)
"""
    with store.sec_job():
        result = subprocess.run(
            [sys.executable, "-c", code, str(store.engine.url)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        )
    assert result.returncode == 2 and result.stdout.strip() == "sec_job_busy"


def test_failed_http_attempt_updates_shared_clock(store):
    clock = [1_000_000_000]
    calls = []

    def sleep(seconds):
        clock[0] += round(seconds * 1e9)

    def fail(url, headers):
        calls.append(clock[0])
        raise RuntimeError("controlled failure")

    client = SecClient(store, user_agent=CONTACT, http_get=fail)
    with store.sec_job(clock_ns=lambda: clock[0], sleep=sleep) as lease:
        for _ in range(2):
            with pytest.raises(SecError, match="fetch_failed"):
                client._get(INDEX, lease)
    assert calls[1] - calls[0] >= REQUEST_INTERVAL_NS


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/secret",
        "https://data.sec.gov/submissions/CIK1.json",
        "https://data.sec.gov/submissions/CIK0000000001.json?contact=secret",
    ],
)
def test_outside_sec_fixed_paths_refused(store, url):
    get, calls = fake_provider()
    client = SecClient(store, user_agent=CONTACT, http_get=get)
    with store.sec_job() as lease:
        with pytest.raises(SecError, match="url_refused"):
            client._get(url, lease)
    assert not calls


def test_new_migration_creates_only_owned_tables(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = Path(__file__).parents[2] / "sql/migrations/versions/20261014_0024_sec_country.py"
    spec = importlib.util.spec_from_file_location("sec_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert set(inspect(connection).get_table_names()) == {
            "momentum_live_sec_country",
            "momentum_live_sec_request_clock",
        }
        assert (
            connection.execute(MomliveSecRequestClock.__table__.select()).one().last_request_ns == 0
        )
        module.downgrade()
        assert inspect(connection).get_table_names() == []
    engine.dispose()


def test_missing_contact_prepare_refuses_without_db(monkeypatch, tmp_path, caplog):
    from project_mai_tai.services import momentum_live_sec_prepare as prepare

    monkeypatch.delenv("MAI_TAI_SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sec",
            "--cohort",
            "SEC_INDEX",
            "--security-type",
            "TYPE_UNKNOWN",
            "--lookup-day",
            "2026-10-15",
        ],
    )
    monkeypatch.setattr(
        prepare, "get_settings", lambda: pytest.fail("read settings without contact")
    )
    assert prepare.main() == 2
    assert "verified_sec_contact_required" in caplog.text


@pytest.mark.parametrize(
    "symbol,cik,business,incorporation,expected",
    [
        ("TDIC", "2041338", "K3", "E9", False),
        ("GLND", "2093507", "TX", "TX", True),
        ("GENK", "1891856", "CA", "DE", True),
        ("TC", "1743340", "F4", "E9", False),
        ("NCPL", "1414767", "MA", "UT", True),
        ("NWGL", "1948294", None, "D8", None),
        ("SCNI", "1611747", "L3", "L3", False),
        ("DLXY", "2025218", "U0", "E9", False),
    ],
)
def test_retained_current_sec_fields_not_morning_availability(
    store, symbol, cik, business, incorporation, expected
):
    # Actual fields captured 2026-10-14 18:44 UTC; controlled decision AFTER capture.
    stamp = NOW.replace(hour=18, minute=44)
    result = classify_submission(
        symbol,
        cik,
        submission(cik=cik, business=business, incorporation=incorporation, tickers=[symbol]),
        as_of=stamp,
        lookup_day=DAY,
    )
    assert result.is_us is expected
    store.put(result)
    router = CountryRouter(store)
    asyncio.run(router.start())
    assert router.route_for(symbol, stamp + timedelta(minutes=1)).broker == (
        "schwab" if expected is True else "webull"
    )
    # Morning classification cannot be retrofitted from an afternoon lookup.
    assert router.route_for(symbol, NOW).is_us is None
    assert router.route_for(symbol, NOW).status == "FUTURE"


def test_reload_async_refreshes_after_overnight_job(store):
    async def run():
        store.put(record())
        router = CountryRouter(store)
        await router.start()
        assert router.route_for("TEST", NOW).broker == "schwab"
        store.put(record("F4", "E9"))
        assert router.route_for("TEST", NOW).broker == "schwab"
        assert await router.reload_async() == 1
        assert router.route_for("TEST", NOW).broker == "webull"

    asyncio.run(run())


def test_reload_does_not_overwrite_lookup_completed_during_read(store, monkeypatch):
    reading, release, committed = threading.Event(), threading.Event(), threading.Event()
    store.put(CountryRecord("TEST", None, None, None, None, NOW, "PENDING", DAY))
    original_load = store.load

    class Client:
        def lookup(self, symbol):
            result = record(symbol=symbol)
            store.put(result)
            committed.set()
            return result

    def delayed_load():
        snapshot = original_load()
        reading.set()
        assert release.wait(3)
        return snapshot

    async def run():
        router = CountryRouter(store, Client())
        await router.start()
        monkeypatch.setattr(store, "load", delayed_load)
        reload = asyncio.create_task(router.reload_async())
        assert await asyncio.to_thread(reading.wait, 3)
        assert router.prefetch("TEST")
        assert router.route_for("TEST", NOW).broker == "webull"
        assert await asyncio.to_thread(committed.wait, 3)
        release.set()
        await reload
        await router._queue.join()
        assert router.route_for("TEST", NOW).broker == "schwab"
        await router.close()

    asyncio.run(run())


def test_0355_crossed_during_rate_wait_refuses_before_http(store, monkeypatch):
    from project_mai_tai.momentum_live_sec import _SecLease

    clock = [NOW.replace(hour=7, minute=54, second=59)]

    def wait(lease):
        clock[0] = NOW.replace(hour=7, minute=55)

    monkeypatch.setattr(_SecLease, "wait", wait)
    get, calls = fake_provider()
    result = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: clock[0])._nightly(
        ["TEST"], lookup_day=DAY, universe_provenance="Controlled"
    )
    assert calls == [] and result["requests"] == 0 and result["status"] == "PARTIAL"


def test_0355_crossed_waiting_second_request_keeps_completed_progress(store, monkeypatch):
    from project_mai_tai.momentum_live_sec import _SecLease

    clock = [NOW.replace(hour=7, minute=54, second=59)]
    waits = [0]

    def wait(lease):
        waits[0] += 1
        if waits[0] == 2:
            clock[0] = NOW.replace(hour=7, minute=55)

    monkeypatch.setattr(_SecLease, "wait", wait)
    get, calls = fake_provider()
    result = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: clock[0])._nightly(
        ["TEST", "MISSING"], lookup_day=DAY, universe_provenance="Controlled"
    )
    assert calls == [INDEX] and result["requests"] == 1
    assert result["completed_symbols"] == 1 and result["pending_symbols"] == 1
    assert store.get("MISSING").status == "NO_CIK"


def test_sec_index_public_population_exactly_index_no_file_or_union(store):
    index = {
        "0": {"ticker": "TEST", "cik_str": 1},
        "1": {"ticker": "ALIAS", "cik_str": 1},
        "2": {"ticker": "FOREIGN", "cik_str": 2},
    }
    calls = []

    def get(url, headers):
        calls.append(url)
        body = (
            index
            if url == INDEX
            else submission(tickers=["TEST", "ALIAS"])
            if "0000000001" in url
            else submission(cik="2", tickers=["FOREIGN"], business="F4", incorporation="E9")
        )
        return json.dumps(body).encode()

    result = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NIGHT).nightly(
        lookup_day=DAY, cohort="SEC_INDEX", security_type="TYPE_UNKNOWN"
    )
    assert result["cohort"] == "SEC_INDEX" and result["security_type"] == "TYPE_UNKNOWN"
    assert result["all_listed_common_completeness"] == "UNKNOWN"
    assert (
        result["sec_index_count"]
        == result["requested_symbols"]
        == result["attempted_symbols"]
        == result["completed_symbols"]
        == 3
    )
    assert result["unique_ciks"] == result["attempted_ciks"] == result["completed_ciks"] == 2
    assert calls.count(INDEX) == 1 and len(calls) == 3
    assert result["counts"]["US"] == 2 and result["counts"]["NON_US"] == 1
    assert result["http_errors"] == {"403": 0, "429": 0} and result["runtime_seconds"] >= 0
    assert result["status"] == "FULL"


@pytest.mark.parametrize(
    "cohort,security_type",
    [
        ("SEC_INDEX", "COMMON"),
        ("COMMON", "TYPE_UNKNOWN"),
        ("GATEWAY_UNION", "TYPE_UNKNOWN"),
        ("SEC_INDEX", "UNKNOWN"),
    ],
)
def test_fake_common_or_undeclared_cohort_refused_before_io(store, cohort, security_type):
    get, calls = fake_provider()
    with pytest.raises(SecError, match="sec_index_type_unknown_required"):
        SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NIGHT).nightly(
            lookup_day=DAY, cohort=cohort, security_type=security_type
        )
    assert not calls


def test_sec_index_incomplete_conflict_and_raw_unique_cik_denominators(store):
    index = {
        "0": {"ticker": "TEST", "cik_str": 1},
        "1": {"ticker": "TEST", "cik_str": 2},
        "2": {"ticker": "OTHER", "cik_str": 3},
    }
    get, calls = fake_provider(index, submission(cik="3", tickers=["OTHER"], business=None))
    result = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NIGHT).nightly(
        lookup_day=DAY, cohort="SEC_INDEX", security_type="TYPE_UNKNOWN"
    )
    assert result["sec_index_count"] == result["unique_ciks"] == 3
    assert (
        result["requested_symbols"]
        == result["attempted_symbols"]
        == result["completed_symbols"]
        == 2
    )
    assert result["attempted_ciks"] == result["completed_ciks"] == 1
    assert result["counts"]["INCOMPLETE"] == result["counts"]["CIK_CONFLICT"] == 1
    assert result["counts"]["NO_CIK"] == 0 and len(calls) == 2


@pytest.mark.parametrize("code", [403, 429])
@pytest.mark.parametrize("fail_index", [True, False])
def test_http_access_refusal_reported_and_stops_without_retry(store, code, fail_index):
    calls = []
    index = {"0": {"ticker": "TEST", "cik_str": 1}, "1": {"ticker": "OTHER", "cik_str": 2}}

    def get(url, headers):
        calls.append(url)
        if fail_index or url != INDEX:
            raise HTTPError(url, code, CONTACT, None, None)
        return json.dumps(index).encode()

    result = SecClient(store, user_agent=CONTACT, http_get=get, clock=lambda: NIGHT).nightly(
        lookup_day=DAY, cohort="SEC_INDEX", security_type="TYPE_UNKNOWN"
    )
    assert len(calls) == result["requests"] == (1 if fail_index else 2)
    assert result["http_errors"][str(code)] == 1 and sum(result["http_errors"].values()) == 1
    assert result["stop_reason"] == f"sec_http_{code}" and result["status"] == "PARTIAL"
    assert result["completed_ciks"] == 0 and CONTACT not in json.dumps(result)
    if fail_index:
        assert result["sec_index_count"] is None and result["requested_symbols"] is None
    else:
        assert result["counts"]["FETCH_FAILED"] == 1 and result["pending_symbols"] == 1


def test_sec_index_cli_accepted_controlled_no_db_or_provider(monkeypatch, capsys):
    from project_mai_tai.services import momentum_live_sec_prepare as prepare

    monkeypatch.setenv("MAI_TAI_SEC_USER_AGENT", CONTACT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sec",
            "--cohort",
            "SEC_INDEX",
            "--security-type",
            "TYPE_UNKNOWN",
            "--lookup-day",
            "2026-10-15",
        ],
    )
    monkeypatch.setattr(prepare, "get_settings", lambda: object())
    monkeypatch.setattr(prepare, "build_timed_session_factory", lambda *args, **kwargs: object())
    monkeypatch.setattr(prepare, "CountryStore", lambda factory: object())

    class Client:
        def __init__(self, store, *, user_agent):
            assert user_agent == CONTACT

        def nightly(self, **kwargs):
            assert kwargs["cohort"] == "SEC_INDEX" and kwargs["security_type"] == "TYPE_UNKNOWN"
            return {
                "status": "FULL",
                "cohort": kwargs["cohort"],
                "http_errors": {"403": 0, "429": 0},
                "counts": {"FETCH_FAILED": 0},
            }

    monkeypatch.setattr(prepare, "SecClient", Client)
    assert prepare.main() == 0
    assert json.loads(capsys.readouterr().out)["cohort"] == "SEC_INDEX"


def test_cli_fake_common_refused_before_settings_or_db(monkeypatch, caplog):
    from project_mai_tai.services import momentum_live_sec_prepare as prepare

    monkeypatch.setattr(
        sys,
        "argv",
        ["sec", "--cohort", "SEC_INDEX", "--security-type", "COMMON", "--lookup-day", "2026-10-15"],
    )
    monkeypatch.setattr(
        prepare, "get_settings", lambda: pytest.fail("opened settings for fake COMMON")
    )
    assert prepare.main() == 2
    assert "sec_index_type_unknown_required" in caplog.text
