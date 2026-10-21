"""Presentation-only controls for the 06:26/06:28 momentum page order."""
from copy import deepcopy
from html import unescape
import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from project_mai_tai.services import control_plane as cp
from tests.unit.test_mompage2 import closed
from tests.unit.test_mompage4_live_first import app_fixture


def inputs(*, status="READABLE"):
    trade = {**closed("LIVEONLY", "08:00:34", pnl="4.96"),
             "account_name": "live:schwab_1m_v2", "quantity": "7"}
    live = {"strategy_code": "momentum_warn_live", "status": status,
            "daily_pnl": "4.96", "positions": [], "closed_today": [trade]}
    warnings = [{"strategy_code": code, "status": "READABLE", "daily_pnl": "9876.54",
                 "positions": [], "closed_today": [closed("PAPER_ONLY_SENTINEL", "08:00:34", pnl="9876.54")],
                 "recent_decisions": [{"symbol": "SIGNAL_ONLY", "status": "WATCH",
                                       "last_bar_at": "2026-10-14T08:00:34-04:00", "reason": "controlled signal"}]}
                for code in ("momentum_60s_warn", "momentum_30s_warn")]
    return ({"bots": [{"strategy_code": "schwab_1m_v2"}, {"strategy_code": "momentum_30s"}],
             "recent_orders": [], "account_positions": [], "virtual_positions": []}, live, warnings,
            {"status": "READABLE", "entry_admission_enabled": True,
             "episodes": [{"filled_quantity": "0"}]}, {"status": "UNKNOWN"})


def render(values):
    data, live, warnings, owner, metrics = values
    return cp._render_momentum_page(data, live=live, warnings=warnings, owner=owner, metrics=metrics)


def test_same_atr_workspace_components_keep_live_values_and_inputs_unchanged(monkeypatch):
    values = inputs()
    before = deepcopy(values)
    calls = []
    original = cp._render_momentum_ledger

    def capture(value, **kwargs):
        calls.append((deepcopy(value), kwargs))
        return original(value, **kwargs)

    monkeypatch.setattr(cp, "_render_momentum_ledger", capture)
    html = render(values)
    assert values == before
    assert len(calls) == 1 and calls[0][0] == cp._momentum_page_ledger_copy(values[1])
    assert calls[0][1] == {"title": "LIVE (real orders)", "method": "LIVE", "other_rows": [], "paper": False}
    for component in ('class="shell"', 'class="sidebar"', 'class="brand"', 'class="brand-badge"',
                      'class="side-section"', 'class="stack"', 'class="line-item"',
                      'class="metric-grid"', 'class="metric-card"', 'class="workspace"',
                      'class="panel-header"', 'class="nav-strip"', 'class="table-wrap"'):
        assert component in html
    for label in ("Status:", "Account:", "Mode:", "Provider:", "Interval:", "Overview",
                  "Day P&amp;L (LIVE)", "Open", "Completed trades", "Pending entries"):
        assert label in html
    assert '<span>Day P&amp;L (LIVE)</span><strong>$+4.96</strong>' in html
    assert '<span>Open</span><strong>0</strong>' in html
    assert '<span>Completed trades</span><strong>1</strong>' in html
    assert '<span>Pending entries</span><strong>1</strong>' in html
    assert 'Day total: $+4.96' in html and '1 completed broker trades' in html
    assert '<td><strong>LIVEONLY</strong>' in html and '<td style="text-align:right">7</td>' in html
    assert "'Consolas','Monaco',monospace" in html
    assert 'grid-template-columns:300px minmax(0,1fr)' in html
    assert '@media(max-width:1200px)' in html and '@media(max-width:900px)' in html


def test_no_reference_rows_or_paper_pnl_but_both_signal_intervals_and_health_remain():
    html = render(inputs())
    assert 'Warning method' not in html and 'REFERENCE' not in html
    assert 'PAPER_ONLY_SENTINEL' not in html and '9876.54' not in html
    assert 'completed paper trades' not in html and 'completed references' not in html
    assert 'SIGNAL_ONLY' in html and '<td>30s</td>' in html and '<td>60s</td>' in html
    assert 'Decision Tape' in html and 'Failed Actions' in html and 'Listening Status' in html
    assert 'Warning feed: UNKNOWN' in html
    assert 'P&L uses actual LIVE broker fills only.' in unescape(html)


@pytest.mark.parametrize("owner_status,enabled,label", [("READABLE", True, "LIVE"),
    ("READABLE", False, "OFF"), ("UNKNOWN", False, "UNKNOWN")])
def test_sidebar_status_still_comes_from_owner_proof(owner_status, enabled, label):
    values = list(inputs())
    values[3] = {"status": owner_status, "entry_admission_enabled": enabled, "episodes": []}
    html = render(values)
    assert f'<strong>Status:</strong> {label}</div>' in html
    assert '<strong>Mode:</strong> LIVE</div>' in html


def test_partial_unknown_episode_remains_visible_without_false_flat_or_zero():
    values = list(inputs(status="PARTIAL"))
    values[1]["daily_pnl_partial"] = True
    values[1]["unknown_episodes"] = [{"status": "UNKNOWN", "ticker": "CRMT", "symbol": "CRMT",
        "account_name": "live:schwab_1m_v2", "quantity": "111", "entry_price": "1.0",
        "exit_price": None, "entry_time": "2026-10-14T08:00:34-04:00", "exit_time": None,
        "pnl": None, "pnl_pct": None, "reason": "exit_time_UNKNOWN"}]
    before = deepcopy(values)
    html = render(values)
    assert values == before
    assert '$+4.96 (partial)' in html
    assert '<span>Open</span><strong>UNKNOWN</strong>' in html
    assert '<span>Completed trades</span><strong>UNKNOWN</strong>' in html
    assert 'Live positions UNKNOWN' in html and 'No open positions' not in html
    assert '<strong>LIVEONLY</strong>' in html and '<strong>CRMT</strong>' in html
    assert 'exit_time_UNKNOWN' in html and 'UNKNOWN episode; excluded from P&amp;L' in html
    assert '111' in html and 'Total completed count UNKNOWN' in html


def test_actual_atr_and_momentum_pages_share_workspace_shell_and_nav(monkeypatch):
    app, _, redis = app_fixture(monkeypatch, with_fill=False)
    with TestClient(app) as client:
        app.state.repository.redis = redis
        atr = client.get('/bot/1m-schwab-v2')
        momentum = client.get('/bot/momentum')
    assert atr.status_code == momentum.status_code == 200
    for component in ('class="shell"', 'class="sidebar"', 'class="brand"', 'class="metric-grid"',
                      'class="workspace"', 'class="panel-header"', 'class="nav-strip"'):
        assert component in atr.text and component in momentum.text
    for html in (atr.text, momentum.text):
        assert "'Consolas','Monaco',monospace" in html
        assert '/scanner/dashboard' in html and 'Mai Tai Control Plane' in html
    assert 'Warning method' not in momentum.text and 'REFERENCE' not in momentum.text
    if destination := os.environ.get('MOMFORMAT_HTML_CAPTURE'):
        Path(destination).write_text(momentum.text, encoding='utf-8')
