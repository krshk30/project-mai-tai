"""Snapshot-persist debounce (#350 piece #1).

Validates the trailing-edge debounce on `_replace_dashboard_snapshot`:
  * throttle=0 -> persist every call (byte-identical to today),
  * throttle>0 -> coalesce per snapshot_type, keep only the LATEST (trailing),
  * periodic flush persists the trailing snapshot once the window elapses,
  * force flush (shutdown / day-roll) persists pending regardless of window,
  * never drops the latest; per-type independent.

Uses `StrategyEngineService.__new__` (no Redis/DB) with `_do_replace_dashboard_snapshot`
stubbed and a controllable clock patched over the module's `utcnow`.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import project_mai_tai.services.strategy_engine_app as se


class _Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += timedelta(seconds=secs)


def _svc(throttle: float) -> se.StrategyEngineService:
    svc = se.StrategyEngineService.__new__(se.StrategyEngineService)
    svc.session_factory = object()  # non-None so the gate doesn't early-return
    svc._snapshot_throttle_secs = throttle
    svc._snapshot_last_persist_at = {}
    svc._snapshot_pending = {}
    svc.persisted: list = []
    svc._do_replace_dashboard_snapshot = lambda t, p: svc.persisted.append((t, dict(p)))
    return svc


def test_throttle_off_persists_every_call(monkeypatch):
    monkeypatch.setattr(se, "utcnow", _Clock())
    svc = _svc(0.0)
    for i in range(5):
        svc._replace_dashboard_snapshot("dash", {"i": i})
    assert [p["i"] for _, p in svc.persisted] == [0, 1, 2, 3, 4]  # byte-identical: every call
    assert svc._snapshot_pending == {}


def test_debounce_coalesces_then_trailing_flush(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(se, "utcnow", clock)
    svc = _svc(1.0)
    svc._replace_dashboard_snapshot("dash", {"i": 0})        # first -> persist now
    assert svc.persisted == [("dash", {"i": 0})]
    for i in (1, 2, 3):                                       # within the 1s window
        clock.advance(0.1)
        svc._replace_dashboard_snapshot("dash", {"i": i})
    assert len(svc.persisted) == 1                            # coalesced — no extra persist
    assert svc._snapshot_pending["dash"] == {"i": 3}          # only the LATEST kept
    clock.advance(1.0)                                        # window elapses
    svc._flush_pending_snapshots()                            # trailing-edge drain
    assert svc.persisted[-1] == ("dash", {"i": 3})            # latest persisted
    assert svc._snapshot_pending == {}


def test_flush_is_noop_within_window(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(se, "utcnow", clock)
    svc = _svc(1.0)
    svc._replace_dashboard_snapshot("dash", {"i": 0})
    clock.advance(0.2)
    svc._replace_dashboard_snapshot("dash", {"i": 1})         # pending
    svc._flush_pending_snapshots()                            # still inside window
    assert len(svc.persisted) == 1                            # not yet flushed
    assert svc._snapshot_pending["dash"] == {"i": 1}


def test_force_flush_ignores_window(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(se, "utcnow", clock)
    svc = _svc(5.0)
    svc._replace_dashboard_snapshot("dash", {"i": 0})
    clock.advance(0.1)
    svc._replace_dashboard_snapshot("dash", {"i": 1})         # pending, window not elapsed
    svc._flush_pending_snapshots(force=True)                  # shutdown / day-roll
    assert svc.persisted[-1] == ("dash", {"i": 1})            # persisted despite window
    assert svc._snapshot_pending == {}


def test_per_type_independent(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(se, "utcnow", clock)
    svc = _svc(1.0)
    svc._replace_dashboard_snapshot("a", {"i": 0})            # first a -> persist
    svc._replace_dashboard_snapshot("b", {"i": 0})            # first b -> persist
    assert ("a", {"i": 0}) in svc.persisted and ("b", {"i": 0}) in svc.persisted
    clock.advance(0.1)
    svc._replace_dashboard_snapshot("a", {"i": 1})            # coalesce a
    svc._replace_dashboard_snapshot("b", {"i": 1})            # coalesce b
    assert svc._snapshot_pending == {"a": {"i": 1}, "b": {"i": 1}}


def test_first_call_per_type_persists_immediately(monkeypatch):
    # No cold-start delay: the very first snapshot of a type is written at once.
    monkeypatch.setattr(se, "utcnow", _Clock())
    svc = _svc(10.0)
    svc._replace_dashboard_snapshot("dash", {"i": 0})
    assert svc.persisted == [("dash", {"i": 0})]
