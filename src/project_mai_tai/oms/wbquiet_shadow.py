"""Bounded, observation-only WBQUIET1 data. Never schedules a broker operation."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import math
import os
import threading
import time
from zoneinfo import ZoneInfo

from sqlalchemy import exists, or_, select, text

from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, Fill, OmsManagedPosition,
    TradeIntent, VirtualPosition,
)

MAX_ACCOUNTS = 16
MAX_READERS = 48
WAIT_SECONDS = 0.05
TERMINAL = ("filled", "cancelled", "canceled", "rejected", "aborted", "expired")
ET = ZoneInfo("America/New_York")


@dataclass
class Frame:
    pass_id: int
    at: datetime
    started: float
    accounts: dict = field(default_factory=dict)
    actual: dict = field(default_factory=dict)
    readers: list = field(default_factory=list)
    read_elapsed: dict = field(default_factory=dict)

    def read(self, name: str, outcome: str) -> None:
        if name in self.accounts:
            self.actual[name] = outcome
            self.read_elapsed[name] = round(time.monotonic() - self.started, 6)

    def consumed(self, names, reader: str) -> None:
        elapsed = time.monotonic() - self.started
        for name in names:
            if name in self.accounts and len(self.readers) < MAX_READERS:
                self.readers.append((name, reader, round(elapsed, 6)))


CURRENT: ContextVar[Frame | None] = ContextVar("wbquiet_shadow_frame", default=None)
PERIODIC: ContextVar[bool] = ContextVar("wbquiet_shadow_periodic", default=False)


def note_read(name: str, outcome: str) -> None:
    try:
        frame = CURRENT.get()
        if frame is not None:
            frame.read(name, outcome)
    except Exception:
        pass


def note_consumed(names, reader: str) -> None:
    try:
        frame = CURRENT.get()
        if frame is not None:
            frame.consumed(names, reader)
    except Exception:
        pass


def bind_pass(frame, pass_id: int) -> None:
    try:
        if frame is not None:
            frame.pass_id = pass_id
    except Exception:
        pass


def cached_adapter(router, name):
    """Inspect already-constructed routing only; do not instantiate an adapter."""
    values = vars(router)
    if "provider_by_account" in values:
        provider = values["provider_by_account"].get(name, values.get("default_provider"))
        return provider, values.get("_adapters_by_provider", {}).get(provider)
    if "accounts_by_name" in values and "app_key" in values:
        return "webull", router
    return None, None


def source_evidence(adapter, name: str, now: float) -> dict:
    evidence = {"source_age_seconds": None, "source": "UNMEASURED", "fresh": False}
    if adapter is None:
        return evidence
    values = vars(adapter)
    lock = values.get("_positions_lock")
    if lock is None or not lock.acquire(blocking=False):
        return evidence
    try:
        cached = values.get("_positions_cache", {}).get(name)
        if cached is None:
            return evidence
        stamp = float(cached[0])
        age = now - stamp
        ttl = float(values.get("_positions_throttle_secs", 10.0))
        backed_off = float(values.get("_positions_backoff_until", {}).get(name, 0)) > now
        if not all(math.isfinite(v) for v in (stamp, age, ttl)) or age < 0:
            return evidence
        evidence.update(source="adapter_cache_acquisition_not_pass_or_wire_id",
                        source_age_seconds=round(age, 6),
                        fresh=age < ttl and not backed_off)
        if len(cached[1]) > 256:
            evidence["fresh"] = False
        else:
            evidence["broker_held"] = any(item.quantity != 0 for item in cached[1])
        return evidence
    finally:
        lock.release()


def classify(row: dict, at: datetime) -> tuple[str, int | None]:
    if row.get("configured") is False:
        return "no_credentials", None
    if row.get("held") or row.get("broker_held"):
        return "held", 15
    if row.get("working"):
        return "working", 15
    if row.get("fresh_fill"):
        return "fresh-fill", 15
    if not row.get("known") or not row.get("fresh"):
        return "unknown", 15
    local = at.astimezone(ET)
    if local.hour >= 20 or local.hour < 4:
        return "overnight", 300
    return "flat", 60


def sample_books(session_factory, names, now: datetime) -> dict:
    """Separate rollback-only session; no effect on the sync transaction."""
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '100ms'"))
            session.execute(text("SET LOCAL lock_timeout = '25ms'"))
        account = BrokerAccount
        held = or_(
            exists().where(OmsManagedPosition.broker_account_name == account.name,
                           OmsManagedPosition.status == "open"),
            exists().where(VirtualPosition.broker_account_id == account.id,
                           VirtualPosition.quantity != 0),
            exists().where(AccountPosition.broker_account_id == account.id,
                           AccountPosition.quantity != 0),
        )
        working = or_(
            exists().where(BrokerOrder.broker_account_id == account.id,
                           BrokerOrder.status.not_in(TERMINAL)),
            exists().where(TradeIntent.broker_account_id == account.id,
                           TradeIntent.status.in_(("pending", "created", "processing", "submitting",
                                                   "submitted", "accepted", "partially_filled",
                                                   "held", "queued"))),
        )
        recent = exists().where(Fill.broker_account_id == account.id,
                                Fill.filled_at >= now - timedelta(minutes=10))
        rows = session.execute(select(account.name, held, working, recent)
                               .where(account.name.in_(names)).limit(MAX_ACCOUNTS)).all()
        return {name: {"known": True, "held": bool(h), "working": bool(w),
                       "fresh_fill": bool(f)} for name, h, w, f in rows}


class ShadowObserver:
    def __init__(self):
        self.busy = threading.Lock()
        self.sequence = 0
        self.last_nominal: dict[str, float] = {}
        self.dropped = 0

    async def off_loop(self, fn):
        # The worker owns the lock until it REALLY finishes, even after timeout.
        # At most one outstanding thread, with no work queue or unbounded tasks.
        if not self.busy.acquire(blocking=False):
            self.dropped += 1
            return None

        def work():
            try:
                return fn()
            except Exception:
                self.dropped += 1
                return None
            finally:
                self.busy.release()

        task = asyncio.create_task(asyncio.to_thread(work))
        try:
            return await asyncio.wait_for(asyncio.shield(task), WAIT_SECONDS)
        except TimeoutError:
            self.dropped += 1
            return None

    async def prepare(self, service, account_names) -> Frame | None:
        def collect():
            started = time.monotonic()
            at = datetime.now(UTC)
            router = service.broker_adapter
            values = vars(router)
            routes = values.get("provider_by_account", {})
            names = list(account_names) if account_names is not None else list(routes)
            if not routes:
                if not names:
                    names = list(vars(router).get("accounts_by_name", {}))
            # Excess accounts fail observation, rather than silently certifying a subset.
            names = [n for n in names if cached_adapter(router, n)[0] == "webull"]
            if len(names) > MAX_ACCOUNTS:
                raise ValueError("shadow account bound")
            if not names:
                return None
            books = sample_books(service.session_factory, names, at)
            self.sequence += 1
            frame = Frame(self.sequence, at, started)
            for name in names:
                _, adapter = cached_adapter(router, name)
                config = vars(adapter) if adapter is not None else {}
                configured = (bool(config.get("app_key") and config.get("app_secret")
                                   and name in config.get("accounts_by_name", {}))
                              if adapter is not None else None)
                frame.accounts[name] = {
                    **books.get(name, {"known": False}), "configured": configured,
                    **source_evidence(adapter, name, started),
                }
            return frame
        return await self.off_loop(collect)

    async def finish(self, service, frame: Frame, outcome: str):
        def emit():
            records = []
            for name, facts in frame.accounts.items():
                state, cadence = classify(facts, frame.at)
                actual = frame.actual.get(name, "not_reached")
                # A failed actual read vetoes quiet classification in this shadow receipt.
                if actual == "unreadable" and state != "no_credentials":
                    state, cadence = "unknown", 15
                previous = self.last_nominal.get(name)
                action = ("ignore" if cadence is None else "read" if state == "unknown"
                          or previous is None or frame.started - previous >= cadence else "skip")
                if actual == "not_reached" and cadence is not None:
                    state, action = "unknown", "not_evaluated"
                if action == "read":
                    if len(self.last_nominal) < MAX_ACCOUNTS or name in self.last_nominal:
                        self.last_nominal[name] = frame.started
                readers = [{"reader": reader, "elapsed_seconds": elapsed,
                            "within_60s": elapsed <= 60}
                           for acct, reader, elapsed in frame.readers if acct == name]
                records.append({"account": name, "state": state, "nominal_seconds": cadence,
                                "would": action, "actual_adapter_read": actual,
                                "actual_read_completed_elapsed_seconds": frame.read_elapsed.get(name),
                                "facts_at_pass_start": facts, "covered_readers": readers})
            service.logger.info("[WBQUIET-SHADOW] %s", json.dumps({
                "pass_id": frame.pass_id, "process_pid": os.getpid(),
                "as_of": frame.at.isoformat(), "outcome": outcome,
                "accounts": records, "dropped_observations": self.dropped,
                "other_oms_and_external_readers": "UNMEASURED",
                "wire_calls_saved": "UNMEASURED", "policy_applied": False,
                "basis": "nominal_counterfactual_not_decision_equivalence",
            }, sort_keys=True))
        await self.off_loop(emit)


async def prepare(service, account_names):
    if not PERIODIC.get():
        return None, None
    try:
        observer = service.__dict__.setdefault("_wbquiet_shadow", ShadowObserver())
        frame = await observer.prepare(service, account_names)
        return observer, frame
    except Exception:
        return None, None


async def finish(service, observer, frame, outcome):
    try:
        if observer is not None and frame is not None:
            await observer.finish(service, frame, outcome)
    except Exception:
        pass
