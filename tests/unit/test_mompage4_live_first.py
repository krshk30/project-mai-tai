"""MOMPAGE4 presentation-only controls; no provider or retained-live evidence."""

from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount
from project_mai_tai.services import control_plane as cp
from tests.unit.test_momlive1_page_entry_time import cycle
from tests.unit.test_mompage1 import RedisSnapshot, make_app
from tests.unit.test_mompage2 import closed
from tests.unit.test_momwarn1_warning_page_metrics import sample


NOW = int(datetime(2026, 10, 15, 14, tzinfo=UTC).timestamp() * 1e9)


def owner(**changes):
    return {"version": 1, "source_service": "oms", "strategy_code": "momentum_warn_live",
            "observed_ns": NOW, "producer_instance_id": "123:456", "restore_ready": True,
            "entry_admission_enabled": True, "account_names": ["live:orb", "live:schwab_1m_v2"],
            "episodes": [], "quote_symbols": [], **changes}


class PageRedis(RedisSnapshot):
    def __init__(self, proof):
        super().__init__(running=True)
        self.proof, self.calls = proof, []

    async def xrevrange(self, stream, **kwargs):
        self.calls.append((stream, kwargs))
        if stream.endswith(":momentum-live-owner-proof"):
            return [] if self.proof is None else [("1-0", {"data": json.dumps(self.proof)})]
        if stream.endswith(":momentum-warning-metrics"):
            value = sample(observed_ns=NOW, coverage_status="provider_event_observed",
                           A_subscription_requested_ns=NOW-1_000_000_000)
            return [("1-0", {"data": json.dumps(value)})]
        return await super().xrevrange(stream, **kwargs)


def app_fixture(monkeypatch, *, proof=None, with_fill=True):
    app, factory = make_app(monkeypatch, records=False)
    monkeypatch.setattr(cp, "utcnow", lambda: datetime.fromtimestamp(NOW / 1e9, UTC))
    monkeypatch.setattr(cp.time, "time_ns", lambda: NOW)
    if with_fill:
        cycle(factory, "AIXI", [(datetime(2026, 10, 15, 13, tzinfo=UTC), "31", "3.11")],
              datetime(2026, 10, 15, 13, 1, tzinfo=UTC), "3.27")
        with factory.begin() as session:
            session.scalar(select(BrokerAccount).where(BrokerAccount.name == "live:webull")).name = "live:orb"
    redis = PageRedis(owner() if proof is None else proof)
    return app, factory, redis


@pytest.mark.parametrize("route", ["/bot/momentum-30", "/bot/momentum-60"])
def test_old_urls_are_actual_http_redirects_to_one_page(monkeypatch, route):
    app, _, _ = app_fixture(monkeypatch, with_fill=False)
    with TestClient(app) as client:
        response = client.get(route, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/bot/momentum"


@pytest.mark.parametrize("enabled,label", [(True, "LIVE"), (False, "OFF")])
def test_sidebar_uses_owner_admission_not_local_flag_and_live_accounts(monkeypatch, enabled, label):
    app, _, redis = app_fixture(monkeypatch, proof=owner(entry_admission_enabled=enabled))
    with TestClient(app) as client:
        app.state.repository.redis = redis
        response = client.get("/bot/momentum")
    assert response.status_code == 200
    sidebar = response.text.split('<aside id="momentum-sidebar" class="sidebar">', 1)[1].split('</aside>', 1)[0]
    assert f'<strong>Status:</strong> {label}</div>' in sidebar
    assert '<strong>Mode:</strong> LIVE</div>' in sidebar
    assert 'live:orb (Webull)' in sidebar and 'live:schwab_1m_v2 (Schwab)' in sidebar
    assert 'Webull + Schwab (SEC route)' in sidebar and '30s + 60s' in sidebar
    assert 'paper' not in sidebar.lower()
    assert [call for call in redis.calls if call[0].endswith(':momentum-live-owner-proof')] == [
        ('test:momentum-live-owner-proof', {'count': 1})]


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"observed_ns": NOW-3_000_000_001}, {"observed_ns": NOW+1}, {"observed_ns": True},
    {"entry_admission_enabled": "true"}, {"restore_ready": False}, {"source_service": "other"},
    {"account_names": ["paper:momentum"]}, {"producer_instance_id": "unknown"},
    {"version": True}, {"episodes": [{"symbol": "AIXI"}]},
])
async def test_bad_owner_proof_is_unknown_not_off_or_live(changes):
    result = await cp._read_momentum_page_owner(PageRedis(owner(**changes)), 'test', now_ns=NOW)
    assert result == {"status": "UNKNOWN"}


@pytest.mark.asyncio
async def test_absent_owner_proof_unknown_and_exact_three_second_boundary_readable():
    assert await cp._read_momentum_page_owner(PageRedis(None), 'test', now_ns=NOW) == {"status": "UNKNOWN"}
    assert (await cp._read_momentum_page_owner(PageRedis(owner(observed_ns=NOW-3_000_000_000)),
                                              'test', now_ns=NOW))["status"] == "READABLE"


def test_one_live_pnl_warning_references_never_aggregate_and_html_capture(monkeypatch):
    app, _, redis = app_fixture(monkeypatch)
    warnings = []

    def warning_loader(factory, code, day):
        value = {"strategy_code": code, "status": "READABLE", "positions": [],
                 "closed_today": [closed('REFERENCE60' if '60s' in code else 'REFERENCE30', '08:00:34', pnl='9999')],
                 "daily_pnl": "9999", "recent_decisions": []}
        warnings.append(deepcopy(value))
        return value

    monkeypatch.setattr(cp, '_load_warning_page_ledger', warning_loader)
    def forbidden_snapshot(*args):
        raise AssertionError('MOMPAGE4 must not load the retired five-second ledger')
    monkeypatch.setattr(cp, 'load_completed_today', forbidden_snapshot)
    with TestClient(app) as client:
        app.state.repository.redis = redis
        before = client.get('/api/bots').json()
        response = client.get('/bot/momentum')
        after = client.get('/api/bots').json()
    assert response.status_code == 200
    html = response.text
    assert 'Day total: $+4.96' in html and '1 completed broker trades' in html
    assert '<strong>$+4.96</strong>' in html and html.count('Day total:') == 1
    assert 'Day total: $+9999' not in html and 'Day total: $+19998' not in html
    assert 'Signal reference' not in html and 'REFERENCE' not in html
    assert 'Warning method' not in html and 'REFERENCE60' not in html and 'REFERENCE30' not in html
    titles = ['LIVE (real orders)',
              'Open Positions (LIVE)', 'Order History (LIVE)', 'Decision Tape', 'Failed Actions', 'Listening Status']
    assert [html.index(title) for title in titles] == sorted(html.index(title) for title in titles)
    for removed in ('Snapshot method', 'Snapshot day P&L', 'Snapshot trades', 'TOS Parity', 'Two paper simulations'):
        assert removed not in html
    assert html.count('<h3>Listening Status</h3>') == 1
    assert html.count('<summary>Feed details</summary>') == 1
    assert before['bots'] == after['bots']
    assert len(warnings) == 2
    if destination := os.environ.get('MOMPAGE4_HTML_CAPTURE'):
        Path(destination).write_text(html, encoding='utf-8')


def test_merged_tapes_have_explicit_interval_and_no_foreign_or_snapshot_rows():
    warnings = [{"strategy_code": code, "status": "READABLE", "recent_decisions": [
        {"symbol": symbol, "status": "NO_FILL", "reason": '<bad>', "last_bar_at": stamp}]}
        for code, symbol, stamp in [('momentum_30s_warn', 'THIRTY', '2026-10-15T09:01:00-04:00'),
                                    ('momentum_60s_warn', 'SIXTY', '2026-10-15T09:02:00-04:00')]]
    original = deepcopy(warnings)
    for failures in (False, True):
        html = cp._momentum_page_tape_rows(warnings, failures=failures)
        assert html.index('SIXTY') < html.index('THIRTY')
        assert '<td>30s</td>' in html and '<td>60s</td>' in html
        assert '<bad>' not in html and '&lt;bad&gt;' in html
    assert warnings == original


def test_render_unknowns_o17_warning_and_live_only_orders_do_not_invent_flat_or_zero():
    data = {"bots": [], "recent_orders": [{"strategy_code": 'orb_schwab', "symbol": 'NOT_MOMENTUM'}],
            "row_conversion_warnings": [{"table": 'broker_orders', "row_id": 'bad-order', "error_type": 'ValueError'}]}
    html = cp._render_momentum_page(data, live={"status": "UNKNOWN"},
        warnings=[{"strategy_code": 'momentum_30s_warn', "status": "UNKNOWN"}],
        owner={"status": "UNKNOWN"}, metrics={"status": "UNKNOWN"})
    assert '<strong>Status:</strong> UNKNOWN</div>' in html
    assert 'Live positions UNKNOWN' in html and 'No open positions' not in html
    assert '$+0.00' not in html and 'NOT_MOMENTUM' not in html
    assert 'bad-order' in html and 'Data is incomplete' in html
    assert 'Warning history UNKNOWN' in html


def test_momentum_navigation_and_home_links_are_canonical_without_other_route_changes():
    nav = cp._build_bot_nav_html(['schwab_1m_v2', 'momentum_30s', 'momentum_60s', 'orb_schwab'])
    assert nav.count('href="/bot/momentum"') == 1
    assert 'Momentum 30' not in nav and 'Momentum 60' not in nav
    assert 'href="/bot/orb"' in nav and 'href="/bot/1m-schwab-v2"' in nav
    assert cp._compact_bot_page_url('momentum_30s') == cp._compact_bot_page_url('momentum_60s') == '/bot/momentum'
    assert cp._compact_bot_page_url('orb_schwab') == '/bot/orb'
    assert cp._compact_bot_page_url('schwab_1m_v2') == '/bot/1m-schwab-v2'


@pytest.mark.parametrize('stamp', [None, '', 'not-a-time', '9999-12-31T23:59:59-23:59'])
def test_bad_timestamp_is_always_comparable_and_keeps_other_tape_rows(stamp):
    warning = {'strategy_code': 'momentum_30s_warn', 'recent_decisions': [
        {'symbol': 'GOOD', 'last_bar_at': '2026-10-15T09:01:00-04:00', 'status': 'NO_FILL'},
        None, {'symbol': 'UNSTAMPED', 'last_bar_at': stamp, 'status': 'NO_FILL'}]}
    rows = cp._momentum_page_tape_rows([warning], failures=True, live_orders=[
        {'symbol': 'LIVEFAIL', 'updated_at': stamp, 'status': 'rejected', 'reason': 'real refusal'}])
    assert '<td>GOOD</td>' in rows and '<td>UNSTAMPED</td>' in rows and '<td>LIVEFAIL</td>' in rows
    assert 'UNKNOWN (live order)' in rows
    assert cp._momentum_page_sort_time(stamp).tzinfo is not None


def test_malformed_row_is_isolated_without_known_partial_total_or_lost_good_row():
    warning = {'strategy_code': 'momentum_30s_warn', 'status': 'READABLE', 'daily_pnl': '9999',
               'closed_today': [None, closed('READABLE', '08:00:34')], 'positions': [],
               'recent_decisions': [None, {'symbol': 'TAPEGOOD', 'status': 'NO_FILL'}]}
    live = {'strategy_code': 'momentum_warn_live', 'status': 'READABLE', 'daily_pnl': '9999',
            'closed_today': [[], closed('LIVEGOOD', '08:00:34')], 'positions': None}
    original = deepcopy((warning, live))
    html = cp._render_momentum_page({'bots': [], 'recent_orders': []}, live=live, warnings=[warning],
                                   owner={'status': 'UNKNOWN'}, metrics={'status': 'UNKNOWN'})
    # 06:28 removes reference trades, not readable LIVE siblings or the signal tape.
    assert '<strong>READABLE</strong>' not in html
    assert '<strong>LIVEGOOD</strong>' in html
    assert '<td>TAPEGOOD</td>' in html
    assert 'Warning method' not in html and 'REFERENCE' not in html
    assert 'Live row data is incomplete' in html and 'Warning history UNKNOWN' in html
    assert '<span>Open</span><strong>UNKNOWN</strong>' in html
    assert '<span>Completed trades</span><strong>UNKNOWN</strong>' in html
    assert 'UNKNOWN. No total calculated. Readable rows shown; completed count UNKNOWN.' in html
    assert '<strong>$+9999.00</strong>' not in html and 'Day total: $+9999' not in html
    assert 'No open positions' not in html
    assert (warning, live) == original


@pytest.mark.parametrize('all_on', [False, True])
def test_real_o17_bad_event_isolation_remains_visible_on_canonical_page(monkeypatch, caplog, all_on):
    from tests.unit.test_o1019_17_loader import case
    app, _, ids, _ = case(monkeypatch, all_on=all_on, bad_event='object_shape')
    with TestClient(app) as client:
        response = client.get('/bot/momentum')
        data = client.get('/api/bots').json()
    assert response.status_code == 200
    assert 'Database row warning: skipped 1 malformed rows' in response.text
    assert str(ids['event']) in response.text
    assert data['skipped_database_rows'] == 1
    assert 'DO_NOT_LOG_O17' not in caplog.text


def test_off_admission_keeps_each_live_account_leg_once_and_unknown_market_pnl():
    positions = [{'ticker': 'OWNED', 'account_name': account, 'quantity': '3',
                  'remaining_quantity': '2', 'entry_price': '1.25', 'entry_time': None}
                 for account in ('live:orb', 'live:schwab_1m_v2')]
    live = {'strategy_code': 'momentum_warn_live', 'status': 'READABLE',
            'daily_pnl': '0', 'closed_today': [], 'positions': positions}
    data = {'bots': [], 'recent_orders': [], 'account_positions': [], 'virtual_positions': [
        {'strategy_code': 'momentum_warn_live', 'broker_account_name': row['account_name'],
         'symbol': 'OWNED', 'quantity': '2', 'average_price': '1.25'} for row in positions]}
    before = deepcopy((live, data))
    html = cp._render_momentum_page(data, live=live, warnings=[],
        owner={'status': 'READABLE', 'entry_admission_enabled': False, 'episodes': []}, metrics={'status': 'UNKNOWN'})
    assert '<strong>Status:</strong> OFF</div>' in html
    panel = html.split('<h3>Open Positions (LIVE)</h3>', 1)[1].split('</section>', 1)[0]
    assert panel.count('<strong>OWNED</strong>') == 2
    assert '<td>LIVE Webull</td>' in panel and '<td>LIVE Schwab</td>' in panel
    assert '<strong>$+0.00</strong>' not in panel
    assert (live, data) == before
