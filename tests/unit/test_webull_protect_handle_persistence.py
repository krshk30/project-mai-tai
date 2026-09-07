"""HDL1 — the Webull protect handle must survive the race it loses today.

⭐ MEASURED 2026-09-07: 10 of 78 attachments (12.8%) over the 7 POST-DEPLOY sessions lost the
handle, and in **10 of 10** the filled entry row exists within **0-1 SECONDS** of the failure.
⛔ The denominator is 78, not 86: handle persistence landed in `581186f` on 2026-08-25 20:22 ET and
the 8 earlier attachments that day carry no `handle_persisted=`, so they could never exercise this
failure. Counting them understated the rate. Every one took
the "no filled entry order accepted the handle" branch; the exception branch fired ZERO times.
⇒ It is a TIMING RACE, not a swallowed error and not a path that never attempts.

`_spawn_webull_protection` runs the attach OFF the fill path deliberately — it retries with sleeps
and must never stall a fill. That background task then beats the outer fill transaction's commit,
`_find_oco_entry_order` finds no FILLED row carrying the entry coid, and the handle is lost. The
pair still rests at the broker, so a restart in that window leaves us blind to what is guarding a
live position and a second pair may be placed on top.

⛔ 12.8% is the rate we can SEE. Rotated logs start 2026-08-19; nothing earlier survives.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import SystemIncident
from project_mai_tai.oms.service import OmsRiskService

ACCT, SYM, BASE = "live:orb", "IMRN", "schwab_1m_v2-IMRN-protect-775584b0e2fb"
ENTRY = "schwab_1m_v2-IMRN-open-2c2de6b319f9"


class _Log(logging.Logger):
    def __init__(self) -> None:
        super().__init__("hdl1-test")
        self.lines: list[str] = []

    def handle(self, record: logging.LogRecord) -> None:  # pragma: no cover - capture only
        self.lines.append(record.getMessage() % record.args if record.args else record.getMessage())

    def _log(self, level, msg, args, **kw):  # noqa: ANN001
        self.lines.append(msg % args if args else msg)


def _svc(*, session_factory=None):
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.logger = _Log()
    if session_factory is not None:
        svc.session_factory = session_factory
    return svc


def _sqlite_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[SystemIncident.__table__])
    return sessionmaker(bind=engine, expire_on_commit=False)


def _no_sleep(monkeypatch) -> list[float]:
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return slept


# ------------------------------------------------------------------ THE REGRESSION
def test_handle_survives_the_fill_commit_race(monkeypatch) -> None:
    """THE 10-of-10 CASE. The first read loses the race; a later one wins. Without the retry the
    handle is lost even though the row appears ~1s later."""
    slept = _no_sleep(monkeypatch)
    svc = _svc()
    calls: list[int] = []

    async def _run_db(fn, *, commit: bool = True):
        calls.append(1)
        return len(calls) >= 3  # the entry row becomes visible on the third read

    svc._run_db = _run_db
    ok = asyncio.run(
        svc._persist_webull_protect_base(ACCT, SYM, BASE, entry_client_order_id=ENTRY)
    )
    assert ok is True, (
        "the handle was lost although the entry row appeared before the bound — this is the "
        "10-of-10 live race, and without the retry it is silently unaddressable"
    )
    assert len(calls) == 3, f"expected 3 reads, got {len(calls)}"
    assert slept, "the retry must back off between reads rather than spin"
    assert any("HANDLE-PERSISTED" in line for line in svc.logger.lines), (
        "a handle rescued by the retry must say so — that line is how we measure whether the "
        "retry is doing any work"
    )


def test_a_first_attempt_success_does_not_retry_or_sleep(monkeypatch) -> None:
    """The common path must be unchanged: one read, no backoff, no incident."""
    slept = _no_sleep(monkeypatch)
    svc = _svc()
    calls: list[int] = []

    async def _run_db(fn, *, commit: bool = True):
        calls.append(1)
        return True

    svc._run_db = _run_db
    assert asyncio.run(svc._persist_webull_protect_base(ACCT, SYM, BASE)) is True
    assert len(calls) == 1, f"a successful first read must not re-read ({len(calls)} reads)"
    assert slept == [], "no backoff may run when the first read succeeds"
    assert not any("HANDLE-LOST" in line for line in svc.logger.lines)


def test_every_attempt_reads_through_run_db_so_the_session_is_fresh(monkeypatch) -> None:
    """⛔ A retry on a REUSED session would re-read the same stale snapshot and could never
    succeed. Every attempt must go through `_run_db`, which opens its own session."""
    _no_sleep(monkeypatch)
    svc = _svc()
    seen: list[bool] = []

    async def _run_db(fn, *, commit: bool = True):
        seen.append(commit)
        return False

    svc._run_db = _run_db
    svc._raise_webull_handle_lost_incident = _swallow
    asyncio.run(svc._persist_webull_protect_base(ACCT, SYM, BASE))
    assert len(seen) == svc._WEBULL_HANDLE_PERSIST_ATTEMPTS
    assert all(seen), "each attempt must commit its own unit of work"


async def _swallow(*_a, **_k) -> None:
    return None


def _live_run_db(factory):
    """A real _run_db: a FRESH session per call, exactly as the worker-thread version behaves."""
    async def _run_db(fn, *, commit: bool = True):
        with factory() as session:
            result = fn(session)
            if commit:
                session.commit()
            return result
    return _run_db


# ------------------------------------------------------------------ THE BOUND
def test_the_retry_is_bounded(monkeypatch) -> None:
    """A handle that never lands must STOP, not spin against a live broker path."""
    _no_sleep(monkeypatch)
    svc = _svc()
    calls: list[int] = []

    async def _run_db(fn, *, commit: bool = True):
        calls.append(1)
        return False

    svc._run_db = _run_db
    svc._raise_webull_handle_lost_incident = _swallow
    assert asyncio.run(svc._persist_webull_protect_base(ACCT, SYM, BASE)) is False
    assert len(calls) == svc._WEBULL_HANDLE_PERSIST_ATTEMPTS, (
        f"unbounded retry: {len(calls)} reads against a bound of "
        f"{svc._WEBULL_HANDLE_PERSIST_ATTEMPTS}"
    )


def test_an_exception_is_retried_and_then_reported_distinctly(monkeypatch) -> None:
    """The two failure branches must stay DISTINGUISHABLE — that distinction is what let the
    diagnosis separate a race from a swallowed error in the first place."""
    _no_sleep(monkeypatch)
    svc = _svc()

    async def _run_db(fn, *, commit: bool = True):
        raise RuntimeError("db unavailable")

    svc._run_db = _run_db
    svc._raise_webull_handle_lost_incident = _swallow
    assert asyncio.run(svc._persist_webull_protect_base(ACCT, SYM, BASE)) is False
    text = "\n".join(svc.logger.lines)
    assert "unaddressable after restart" in text, "the exception branch lost its own wording"
    assert "no filled entry order accepted" not in text, (
        "an exception was reported as the race branch — the two causes are no longer separable"
    )


# ------------------------------------------------------------------ THE SILENCE
def test_an_exhausted_retry_raises_a_durable_operator_incident(monkeypatch) -> None:
    """⛔ HANDLE-LOST already fired 1:1 with the failure and NOTHING read it — the same silence as
    SIL1. A resting pair whose children cannot be addressed must reach the operator."""
    _no_sleep(monkeypatch)
    factory = _sqlite_factory()
    svc = _svc(session_factory=factory)

    async def _run_db(fn, *, commit: bool = True):
        with factory() as session:
            result = fn(session)
            if commit:
                session.commit()
            return result

    svc._run_db = _run_db
    svc._find_oco_entry_order = lambda *a, **k: None  # the race never resolves
    assert asyncio.run(svc._persist_webull_protect_base(ACCT, SYM, BASE)) is False
    with factory() as session:
        incidents = session.scalars(select(SystemIncident)).all()
    assert len(incidents) == 1, (
        f"a lost handle raised {len(incidents)} incidents — it must be visible exactly once"
    )
    inc = incidents[0]
    assert inc.status == "open" and inc.severity == "critical"
    assert inc.payload["source"] == "oms_webull_protect_handle_lost"
    assert inc.payload["broker_account_name"] == ACCT and inc.payload["symbol"] == SYM
    assert inc.payload["base_client_order_id"] == BASE, (
        "the incident must name the base coid — it is the only handle that can address the "
        "resting children by hand"
    )
    assert SYM in inc.title and ACCT in inc.title


def test_repeated_loss_on_one_symbol_does_not_multiply_incidents(monkeypatch) -> None:
    """One open incident per (account, symbol) — a storm must not bury the panel."""
    _no_sleep(monkeypatch)
    factory = _sqlite_factory()
    svc = _svc(session_factory=factory)
    svc._run_db = _live_run_db(factory)
    svc._find_oco_entry_order = lambda *a, **k: None
    for _ in range(3):
        asyncio.run(svc._persist_webull_protect_base(ACCT, SYM, BASE))
    with factory() as session:
        assert len(session.scalars(select(SystemIncident)).all()) == 1


# ------------------------------------------------------------------ THE DEDUPE MUST NOT DESTROY
def test_a_second_loss_keeps_the_first_handle(monkeypatch) -> None:
    """⛔ THE DEDUPE COLLAPSES THE ROW, NEVER THE EVIDENCE.

    Found by codex-2 reviewing this PR. Collapsing to one incident used to REPLACE the payload, so
    a second loss on the same (account, symbol) erased the FIRST pair's base coid — the only handle
    that could ever address it. That destroys exactly what this incident exists to preserve: the
    stated hazard is a second pair landing on top of an unaddressable first one.

    ⛔ The earlier version of the repeated-loss test above passed the SAME base three times, so it
    could not have caught this. A control whose inputs cannot distinguish the defect is not a
    control. Distinct bases are the whole point of this test."""
    _no_sleep(monkeypatch)
    factory = _sqlite_factory()
    svc = _svc(session_factory=factory)
    svc._run_db = _live_run_db(factory)
    svc._find_oco_entry_order = lambda *a, **k: None
    first, second = f"{BASE}-FIRST", f"{BASE}-SECOND"
    asyncio.run(svc._persist_webull_protect_base(ACCT, SYM, first))
    asyncio.run(svc._persist_webull_protect_base(ACCT, SYM, second))
    with factory() as session:
        incidents = session.scalars(select(SystemIncident)).all()
    assert len(incidents) == 1, "still one row per (account, symbol)"
    handles = incidents[0].payload["base_client_order_ids"]
    assert first in handles, (
        "the FIRST pair's handle was erased by the second loss — that pair is now unaddressable "
        "and nothing records it ever existed"
    )
    assert second in handles
    assert incidents[0].payload["loss_count"] == 2


def test_the_operator_surface_carries_the_handle(monkeypatch) -> None:
    """⛔ THE TITLE IS ALL THE OPERATOR SEES. `load_dashboard_data` serialises incidents as
    service/severity/title/status/opened_at and DISCARDS the payload, so a handle stored only in
    the payload never reaches the panel — while the incident text instructs a manual action that
    needs it. Found by codex-2. Assert the operator-facing field, not the internal one."""
    _no_sleep(monkeypatch)
    factory = _sqlite_factory()
    svc = _svc(session_factory=factory)
    svc._run_db = _live_run_db(factory)
    svc._find_oco_entry_order = lambda *a, **k: None
    asyncio.run(svc._persist_webull_protect_base(ACCT, SYM, BASE))
    with factory() as session:
        incident = session.scalars(select(SystemIncident)).one()
    assert BASE in incident.title, (
        "the base coid is not on the operator surface; the alert asks for a manual action and "
        "withholds the only handle that can perform it"
    )
    assert SYM in incident.title and ACCT in incident.title
    assert len(incident.title) <= 255, "SystemIncident.title is String(255)"


def test_many_handles_stay_within_the_title_column(monkeypatch) -> None:
    """The title must stay inside String(255) and say how many it could not show."""
    _no_sleep(monkeypatch)
    factory = _sqlite_factory()
    svc = _svc(session_factory=factory)
    svc._run_db = _live_run_db(factory)
    svc._find_oco_entry_order = lambda *a, **k: None
    for index in range(12):
        asyncio.run(svc._persist_webull_protect_base(ACCT, SYM, f"{BASE}-{index:02d}"))
    with factory() as session:
        incident = session.scalars(select(SystemIncident)).one()
    assert len(incident.title) <= 255, f"title overflowed the column at {len(incident.title)}"
    assert len(incident.payload["base_client_order_ids"]) == 12, "every handle must survive"
    assert "more in payload" in incident.title, "the operator must be told the list is truncated"
