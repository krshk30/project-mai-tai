"""MOMLIVE1 routing policy, isolated persistence and off-loop type preparation.

No provider client is constructed here. Callers supply the existing keyed client
at prepare time; route() uses an immutable snapshot and never performs I/O.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import MappingProxyType
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from project_mai_tai.broker_eligibility import (
    SCHWAB_OPENING_INELIGIBLE_REASON_SUBSTRINGS,
    is_schwab_opening_ineligible_reason,
)
from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS

FOREIGN_POLICY_TYPES = frozenset({"ADRC", "ADRP", "ADRR", "OS"})
REPORT_CATEGORIES = frozenset(
    {
        "schwab",
        "webull_type",
        "webull_ineligible",
        "schwab_refused_then_webull",
        "webull_before_07",
        "refused_hours",
    }
)
_ET = ZoneInfo("America/New_York")


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}", value):
        raise ValueError("invalid_routing_symbol")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("routing_time_requires_timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RouteDecision:
    broker: str | None
    category: str
    type_code: str | None
    type_source: str
    issuer_country: str = "UNKNOWN"


@dataclass(frozen=True)
class RoutingSnapshot:
    session_date: date
    types: Mapping[str, str | None]
    conflicts: frozenset[str]
    ineligible_first_seen: Mapping[str, datetime]
    type_status: str
    ineligible_provenance: Mapping[str, Mapping[str, str | None]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "types", MappingProxyType(dict(self.types)))
        object.__setattr__(
            self, "ineligible_first_seen", MappingProxyType(dict(self.ineligible_first_seen))
        )
        object.__setattr__(
            self,
            "ineligible_provenance",
            MappingProxyType(
                {s: MappingProxyType(dict(v)) for s, v in self.ineligible_provenance.items()}
            ),
        )

    def route(self, symbol: str, now: datetime) -> RouteDecision:
        symbol = _symbol(symbol)
        now = _utc(now)
        local = now.astimezone(_ET)
        same_day = local.date() == self.session_date
        code = self.types.get(symbol) if same_day else None
        source = "cached_type" if code is not None else "UNKNOWN"
        if same_day and symbol in self.conflicts:
            code, source = None, "CONFLICT"
        if (
            local.weekday() >= 5
            or local.date() in US_MARKET_HOLIDAYS
            or not time(4) <= local.time() < time(16)
        ):
            return RouteDecision(None, "refused_hours", code, source)
        if local.time() < time(7):
            return RouteDecision("webull", "webull_before_07", code, source)
        first_seen = self.ineligible_first_seen.get(symbol)
        if first_seen is not None and first_seen <= now:
            return RouteDecision("webull", "webull_ineligible", code, source)
        if code in FOREIGN_POLICY_TYPES:
            return RouteDecision("webull", "webull_type", code, source)
        return RouteDecision("schwab", "schwab", code, source)


class RoutingStore:
    """Dedicated MOMLIVE1 SQLite file; all methods are off-hot-path operations."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables:
                if "routing_owner" not in tables:
                    raise ValueError("wrong_routing_store_owner")
                owners = [row[0] for row in db.execute("SELECT owner FROM routing_owner")]
                if owners != ["momentum_warn_live_v1"]:
                    raise ValueError("wrong_routing_store_owner")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS routing_owner (owner TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS ineligible (
                    symbol TEXT PRIMARY KEY, first_seen TEXT NOT NULL,
                    source_account_id TEXT, reason TEXT NOT NULL, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preparations (
                    day TEXT PRIMARY KEY, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS types (
                    day TEXT NOT NULL, symbol TEXT NOT NULL, code TEXT,
                    conflict INTEGER NOT NULL, PRIMARY KEY(day,symbol));
                CREATE TABLE IF NOT EXISTS routing_report (
                    day TEXT NOT NULL, episode TEXT NOT NULL, category TEXT NOT NULL,
                    PRIMARY KEY(day,episode));
            """)
            owners = [row[0] for row in db.execute("SELECT owner FROM routing_owner")]
            if owners and owners != ["momentum_warn_live_v1"]:
                raise ValueError("wrong_routing_store_owner")
            db.execute("INSERT OR IGNORE INTO routing_owner VALUES ('momentum_warn_live_v1')")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def record_ineligible(
        self,
        symbol: str,
        first_seen_at: datetime,
        reason: str,
        *,
        source_account_id: str | None = None,
        source: str = "momentum_warn_live",
    ) -> bool:
        symbol = _symbol(symbol)
        stamp = _utc(first_seen_at).isoformat()
        if not is_schwab_opening_ineligible_reason(reason):
            return False
        if source not in {"momentum_warn_live", "schwab_ineligible_today"}:
            raise ValueError("invalid_ineligible_source")
        if source_account_id is not None and not re.fullmatch(
            r"[A-Za-z0-9:_-]{1,64}", source_account_id
        ):
            raise ValueError("invalid_ineligible_account")
        normalized_reason = next(
            fragment
            for fragment in SCHWAB_OPENING_INELIGIBLE_REASON_SUBSTRINGS
            if fragment in reason.lower()
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT first_seen FROM ineligible WHERE symbol=?", (symbol,)
            ).fetchone()
            if prior and prior[0] <= stamp:
                return False
            db.execute(
                "INSERT INTO ineligible VALUES (?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET first_seen=excluded.first_seen,source_account_id=excluded.source_account_id,reason=excluded.reason,source=excluded.source",
                (symbol, stamp, source_account_id, normalized_reason, source),
            )
        return True

    def seed_existing_ineligible(
        self, rows: list[Mapping[str, object]], *, source_account_id: str
    ) -> int:
        """Read-side import only; caller supplies existing cache rows for its ATR account."""
        if len(rows) > 10_000:
            raise ValueError("ineligible_seed_bound")
        changed = 0
        for row in rows:
            if str(row.get("broker_account_id")) != source_account_id:
                continue
            stamp = row.get("first_seen_at")
            if isinstance(stamp, str):
                stamp = datetime.fromisoformat(stamp)
            changed += self.record_ineligible(
                row.get("symbol"),
                stamp,
                str(row.get("reason_text") or ""),
                source_account_id=source_account_id,
                source="schwab_ineligible_today",
            )
        return changed

    def snapshot(self, session_date: date) -> RoutingSnapshot:
        with self._connect() as db:
            db.execute("BEGIN")
            types = list(
                db.execute(
                    "SELECT symbol,code,conflict FROM types WHERE day=?",
                    (session_date.isoformat(),),
                )
            )
            ineligible_rows = list(
                db.execute(
                    "SELECT symbol,first_seen,source_account_id,reason,source FROM ineligible"
                )
            )
            ineligible = {row[0]: row[1] for row in ineligible_rows}
            status = db.execute(
                "SELECT status FROM preparations WHERE day=?", (session_date.isoformat(),)
            ).fetchone()
        return RoutingSnapshot(
            session_date,
            MappingProxyType({s: c for s, c, conflict in types if not conflict}),
            frozenset(s for s, _, conflict in types if conflict),
            MappingProxyType({s: datetime.fromisoformat(t) for s, t in ineligible.items()}),
            status[0] if status else "MISSING",
            {
                s: {"source_account_id": account, "reason": reason, "source": source}
                for s, _, account, reason, source in ineligible_rows
            },
        )

    def _claim_preparation(self, day: date) -> bool:
        with self._connect() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO preparations VALUES (?, 'PENDING')", (day.isoformat(),)
            )
            return result.rowcount == 1

    def _save_types(self, day: date, types: dict[str, str | None], conflicts: set[str]) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT INTO types VALUES (?,?,?,?)",
                [(day.isoformat(), s, c, int(s in conflicts)) for s, c in types.items()],
            )
            db.execute("UPDATE preparations SET status='READY' WHERE day=?", (day.isoformat(),))

    def _fail_preparation(self, day: date) -> None:
        with self._connect() as db:
            db.execute("UPDATE preparations SET status='FAILED' WHERE day=?", (day.isoformat(),))

    def record_route(
        self, episode_id: str, session_date: date, category: str, *, max_episodes: int = 100_000
    ) -> bool:
        if not episode_id or len(episode_id) > 255 or category not in REPORT_CATEGORIES:
            raise ValueError("invalid_route_report")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key = (session_date.isoformat(), episode_id)
            prior = db.execute(
                "SELECT category FROM routing_report WHERE day=? AND episode=?", key
            ).fetchone()
            if prior:
                if prior[0] == category:
                    return False
                if (prior[0], category) != ("schwab", "schwab_refused_then_webull"):
                    raise ValueError("conflicting_route_report")
                db.execute(
                    "UPDATE routing_report SET category=? WHERE day=? AND episode=?",
                    (category, *key),
                )
            else:
                count = db.execute(
                    "SELECT count(*) FROM routing_report WHERE day=?", (key[0],)
                ).fetchone()[0]
                if count >= max_episodes:
                    raise ValueError("routing_report_capacity")
                db.execute("INSERT INTO routing_report VALUES (?,?,?)", (*key, category))
        return True

    def daily_report(self, session_date: date) -> dict[str, int]:
        with self._connect() as db:
            counts = dict(
                db.execute(
                    "SELECT category,count(*) FROM routing_report WHERE day=? GROUP BY category",
                    (session_date.isoformat(),),
                )
            )
        return {category: counts.get(category, 0) for category in sorted(REPORT_CATEGORIES)}


@dataclass(frozen=True)
class TickerPage:
    rows: tuple[Mapping[str, object], ...]
    next_cursor: str | None = None


class TypeBootstrapError(ValueError):
    """Fixed error codes only: provider exception text may contain credentials."""


class MassiveTickerPages:
    """Future prepare adapter around an injected existing SDK client, never constructed here."""

    def __init__(self, client: object, session_date: date):
        self.client = client
        self.session_date = session_date

    def __call__(self, cursor: str | None) -> TickerPage:
        try:
            response = self.client.list_tickers(
                market="stocks",
                active=True,
                date=self.session_date.isoformat(),
                limit=1000,
                sort="ticker",
                order="asc",
                raw=True,
                params={"cursor": cursor} if cursor else None,
            )
            if isinstance(response, Mapping):
                body = response
            else:
                if len(response.data) > 8 * 1024 * 1024:
                    raise TypeBootstrapError("type_page_bytes_bound")
                body = json.loads(response.data)
            if body.get("status") != "OK" or not isinstance(body.get("results"), list):
                raise TypeBootstrapError("invalid_type_page")
            next_cursor = None
            if body.get("next_url"):
                url = urlparse(body["next_url"])
                if (
                    url.scheme != "https"
                    or url.hostname not in {"api.massive.com", "api.polygon.io"}
                    or url.path != "/v3/reference/tickers"
                ):
                    raise TypeBootstrapError("invalid_type_cursor_origin")
                values = parse_qs(url.query).get("cursor", [])
                if len(values) != 1 or not values[0] or len(values[0]) > 4096:
                    raise TypeBootstrapError("invalid_type_cursor")
                next_cursor = values[0]
            return TickerPage(tuple(body["results"]), next_cursor)
        except TypeBootstrapError:
            raise
        except Exception:
            raise TypeBootstrapError("type_page_unavailable") from None


def _prepare_types(
    store: RoutingStore,
    fetch_page: Callable[[str | None], TickerPage],
    session_date: date,
    max_pages: int,
    max_rows: int,
) -> RoutingSnapshot:
    if not 1 <= max_pages <= 100 or not 1 <= max_rows <= 100_000:
        raise ValueError("invalid_type_bootstrap_bounds")
    if not store._claim_preparation(session_date):
        return store.snapshot(session_date)
    cursor = None
    cursors: set[str] = set()
    types: dict[str, str | None] = {}
    conflicts: set[str] = set()
    row_count = 0
    try:
        for _ in range(max_pages):
            try:
                page = fetch_page(cursor)
            except Exception:
                raise TypeBootstrapError("type_page_unavailable") from None
            if len(page.rows) > 1000:
                raise TypeBootstrapError("type_page_row_bound")
            row_count += len(page.rows)
            if row_count > max_rows:
                raise TypeBootstrapError("type_total_row_bound")
            for row in page.rows:
                if row.get("market", "stocks") != "stocks":
                    raise TypeBootstrapError("invalid_type_market")
                if row.get("active", True) is not True:
                    raise TypeBootstrapError("invalid_type_active")
                symbol = _symbol(row.get("ticker"))
                code = row.get("type")
                if code is not None and (
                    not isinstance(code, str) or not re.fullmatch(r"[A-Z]{1,12}", code)
                ):
                    raise TypeBootstrapError("invalid_type_code")
                if symbol in types and types[symbol] != code:
                    conflicts.add(symbol)
                types[symbol] = code
            if page.next_cursor is None:
                store._save_types(session_date, types, conflicts)
                return store.snapshot(session_date)
            if not page.next_cursor or len(page.next_cursor) > 4096 or page.next_cursor in cursors:
                raise TypeBootstrapError("type_cursor_cycle_or_bound")
            cursors.add(page.next_cursor)
            cursor = page.next_cursor
        raise TypeBootstrapError("type_page_count_bound")
    except Exception:
        store._fail_preparation(session_date)
        raise TypeBootstrapError("type_preparation_failed") from None


async def prepare_type_cache(
    store: RoutingStore,
    fetch_page: Callable[[str | None], TickerPage],
    session_date: date,
    *,
    max_pages: int = 20,
    max_rows: int = 20_000,
) -> RoutingSnapshot:
    """One logical paginated prepare per day, persisted claim; failures never cache partial data."""
    return await asyncio.to_thread(
        _prepare_types, store, fetch_page, session_date, max_pages, max_rows
    )
