"""Bounded, observation-only WBQUIET1 data. Never schedules a broker operation."""
from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import islice
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
MAX_WINDOWS = 16
READER_WINDOW_SECONDS = 60
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
    read_counts: dict = field(default_factory=dict)
    committed: dict = field(default_factory=dict)
    acquisition_generations: dict = field(default_factory=dict)

    def read(self, name: str, outcome: str) -> None:
        if name in self.accounts:
            self.actual[name] = outcome
            self.acquisition_generations[name] = "UNMEASURED"
            self.read_counts[name] = self.read_counts.get(name, 0) + 1
            self.read_elapsed[name] = round(time.monotonic() - self.started, 6)

    def consumed(self, names, reader: str) -> None:
        elapsed = time.monotonic() - self.started
        for name in names:
            if name in self.accounts and len(self.readers) < MAX_READERS:
                self.readers.append((name, reader, round(elapsed, 6)))


CURRENT: ContextVar[Frame | None] = ContextVar("wbquiet_shadow_frame", default=None)
PERIODIC: ContextVar[bool] = ContextVar("wbquiet_shadow_periodic", default=False)


def note_read(name: str, outcome: str, *, service=None, positions=None) -> None:
    try:
        frame = CURRENT.get()
        if frame is not None:
            frame.read(name, outcome)
            if service is not None and name in frame.accounts:
                _, adapter = cached_adapter(service.broker_adapter, name)
                frame.acquisition_generations[name] = source_evidence(
                    adapter, name, time.monotonic(), positions=positions,
                ).get("acquisition_generation", "UNMEASURED")
    except Exception:
        pass


def note_consumed(names, reader: str, *, service=None, source="periodic_positions",
                  outcome="consumed", generation=None, positions=None, symbol=None,
                  provider=None) -> None:
    observer = None
    try:
        frame = CURRENT.get()
        if frame is not None:
            frame.consumed(names, reader)
        if service is not None:
            observer = vars(service).get("_wbquiet_shadow")
            if observer is not None:
                acquisition = "UNMEASURED"
                if isinstance(positions, (list, tuple)) and symbol is not None:
                    stamp = next((getattr(p, "as_of", None) for p in islice(positions, 256)
                                  if p.symbol.upper() == symbol.upper()
                                  and p.broker_account_name in names), None)
                    generation = stamp.isoformat() if isinstance(stamp, datetime) else None
                    if len(names) == 1:
                        _, adapter = cached_adapter(service.broker_adapter, names[0])
                        acquisition = source_evidence(adapter, names[0], time.monotonic(),
                                                      positions=positions).get("acquisition_generation", "UNMEASURED")
                observer.reader_note(service, names, reader, source, outcome, generation, provider, acquisition)
    except Exception:
        if isinstance(observer, ShadowObserver):
            observer.reader_dropped += 1


def note_committed(names) -> None:
    """Called only after the existing position-persistence transaction returns."""
    try:
        frame = CURRENT.get()
        if frame is not None:
            for name in names:
                if frame.actual.get(name) == "returned_not_wire_proof":
                    frame.committed[name] = {
                        "process_pid": os.getpid(), "pass_id": frame.pass_id, "account": name,
                        "acquisition_generation": frame.acquisition_generations.get(name, "UNMEASURED"),
                        "local_read_id": f"{os.getpid()}:{frame.pass_id}:{name}:{frame.read_counts[name]}",
                        "generation_basis": "matched_immutable_cache_objects_not_wire_or_broker_id",
                        "adapter_calls": frame.read_counts[name],
                        "acquired_elapsed_seconds": frame.read_elapsed[name],
                        "committed_elapsed_seconds": round(time.monotonic() - frame.started, 6),
                    }
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


def source_evidence(adapter, name: str, now: float, *, positions=None) -> dict:
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
        # Empty/equal-valued lists cannot prove which acquisition was returned.
        if (isinstance(positions, (list, tuple)) and 0 < len(positions) <= 256
                and len(positions) == len(cached[1])
                and all(returned is stored and returned.broker_account_name == name
                        for returned, stored in zip(positions, cached[1]))):
            evidence["acquisition_generation"] = f"adapter_cache:{stamp!r}"
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
        self.reader_lock = threading.Lock()
        self.committed_generations: dict[str, deque] = {}
        self.last_pass = None
        self.reader_sequence = 0
        self.reader_dropped = 0

    def retain(self, frame, *, published=False):
        if not self.reader_lock.acquire(blocking=False):
            self.reader_dropped += 1
            return
        try:
            now = time.monotonic()
            self.last_pass = (frame.pass_id, now)
            self.expire(now)
            for name, facts in frame.committed.items():
                if name not in self.committed_generations:
                    if len(self.committed_generations) >= MAX_ACCOUNTS:
                        self.reader_dropped += 1
                        continue
                    self.committed_generations[name] = deque(maxlen=MAX_WINDOWS)
                generations = self.committed_generations[name]
                if len(generations) == MAX_WINDOWS:
                    self.reader_dropped += 1
                committed_at = frame.started + facts.get("committed_elapsed_seconds", 0)
                generations.append((committed_at, {**facts, "shadow_receipt_emitted": published}))
        finally:
            self.reader_lock.release()

    def expire(self, now):
        for name, generations in list(self.committed_generations.items()):
            while generations and now - generations[0][0] > READER_WINDOW_SECONDS:
                generations.popleft()
            if not generations:
                del self.committed_generations[name]

    def reader_note(self, service, names, reader, source, outcome, generation, provider, acquisition):
        # Never wait for observation or retain a reader task/logging backlog.
        if not self.reader_lock.acquire(blocking=False):
            self.reader_dropped += 1
            return
        try:
            now = time.monotonic()
            self.expire(now)
            if self.last_pass is None or not 0 <= now - self.last_pass[1] <= READER_WINDOW_SECONDS:
                return
            self.reader_sequence += 1
            records = []
            for index, name in enumerate(names):
                if index >= MAX_ACCOUNTS:
                    self.reader_dropped += 1
                    break
                route, _ = cached_adapter(service.broker_adapter, name)
                actual_provider = route or "UNMEASURED"
                anchors = [{**facts, "elapsed_seconds": round(now - started, 6)}
                           for started, facts in self.committed_generations.get(name, ())
                           if 0 <= now - started <= READER_WINDOW_SECONDS]
                records.append({"account": name, "reader": reader, "source": source,
                                "provider": actual_provider,
                                "reader_provider_scope": provider or "UNMEASURED",
                                "webull_cadence_eligible": actual_provider == "webull" and provider != "schwab",
                                "outcome": outcome, "generation": generation or "UNMEASURED",
                                "generation_basis": "caller_observed_metadata_not_wire_id",
                                "adapter_calls": int(source == "adapter_positions"),
                                "adapter_read_id": (f"{os.getpid()}:{self.reader_sequence}:{name}"
                                                    if source == "adapter_positions" else None),
                                "overlapping_periodic_passes": anchors,
                                "reader_acquisition_generation": acquisition,
                                "same_source_generation": (
                                    "observed_adapter_cache_identity" if acquisition != "UNMEASURED"
                                    and not self.reader_dropped
                                    and all(a["shadow_receipt_emitted"] for a in anchors)
                                    and any(a.get("acquisition_generation") == acquisition for a in anchors)
                                    else "UNMEASURED"),
                                "periodic_evidence": ("UNMEASURED" if not anchors or self.reader_dropped
                                                      or not all(a["shadow_receipt_emitted"] for a in anchors)
                                                      or any(a.get("acquisition_generation") == "UNMEASURED"
                                                             for a in anchors)
                                                      else "committed_generation_observed")})
            record = {"process_pid": os.getpid(), "reader_sequence": self.reader_sequence,
                      "readers": records, "window_seconds": READER_WINDOW_SECONDS,
                      "dropped_reader_observations": self.reader_dropped,
                      "other_oms_and_external_readers": "PARTIAL",
                      "wire_calls": "UNMEASURED", "wire_calls_saved": "UNMEASURED",
                      "policy_applied": False}
        finally:
            self.reader_lock.release()
        # Logging is also best effort. The public hook catches its exceptions.
        service.logger.info("[WBQUIET-READER] %s", json.dumps(record, sort_keys=True))

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
                                "actual_adapter_calls": frame.read_counts.get(name, 0),
                                "committed_generation": frame.committed.get(name, "UNMEASURED"),
                                "facts_at_pass_start": facts, "covered_readers": readers})
            service.logger.info("[WBQUIET-SHADOW] %s", json.dumps({
                "pass_id": frame.pass_id, "process_pid": os.getpid(),
                "as_of": frame.at.isoformat(), "outcome": outcome,
                "accounts": records, "dropped_observations": self.dropped,
                "other_oms_and_external_readers": "UNMEASURED",
                "wire_calls_saved": "UNMEASURED", "policy_applied": False,
                "basis": "nominal_counterfactual_not_decision_equivalence",
            }, sort_keys=True))
            return True
        published = await self.off_loop(emit)
        self.retain(frame, published=published is True)


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
