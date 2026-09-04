from __future__ import annotations

import types

from project_mai_tai.services.orb_app import OrbService, _SymbolState
from project_mai_tai.strategy_core.orb_intrabar import OpeningRange, OrbBar


def _svc(universe):
    svc = OrbService.__new__(OrbService)
    svc.settings = types.SimpleNamespace(orb_broker_account_name="paper:orb", orb_trail_pct=8.0)
    svc._universe = {s.upper() for s in universe}
    svc._states = {}
    return svc


def test_heartbeat_reflects_paper_status_without_claiming_a_position():
    svc = _svc(["ARMED", "ENTERED"])
    svc._states = {
        "BUILD": _SymbolState(or_bars=[OrbBar(None, 5, 5.09, 4.95, 5.0, 100)], or_evaluated=False),
        "ARMED": _SymbolState(or_evaluated=True, opening_range=OpeningRange(5.09, 4.95, 100.0)),
        "SKIP": _SymbolState(or_evaluated=True, opening_range=None),
        "ENTERED": _SymbolState(
            or_evaluated=True, opening_range=OpeningRange(5.09, 4.95, 100.0),
            paper_entries=1, last_paper_entry_price=5.33,
        ),
    }
    p = svc._build_heartbeat_payload()
    assert p.strategy_code == "orb" and p.account_name == "paper:orb"
    statuses = {r["ticker"]: r["status"] for r in p.recent_decisions}
    assert statuses == {
        "BUILD": "building_or",
        "ARMED": "armed",
        "SKIP": "skipped",
        "ENTERED": "paper_entry_recorded",
    }
    assert p.watchlist == ["ARMED", "ENTERED"]
    assert p.data_health["status"] == "healthy" and p.data_health["universe_size"] == 2
    assert p.data_health["execution_mode"] == "paper"
    assert p.data_health["broker_route"] == "none"
    assert p.positions == []


def test_heartbeat_empty_when_idle():
    svc = _svc([])
    p = svc._build_heartbeat_payload()
    assert p.watchlist == [] and p.recent_decisions == [] and p.positions == []
    assert p.data_health["status"] == "healthy"
