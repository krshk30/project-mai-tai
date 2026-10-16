"""Country-selected ATR entries; legacy ownership is never a routing decision.

Selection is an in-memory lookup. The bot journals the sealed selection off-loop
before publication, and restores it before accepting market-data callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.fanout_identity import ATR_COUNTRY_ROUTE_KEYS as ROUTE_KEYS
from project_mai_tai.momentum_live_sec import CountryRoute
from project_mai_tai.strategy_core.entry_gate import within_entry_window
from project_mai_tai.strategy_core.time_utils import is_fillable_et_session

SNAPSHOT_TYPE = "v2_atr_country_route"
BOUND = 30_000
logger = logging.getLogger(__name__)


class CountryRouteRefusal(ValueError):
    pass


def country_entry_window(now: datetime, settings) -> bool:
    # Q1 20:3x: route_for's momentum-only pre-07 allowance cannot widen ATR.
    return within_entry_window(now, settings) and is_fillable_et_session(
        now, 7, 15, start_minute=0, end_minute=45)


@dataclass(frozen=True)
class AtrCountrySelection:
    symbol: str
    segment_id: int
    broker: str
    account: str
    reason: str
    status: str
    as_of: str
    sealed_at: str

    def metadata(self) -> dict[str, str]:
        return dict(zip(ROUTE_KEYS, (
            "1", self.symbol, str(self.segment_id), self.broker, self.account,
            "600" if self.broker == "schwab" else "300", self.reason,
            self.status, self.as_of, self.sealed_at,
        ), strict=True))


def selection_from_metadata(metadata: Mapping[str, object], settings) -> AtrCountrySelection:
    if metadata.get("atr_country_schema") != "1":
        raise CountryRouteRefusal("country_route_schema_UNKNOWN")
    try:
        symbol = str(metadata["atr_country_symbol"])
        segment = int(str(metadata["atr_country_segment_id"]))
        broker = str(metadata["atr_country_broker"])
        account = str(metadata["atr_country_account"])
        sealed_at = str(metadata["atr_country_sealed_at"])
        stamp = datetime.fromisoformat(sealed_at)
        expected = {
            "schwab": settings.strategy_schwab_1m_v2_account_name,
            "webull": settings.strategy_schwab_1m_v2_webull_account_name,
        }
        if (not symbol or symbol != symbol.strip().upper() or segment <= 0
                or broker not in expected or not account or account != expected[broker]
                or len(set(expected.values())) != 2 or stamp.tzinfo is None
                or metadata.get("atr_country_notional_usd") != ("600" if broker == "schwab" else "300")):
            raise ValueError("binding")
        as_of = str(metadata.get("atr_country_as_of", ""))
        if as_of:
            source = datetime.fromisoformat(as_of)
            if source.tzinfo is None or (source > stamp and metadata.get("atr_country_status") != "FUTURE"):
                raise ValueError("country_time")
        if broker == "schwab" and (metadata.get("atr_country_reason") != "sec_us"
                                  or metadata.get("atr_country_status") != "READY" or not as_of):
            raise ValueError("schwab_requires_ready_us")
        return AtrCountrySelection(symbol, segment, broker, account,
                                   str(metadata["atr_country_reason"]),
                                   str(metadata["atr_country_status"]), as_of, sealed_at)
    except (KeyError, TypeError, ValueError) as exc:
        raise CountryRouteRefusal("country_route_binding_UNKNOWN") from exc


class AtrCountryRouteBook:
    """One broker per economic segment, pinned across reactive/rest/reprice paths."""

    def __init__(self, settings, route_for: Callable[[str, datetime], CountryRoute], *,
                 restored: Mapping[tuple[str, int], AtrCountrySelection] | None = None,
                 legacy: set[tuple[str, int]] | None = None):
        self.settings, self.route_for = settings, route_for
        self.routes = dict(restored or {})
        self.has_webull = any(route.broker == "webull" for route in self.routes.values())
        self.legacy = set(legacy or ()) - self.routes.keys()
        self.readable = True

    def is_single(self, symbol: str, segment_id: int) -> bool:
        return (symbol.upper(), int(segment_id)) not in self.legacy

    def select(self, symbol: str, segment_id: int, now: datetime) -> AtrCountrySelection | None:
        key = (symbol.upper(), int(segment_id))
        if not self.is_single(*key):
            return None
        if not self.readable:
            raise CountryRouteRefusal("country_restore_UNKNOWN")
        if not country_entry_window(now, self.settings):
            raise CountryRouteRefusal("country_entry_window_closed")
        if key in self.routes:
            if datetime.fromisoformat(self.routes[key].sealed_at) > now:
                raise CountryRouteRefusal("country_seal_future")
            return self.routes[key]
        if len(self.routes) >= BOUND:
            raise CountryRouteRefusal("country_route_book_bound")
        route = self.route_for(symbol, now)
        if route.broker not in {"schwab", "webull"}:
            raise CountryRouteRefusal("country_route_closed")
        metadata = {
            "atr_country_schema": "1", "atr_country_symbol": key[0],
            "atr_country_segment_id": str(key[1]), "atr_country_broker": route.broker,
            "atr_country_account": (self.settings.strategy_schwab_1m_v2_account_name
                                    if route.broker == "schwab"
                                    else self.settings.strategy_schwab_1m_v2_webull_account_name),
            "atr_country_notional_usd": "600" if route.broker == "schwab" else "300",
            "atr_country_reason": route.reason, "atr_country_status": route.status,
            "atr_country_as_of": route.as_of.isoformat() if route.as_of else "",
            "atr_country_sealed_at": now.astimezone(UTC).isoformat(),
        }
        selected = selection_from_metadata(metadata, self.settings)
        self.routes[key] = selected
        self.has_webull |= selected.broker == "webull"
        logger.info("[ATR-COUNTRY-SELECTED] symbol=%s segment=%s route=%s reason=%s status=%s as_of=%s notional=%s",
                    selected.symbol, selected.segment_id, selected.broker, selected.reason,
                    selected.status, selected.as_of or "UNKNOWN", metadata["atr_country_notional_usd"])
        return selected


class AtrCountryRouteStore:
    """Existing snapshot table, deterministic identity, no schema or broker reads."""

    def __init__(self, session_factory, settings):
        self.factory, self.settings = session_factory, settings

    @staticmethod
    def identity(selected: AtrCountrySelection):
        return uuid5(NAMESPACE_URL, f"mai-tai:atr-country:v1:{selected.symbol}:{selected.segment_id}")

    def restore(self) -> dict[tuple[str, int], AtrCountrySelection]:
        with self.factory() as session:
            rows = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE).limit(BOUND + 1)).all()
            if len(rows) > BOUND:
                raise CountryRouteRefusal("country_route_restore_bound")
            restored = {}
            for row in rows:
                selected = selection_from_metadata(row.payload, self.settings)
                key = (selected.symbol, selected.segment_id)
                if row.id != self.identity(selected) or key in restored:
                    raise CountryRouteRefusal("country_route_history_conflict")
                restored[key] = selected
            return restored

    def record(self, selected: AtrCountrySelection) -> None:
        if selection_from_metadata(selected.metadata(), self.settings) != selected:
            raise CountryRouteRefusal("country_journal_selection_UNKNOWN")
        with self.factory() as session:
            row = session.get(DashboardSnapshot, self.identity(selected))
            if row is not None:
                if row.snapshot_type != SNAPSHOT_TYPE or row.payload != selected.metadata():
                    raise CountryRouteRefusal("country_route_journal_conflict")
                return
            session.add(DashboardSnapshot(id=self.identity(selected), snapshot_type=SNAPSHOT_TYPE,
                                          payload=selected.metadata()))
            session.commit()
