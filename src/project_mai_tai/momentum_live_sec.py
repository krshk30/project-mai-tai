"""SEC-field country policy; network/DB work never runs in route_for or prefetch.

All production SEC jobs must use the same PostgreSQL database coordinator.
One advisory lock serializes jobs across hosts; one clock paces EVERY request.
SQLite/file coordination is for isolated functional tests, not multi-host service.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as wall_time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from sqlalchemy import select, text, update

from project_mai_tai.db.momlive_sec_country import MomliveSecCountry, MomliveSecRequestClock
from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS

logger = logging.getLogger(__name__)
US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)
BUSINESS_US_CODES = US_STATES | {"DC", "PR"}
REQUEST_INTERVAL_NS = 130_000_000
SEC_JOB_LOCK = 1888320142
_ET = ZoneInfo("America/New_York")


class SecError(ValueError):
    """Fixed error codes only; contact headers and responses are never logged."""


def _symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}", symbol):
        raise SecError("invalid_symbol")
    return symbol


def _cik(value: object) -> str:
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,10}", str(value)) or int(value) == 0:
        raise SecError("invalid_cik")
    return str(int(value)).zfill(10)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecError("aware_time_required")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class CountryRecord:
    symbol: str
    cik: str | None
    business_country: str | None
    incorporation: str | None
    is_us: bool | None
    as_of: datetime
    status: str
    lookup_day: date
    source_sha256: str | None = None


@dataclass(frozen=True)
class CountryRoute:
    broker: str | None
    reason: str
    status: str
    is_us: bool | None
    cik: str | None
    as_of: datetime | None


def parse_ticker_map(body: Mapping) -> tuple[dict[str, str], frozenset[str]]:
    if not isinstance(body, Mapping) or len(body) > 40_000:
        raise SecError("invalid_ticker_map")
    mapping, conflicts = {}, set()
    for row in body.values():
        if not isinstance(row, Mapping) or "ticker" not in row or "cik_str" not in row:
            raise SecError("invalid_ticker_row")
        symbol, cik = _symbol(row["ticker"]), _cik(row["cik_str"])
        if symbol in mapping and mapping[symbol] != cik:
            conflicts.add(symbol)
        mapping[symbol] = cik
    for symbol in conflicts:
        mapping.pop(symbol, None)
    return mapping, frozenset(conflicts)


def classify_submission(
    symbol: str,
    cik: str,
    body: Mapping,
    *,
    as_of: datetime,
    lookup_day: date,
    source_sha256: str | None = None,
) -> CountryRecord:
    symbol, cik, as_of = _symbol(symbol), _cik(cik), _aware(as_of)
    if not isinstance(body, Mapping):
        raise SecError("invalid_submission")
    if _cik(body.get("cik")) != cik:
        raise SecError("submission_cik_mismatch")
    tickers = body.get("tickers")
    if not isinstance(tickers, list) or symbol not in tickers:
        raise SecError("submission_ticker_mismatch")
    addresses = body.get("addresses")
    business_address = addresses.get("business") if isinstance(addresses, Mapping) else None
    business = (
        business_address.get("stateOrCountry") if isinstance(business_address, Mapping) else None
    )
    incorporation = body.get("stateOfIncorporation")

    def normalize(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{2,16}", value.strip()):
            return None
        return value.strip().upper()

    business, incorporation = normalize(business), normalize(incorporation)
    complete = business is not None and incorporation is not None
    return CountryRecord(
        symbol,
        cik,
        business,
        incorporation,
        business in BUSINESS_US_CODES and incorporation in US_STATES if complete else None,
        as_of,
        "READY" if complete else "INCOMPLETE",
        lookup_day,
        source_sha256,
    )


class CountryStore:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        with session_factory() as session:
            self.engine = session.get_bind()

    def load(self) -> dict[str, CountryRecord]:
        with self.session_factory() as session:
            rows = session.scalars(select(MomliveSecCountry).limit(30_001)).all()
            if len(rows) > 30_000:
                raise SecError("country_cache_bound")
            return {row.symbol: self._record(row) for row in rows}

    @staticmethod
    def _record(row) -> CountryRecord:
        stamp = row.as_of if row.as_of.tzinfo else row.as_of.replace(tzinfo=UTC)
        record = CountryRecord(
            row.symbol,
            row.cik,
            row.business_country,
            row.incorporation,
            row.is_us,
            stamp,
            row.status,
            row.lookup_day,
            row.source_sha256,
        )
        CountryStore._validate(record)
        return record

    def get(self, symbol: str) -> CountryRecord | None:
        with self.session_factory() as session:
            row = session.get(MomliveSecCountry, _symbol(symbol))
            return self._record(row) if row else None

    @staticmethod
    def _validate(record: CountryRecord) -> None:
        _symbol(record.symbol)
        _aware(record.as_of)
        if record.status not in {
            "READY",
            "INCOMPLETE",
            "NO_CIK",
            "CIK_CONFLICT",
            "FETCH_FAILED",
            "PENDING",
        }:
            raise SecError("invalid_country_status")
        if record.cik is not None and _cik(record.cik) != record.cik:
            raise SecError("noncanonical_cik")
        if record.status == "READY":
            expected = (
                record.business_country in BUSINESS_US_CODES and record.incorporation in US_STATES
            )
            if (
                not record.business_country
                or not record.incorporation
                or record.is_us is not expected
                or record.cik is None
            ):
                raise SecError("inconsistent_ready_record")
        elif record.is_us is not None:
            raise SecError("unknown_record_has_classification")

    def put(self, record: CountryRecord) -> None:
        self._validate(record)
        with self.session_factory() as session, session.begin():
            row = session.get(MomliveSecCountry, record.symbol)
            if row is None:
                row = MomliveSecCountry(symbol=record.symbol)
                session.add(row)
            for field in CountryRecord.__dataclass_fields__:
                setattr(row, field, getattr(record, field))

    def invalidate_for_prepare(self, lookup_day: date) -> None:
        # One DB statement: interrupted preparation cannot advertise yesterday's
        # records as refreshed. New, unprocessed symbols are absent/UNKNOWN.
        with self.session_factory() as session, session.begin():
            session.execute(
                update(MomliveSecCountry).values(
                    status="PENDING", is_us=None, lookup_day=lookup_day
                )
            )

    @contextmanager
    def sec_job(
        self,
        *,
        lock_timeout: float = 5,
        clock_ns: Callable[[], int] = time.time_ns,
        sleep: Callable[[float], None] = time.sleep,
    ):
        """One coordinator for all jobs/accounts/processes, never one limiter per client."""
        connection = self.engine.connect()
        local_lock = None
        locked = False
        deadline = time.monotonic() + lock_timeout
        try:
            if connection.dialect.name == "postgresql":
                while not locked:
                    locked = bool(
                        connection.execute(
                            text("SELECT pg_try_advisory_lock(:key)"), {"key": SEC_JOB_LOCK}
                        ).scalar_one()
                    )
                    connection.commit()
                    if not locked:
                        if time.monotonic() >= deadline:
                            raise SecError("sec_job_busy")
                        sleep(0.05)
            elif connection.dialect.name == "sqlite":
                database = self.engine.url.database
                if not database or database == ":memory:":
                    raise SecError("sec_test_coordinator_requires_file")
                local_lock = open(str(database) + ".sec-job.lock", "a")
                while not locked:
                    try:
                        fcntl.flock(local_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise SecError("sec_job_busy")
                        sleep(0.05)
            else:
                raise SecError("unsupported_sec_coordinator")
            yield _SecLease(connection, clock_ns, sleep)
        finally:
            try:
                if locked and connection.dialect.name == "postgresql":
                    connection.rollback()
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": SEC_JOB_LOCK}
                    )
                    connection.commit()
            finally:
                if local_lock is not None:
                    local_lock.close()
                connection.close()


class _SecLease:
    def __init__(self, connection, clock_ns, sleep):
        self.connection, self.clock_ns, self.sleep = connection, clock_ns, sleep

    def finished(self) -> None:
        connection = self.connection
        now = (
            int(
                connection.execute(
                    text("SELECT extract(epoch FROM clock_timestamp()) * 1000000000")
                ).scalar_one()
            )
            if connection.dialect.name == "postgresql"
            else self.clock_ns()
        )
        connection.execute(
            update(MomliveSecRequestClock)
            .where(MomliveSecRequestClock.id == 1)
            .values(last_request_ns=now)
        )
        connection.commit()

    def wait(self) -> None:
        connection = self.connection

        def now_ns():
            return (
                int(
                    connection.execute(
                        text("SELECT extract(epoch FROM clock_timestamp()) * 1000000000")
                    ).scalar_one()
                )
                if connection.dialect.name == "postgresql"
                else self.clock_ns()
            )

        now = now_ns()
        last = connection.execute(
            select(MomliveSecRequestClock.last_request_ns)
            .where(MomliveSecRequestClock.id == 1)
            .with_for_update()
        ).scalar_one_or_none()
        if last is None:
            raise SecError("sec_request_clock_missing")
        target = max(now, last + REQUEST_INTERVAL_NS)
        connection.commit()
        while target > now:
            self.sleep((target - now) / 1_000_000_000)
            now = now_ns()
        # Record actual wake time, not the target: a delayed wake must not permit a burst.
        connection.execute(
            update(MomliveSecRequestClock)
            .where(MomliveSecRequestClock.id == 1)
            .values(last_request_ns=now)
        )
        connection.commit()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SecError("sec_redirect_refused")


class SecClient:
    def __init__(
        self,
        store: CountryStore,
        *,
        user_agent: str,
        http_get=None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if (
            not isinstance(user_agent, str)
            or len(user_agent) > 256
            or "\n" in user_agent
            or "\r" in user_agent
            or not re.fullmatch(r"[^<>]+\s+[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", user_agent)
        ):
            raise SecError("verified_sec_contact_required")
        self.store, self._user_agent, self._http_get, self.clock = (
            store,
            user_agent,
            http_get,
            clock,
        )

    def _get(self, url: str, lease: _SecLease, *, before_request=None) -> tuple[Mapping, str]:
        if url != "https://www.sec.gov/files/company_tickers.json" and not re.fullmatch(
            r"https://data\.sec\.gov/submissions/CIK\d{10}\.json", url
        ):
            raise SecError("sec_url_refused")
        lease.wait()
        if before_request is not None:
            before_request()
        try:
            headers = {
                "User-Agent": self._user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }
            if self._http_get is not None:
                raw = self._http_get(url, headers)
            else:
                request = Request(url, headers=headers)
                with build_opener(_NoRedirect()).open(request, timeout=5) as response:
                    raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise SecError("sec_response_bound")
            body = json.loads(raw)
            if not isinstance(body, Mapping):
                raise SecError("sec_invalid_json_shape")
            return body, hashlib.sha256(raw).hexdigest()
        except SecError:
            raise
        except HTTPError as error:
            raise SecError(f"sec_http_{error.code}") from None
        except Exception:
            raise SecError("sec_fetch_failed") from None
        finally:
            # Pacing from completion also covers suspension between reservation and send.
            lease.finished()

    def _unknown(self, symbol, day, status, cik=None, sha=None):
        return CountryRecord(
            _symbol(symbol), cik, None, None, None, _aware(self.clock()), status, day, sha
        )

    def lookup(self, symbol: str, *, lookup_day: date | None = None) -> CountryRecord:
        symbol = _symbol(symbol)
        day = lookup_day or _aware(self.clock()).astimezone(_ET).date()
        with self.store.sec_job() as lease:
            existing = self.store.get(symbol)
            if existing is not None and existing.lookup_day == day and existing.status != "PENDING":
                return existing
            try:
                index, sha = self._get("https://www.sec.gov/files/company_tickers.json", lease)
                mapping, conflicts = parse_ticker_map(index)
                cik = mapping.get(symbol)
                if symbol in conflicts:
                    record = self._unknown(symbol, day, "CIK_CONFLICT", sha=sha)
                elif cik is None:
                    record = self._unknown(symbol, day, "NO_CIK", sha=sha)
                else:
                    body, body_sha = self._get(
                        f"https://data.sec.gov/submissions/CIK{cik}.json", lease
                    )
                    record = classify_submission(
                        symbol,
                        cik,
                        body,
                        as_of=self.clock(),
                        lookup_day=day,
                        source_sha256=body_sha,
                    )
            except Exception:
                record = self._unknown(symbol, day, "FETCH_FAILED")
            self.store.put(record)
            return record

    def nightly(
        self,
        symbols: Iterable[str],
        *,
        lookup_day: date,
        universe_provenance: str,
        max_symbols: int = 30_000,
        max_requests: int = 20_000,
        max_seconds: float = 3600,
    ) -> dict:
        now = _aware(self.clock()).astimezone(_ET)
        if wall_time(3, 55) <= now.time() < wall_time(16):
            raise SecError("nightly_window_closed")
        if (
            not isinstance(universe_provenance, str)
            or not universe_provenance.strip()
            or len(universe_provenance) > 512
            or not 1 <= max_symbols <= 30_000
            or not 1 <= max_requests <= 30_001
            or not 0 < max_seconds <= 14_400
        ):
            raise SecError("nightly_bounds_or_provenance_missing")
        universe = set()
        for symbol in symbols:
            universe.add(_symbol(symbol))
            if len(universe) > max_symbols:
                raise SecError("nightly_symbol_bound")
        if not universe:
            raise SecError("empty_common_universe")
        started, requests, completed = time.monotonic(), 0, 0
        counts = {"US": 0, "NON_US": 0, "NO_CIK": 0, "UNKNOWN": 0}
        index_sha = None

        def request_guard():
            nonlocal requests
            local_now = _aware(self.clock()).astimezone(_ET)
            if wall_time(3, 55) <= local_now.time() < wall_time(16):
                raise SecError("nightly_window_closed")
            if requests >= max_requests or time.monotonic() - started >= max_seconds:
                raise SecError("nightly_bound_reached")
            requests += 1

        def report():
            return {
                "status": "FULL" if completed == len(universe) else "PARTIAL",
                "requested_symbols": len(universe),
                "completed_symbols": completed,
                "pending_symbols": len(universe) - completed,
                "requests": requests,
                "counts": counts,
                "universe_provenance": universe_provenance,
                "sec_index_sha256": index_sha,
                "coverage_scope": "caller_supplied_common_universe_not_sec_index",
            }

        with self.store.sec_job() as lease:
            self.store.invalidate_for_prepare(lookup_day)
            try:
                index, index_sha = self._get(
                    "https://www.sec.gov/files/company_tickers.json",
                    lease,
                    before_request=request_guard,
                )
            except SecError as error:
                if str(error) in {"nightly_window_closed", "nightly_bound_reached"}:
                    return report()
                raise
            mapping, conflicts = parse_ticker_map(index)
            groups = {}
            for symbol in sorted(universe):
                cik = mapping.get(symbol)
                if cik is None or symbol in conflicts:
                    status = "CIK_CONFLICT" if symbol in conflicts else "NO_CIK"
                    record = self._unknown(symbol, lookup_day, status, sha=index_sha)
                    self.store.put(record)
                    counts["NO_CIK" if status == "NO_CIK" else "UNKNOWN"] += 1
                    completed += 1
                else:
                    groups.setdefault(cik, []).append(symbol)
            for cik, names in groups.items():
                local_now = _aware(self.clock()).astimezone(_ET)
                if (
                    requests >= max_requests
                    or time.monotonic() - started >= max_seconds
                    or wall_time(3, 55) <= local_now.time() < wall_time(16)
                ):
                    break
                try:
                    body, sha = self._get(
                        f"https://data.sec.gov/submissions/CIK{cik}.json",
                        lease,
                        before_request=request_guard,
                    )
                    records = [
                        classify_submission(
                            symbol,
                            cik,
                            body,
                            as_of=self.clock(),
                            lookup_day=lookup_day,
                            source_sha256=sha,
                        )
                        for symbol in names
                    ]
                except SecError as error:
                    if str(error) in {"nightly_window_closed", "nightly_bound_reached"}:
                        break
                    records = [
                        self._unknown(symbol, lookup_day, "FETCH_FAILED", cik=cik)
                        for symbol in names
                    ]
                except Exception:
                    records = [
                        self._unknown(symbol, lookup_day, "FETCH_FAILED", cik=cik)
                        for symbol in names
                    ]
                for record in records:
                    self.store.put(record)
                    counts[
                        "US"
                        if record.is_us is True
                        else "NON_US"
                        if record.is_us is False
                        else "UNKNOWN"
                    ] += 1
                    completed += 1
        return report()


class CountryRouter:
    def __init__(
        self,
        store: CountryStore,
        client: SecClient | None = None,
        *,
        max_pending: int = 64,
        max_seen: int = 30_000,
    ):
        if not 1 <= max_pending <= 1024 or not 1 <= max_seen <= 30_000:
            raise SecError("invalid_lookup_bounds")
        self.store, self.client = store, client
        self._records: dict[str, CountryRecord] = {}
        self._queue = asyncio.Queue(maxsize=max_pending)
        self._seen: set[str] = set()
        self._max_seen, self._worker = max_seen, None
        self._unknown_logged: set[str] = set()
        self._closing = False
        self._reload_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._worker is not None:
            return
        self._closing = False
        await self.reload_async()
        if self.client is not None and self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def reload_async(self) -> int:
        """Caller schedules at 03:55; atomically reload without blocking detection.

        Lookup commits may occur during the DB read, but their memory updates wait
        for this swap, so a late snapshot cannot overwrite a newer lookup result.
        """
        async with self._reload_lock:
            records = await asyncio.to_thread(self.store.load)
            self._records = records
            self._seen.clear()
            self._unknown_logged.clear()
            return len(records)

    def route_for(self, symbol: str, now: datetime) -> CountryRoute:
        symbol, now = _symbol(symbol), _aware(now)
        local = now.astimezone(_ET)
        record = self._records.get(symbol)
        known = record is not None and record.status == "READY" and record.as_of <= now
        value = record.is_us if known else None
        status = (
            "FUTURE"
            if record is not None and record.as_of > now
            else record.status
            if record is not None
            else "UNKNOWN"
        )
        if (
            local.weekday() >= 5
            or local.date() in US_MARKET_HOLIDAYS
            or not wall_time(4) <= local.time() < wall_time(16)
        ):
            broker, reason = None, "entry_window_closed"
        elif local.time() < wall_time(7):
            broker, reason = "webull", "all_before_07"
        elif value is True:
            broker, reason = "schwab", "sec_us"
        elif value is False:
            broker, reason = "webull", "sec_non_us"
        else:
            broker, reason = "webull", "sec_unknown"
            if symbol not in self._unknown_logged and len(self._unknown_logged) < self._max_seen:
                self._unknown_logged.add(symbol)
                logger.info(
                    "[MOMLIVE-SEC-UNKNOWN] symbol=%s status=%s route=webull", symbol, status
                )
        return CountryRoute(
            broker,
            reason,
            status,
            value,
            record.cik if record else None,
            record.as_of if record else None,
        )

    def prefetch(self, symbol: str) -> bool:
        symbol = _symbol(symbol)
        record = self._records.get(symbol)
        if (
            self._closing
            or self.client is None
            or self._worker is None
            or symbol in self._seen
            or len(self._seen) >= self._max_seen
            or (record is not None and record.status == "READY")
        ):
            return False
        try:
            self._queue.put_nowait(symbol)
        except asyncio.QueueFull:
            return False
        self._seen.add(symbol)
        return True

    async def _run(self) -> None:
        while True:
            symbol = await self._queue.get()
            try:
                if symbol is None:
                    return
                record = await asyncio.to_thread(self.client.lookup, symbol)
                async with self._reload_lock:
                    self._records[symbol] = record
            except Exception:
                logger.warning("[MOMLIVE-SEC-LOOKUP] symbol=%s status=unavailable", symbol)
            finally:
                self._queue.task_done()
            if self._closing:
                return

    async def close(self) -> None:
        if self._worker is not None:
            self._closing = True
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
            self._queue.put_nowait(None)
            # Do not cancel to_thread: that would leave an unjoined SEC/DB job running.
            await self._worker
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
            self._worker = None
