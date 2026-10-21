"""Presentation controls; actual retained rows stay outside git by policy."""
from copy import deepcopy
from datetime import UTC, date, datetime
from html.parser import HTMLParser
import json
import os
from pathlib import Path

import pytest

from project_mai_tai.services import control_plane as cp
from project_mai_tai.db.models import MomentumPaperEvent
from tests.unit.test_mompage1 import make_app
from tests.unit.test_mompage2 import closed, page


class Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.current, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.current = []
        if tag == "th" and self.current is not None:
            self.cell = ""

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag == "th" and self.cell is not None:
            self.current.append(self.cell)
            self.cell = None
        if tag == "table" and self.current is not None:
            self.tables.append(self.current)
            self.current = None


def ledger(rows, **changes):
    return {"strategy_code": "momentum_30s", "status": "READABLE", "closed_today": rows,
            "daily_pnl": "28.80", "positions": [], **changes}


def render(value, method="Warning", paper=True, other=None):
    return cp._render_momentum_ledger(value, title=method, method=method, other_rows=other or [], paper=paper)


@pytest.mark.parametrize("route", ["/bot/momentum-30", "/bot/momentum-60"])
def test_shared_atr_columns_badges_separate_totals_and_short_times(monkeypatch, route):
    html, _ = page(monkeypatch, route)
    tables = Tables()
    tables.feed(html)
    completed = [row for row in tables.tables if row[:3] == ["Ticker", "Path", "Qty"]]
    columns = ["Ticker", "Path", "Qty", "Entry Time", "Entry", "Exit Time", "Exit", "P&L", "Exit Summary"]
    assert completed == [columns]
    assert '<span class="count accent">REFERENCE</span>' not in html
    assert '<span class="count accent">LIVE</span>' in html
    assert "$+49.40" not in html and "$+28.80" not in html
    assert html.count('Day total:') == 1
    assert "Day total: $+78.20" not in html
    assert 'caught 49s' not in html
    assert 'color:#98a6c8;font-size:10px;' not in html  # Retired snapshot-comparison note.
    assert "08:00:34 AM ET" not in html and "2026-10-14 08:00:34 AM ET" not in html
    assert "Broker Qty<br>Current Price" in html
    assert html.count('<h3>Open Positions') == 1
    assert 'Open positions UNKNOWN' not in html


@pytest.mark.parametrize("route", ["/bot/momentum-30", "/bot/momentum-60"])
@pytest.mark.parametrize("enabled", [True, False])
def test_live_note_plain_words_actual_flag_and_no_vwap(monkeypatch, route, enabled):
    from html import unescape
    from unittest.mock import AsyncMock

    build_app = cp.build_app

    def configured_app(*args, **kwargs):
        kwargs["settings"].momentum_live_enabled = enabled
        return build_app(*args, **kwargs)

    monkeypatch.setattr(cp, "build_app", configured_app)
    monkeypatch.setattr(cp, '_read_momentum_page_owner', AsyncMock(return_value={
        'status': 'READABLE', 'entry_admission_enabled': enabled, 'episodes': []}))
    html, _ = page(monkeypatch, route)
    state = "LIVE" if enabled else "OFF"
    assert f'<strong>Status:</strong> {state}</div>' in html
    assert "P&L uses actual LIVE broker fills only." in unescape(html)
    assert "VWAP" not in html
    assert "$+49.40" not in html and "$+28.80" not in html and html.count('Day total:') == 1


def test_atr_and_momentum_call_same_actual_row_component(monkeypatch):
    row = closed("SCNI", "08:00:34")
    calls = []
    original = cp._render_completed_position_row
    def spy(value, **kwargs):
        calls.append(kwargs)
        return original(value, **kwargs)
    monkeypatch.setattr(cp, "_render_completed_position_row", spy)
    monkeypatch.setattr(cp, "_collect_completed_position_rows", lambda *args: [row])
    cp._build_completed_position_rows({}, [], [])
    render(ledger([row]))
    assert calls == [{}, {"note": "", "short_times": True}]


@pytest.mark.parametrize("pnl,color", [("28.80", "#00c853"), ("-28.80", "#ff1744")])
def test_colour_percent_and_no_input_mutation(pnl, color):
    value = ledger([closed("SCNI", "08:00:34", pnl=pnl)], daily_pnl=pnl)
    before = deepcopy(value)
    html = render(value)
    assert f'color:{color};white-space:nowrap;' in html
    assert f"${float(pnl):+.2f} (+5.0%)" in html
    assert value == before


@pytest.mark.parametrize("value,label", [(".8891", "$0.8891"), ("1.0500", "$1.05"), ("12.3456", "$12.3456"), (None, "UNKNOWN")])
def test_price_label_has_two_to_four_decimals_without_data_change(value, label):
    assert cp._momentum_price_label(value) == label


@pytest.mark.parametrize("account,broker", [("live:schwab", "Schwab"), ("live:orb", "Webull")])
def test_live_path_unknown_time_retains_cash_row(account, broker):
    row = {**closed("SCNI", "08:00:34"), "account_name": account, "entry_time": None, "exit_time": None, "pnl_pct": None}
    value = ledger([row])
    before = deepcopy(value)
    html = render(value, "LIVE", False)
    assert "LIVE " + broker in html
    assert "$+28.80 (UNKNOWN)" in html and "Day total: $+28.80" in html
    assert html.count("UNKNOWN") >= 3
    assert "1970" not in html and "None" not in html
    assert '<strong>SCNI</strong>' in html and value == before


@pytest.mark.parametrize("unknown", [None, "UNKNOWN", "NaN"])
def test_missing_pnl_total_not_fake_zero(unknown):
    row = {**closed("SCNI", "08:00:34"), "pnl": unknown, "pnl_pct": unknown}
    html = render(ledger([row], daily_pnl=unknown), "LIVE", False)
    assert "Day total: UNKNOWN" in html and "UNKNOWN (UNKNOWN)" in html
    assert "$+0.00" not in html
    assert 'color:#888;white-space:nowrap;' in html


def test_empty_and_unreadable_open_are_distinct():
    bot = {**ledger([]), "momentum_warning": ledger([])}
    assert "No open positions" in cp._render_momentum_open_positions(bot)
    bot["momentum_warning"]["status"] = "UNKNOWN"
    bot["momentum_snapshot_status"] = "UNKNOWN"
    html = cp._render_momentum_open_positions(bot)
    assert html.count("open positions unreadable") == 1
    assert 'No open positions' not in html and 'Open positions UNKNOWN' not in html
    assert 'Open Positions' not in render(ledger([]))
    assert "ledger unreadable: UNKNOWN" in render(ledger([], status="UNKNOWN"))


def test_repeated_symbols_are_not_coalesced():
    rows = [closed("SCNI", "08:00:34"), closed("SCNI", "08:00:35")]
    assert render(ledger(rows)).count("<strong>SCNI</strong>") == 2


def test_open_live_missing_market_price_is_unknown_not_zero_pnl():
    value = ledger([], positions=[{"ticker": "SCNI", "quantity": "3", "remaining_quantity": "2", "entry_price": "1", "entry_time": None}])
    before = deepcopy(value)
    html = cp._render_momentum_open_positions({**ledger([]), "momentum_warning": ledger([]),
                                              "momentum_live": {**value, "entry_admission_enabled": True}})
    assert 'color:#888">UNKNOWN</td>' in html
    assert '<strong>$+0.00</strong>' not in html and 'None' not in html
    assert '<td style="text-align:right">2<br>' in html
    assert value == before


def test_actual_retained_sixteen_warning_six_snapshot(monkeypatch):
    path = os.environ.get("MOMPAGE3_RETAINED_INPUT")
    if not path:
        pytest.skip("Actual retained row packet is local-only, not committed evidence")
    raw = json.loads(Path(path).read_text())
    warning = raw["ledgers"]["momentum_30s_warn"]
    snapshot = raw["ledgers"]["momentum_30s"]
    assert warning["row_count"] == 16 and snapshot["row_count"] == 6
    before = deepcopy(raw)
    html, _ = page(monkeypatch, "/bot/momentum-30", warning=warning["closed_today"], snapshot=[])
    assert "16 completed references" in html and "6 completed paper trades" not in html
    assert html.count('<td><strong>') == 16
    assert f'Day total: ${float(warning["daily_pnl"]):+.2f}' not in html
    assert f'Day total: ${float(snapshot["daily_pnl"]):+.2f}' not in html
    assert raw == before


def add_event(session, logical_id, event_type, *, payload=None, day=date(2026, 10, 14), code="momentum_30s_warn"):
    session.add(MomentumPaperEvent(event_key=f"{logical_id}:{event_type}", logical_id=logical_id,
                                  strategy_code=code, event_type=event_type, symbol="SCNI",
                                  session_date=day, observed_at=datetime(2026, 10, 14, 12, tzinfo=UTC),
                                  payload=payload or {}))


@pytest.mark.parametrize("terminal", [None, "EXITED", "FINAL"])
def test_warning_open_uses_same_bounded_history_read(monkeypatch, terminal):
    from sqlalchemy import event
    _, factory = make_app(monkeypatch, records=False)
    fill = {"quantity": 3, "fill": {"price": "1.25", "sip_ts_ms": 1791979230733}}
    with factory() as session:
        add_event(session, "controlled-open", "FILLED", payload=fill)
        if terminal:
            add_event(session, "controlled-open", terminal)
        add_event(session, "prior-day", "FILLED", payload=fill, day=date(2026, 10, 13))
        add_event(session, "other-method", "FILLED", payload=fill, code="momentum_60s_warn")
        session.commit()
    reads = []
    event.listen(factory.kw["bind"], "before_cursor_execute", lambda connection, cursor, statement, *args: reads.append(statement))
    value = cp._load_warning_page_ledger(factory, "momentum_30s_warn", date(2026, 10, 14))
    assert len(reads) == 1 and "LIMIT" in reads[0]
    assert value["closed_today"] == [] and value["daily_pnl"] == "0"
    assert len(value["positions"]) == (0 if terminal else 1)
    html = cp._render_momentum_open_positions({**ledger([]), "momentum_warning": value})
    assert ("No open positions" in html) == bool(terminal)
    if not terminal:
        assert '<strong>SCNI</strong>' in html and '<td>Warning</td>' in html


def test_warning_bound_refuses_partial_open_snapshot(monkeypatch):
    from project_mai_tai import momentum_page_history as history
    _, factory = make_app(monkeypatch, records=False)
    monkeypatch.setattr(history, "MAX_TERMINAL_ROWS", 1)
    with factory() as session:
        for logical_id in ("one", "two"):
            add_event(session, logical_id, "FILLED")
        session.commit()
    with pytest.raises(RuntimeError, match="bound exceeded"):
        cp._load_warning_page_ledger(factory, "momentum_30s_warn", date(2026, 10, 14))


def test_warning_open_endpoint_loads_offloop_and_empty_after_final(monkeypatch):
    from fastapi.testclient import TestClient
    import threading
    app, factory = make_app(monkeypatch, records=False)
    monkeypatch.setattr(cp, "utcnow", lambda: datetime(2026, 10, 14, 13, tzinfo=UTC))
    with factory() as session:
        add_event(session, "endpoint-open", "FILLED", payload={"quantity": 3, "fill": {"price": "1.25"}})
        session.commit()
    original = cp._load_warning_page_ledger
    threads = []
    def capture(*args):
        try:
            asyncio_loop = __import__("asyncio").get_running_loop()
        except RuntimeError:
            asyncio_loop = None
        threads.append((threading.current_thread().name, asyncio_loop))
        return original(*args)
    monkeypatch.setattr(cp, "_load_warning_page_ledger", capture)
    with TestClient(app) as client:
        html = client.get('/bot/momentum-30').text
        assert html.count('<h3>Open Positions') == 1
        open_panel = html.split('<h3>Open Positions (LIVE)</h3>', 1)[1].split('</section>', 1)[0]
        assert '<strong>SCNI</strong>' not in open_panel and 'No open positions' in open_panel
        assert '<td>SCNI</td>' in html  # Warning evidence remains in the merged tape.
        assert 'open positions unreadable' not in html and '1970' not in html
        with factory() as session:
            add_event(session, "endpoint-open", "FINAL")
            session.commit()
        html = client.get('/bot/momentum-30').text
        assert 'No open positions' in html and '<td>Warning</td>' not in html
    assert len(threads) == 4 and all(loop is None for _, loop in threads)


def test_combined_paths_preserve_same_symbol_rows_and_live_gate():
    position = {"ticker": "SCNI", "quantity": 3, "entry_price": "1.25"}
    bot = {**ledger([], positions=[position]), "momentum_warning": ledger([], positions=[position]),
           "momentum_live": ledger([], positions=[{**position, "broker": "webull"}], entry_admission_enabled=False)}
    before = deepcopy(bot)
    html = cp._render_momentum_open_positions(bot)
    assert html.count('<strong>SCNI</strong>') == 3
    assert '<td>Warning</td>' in html and '<td>Snapshot</td>' in html and '<td>LIVE Webull</td>' in html
    assert bot == before
    bot["momentum_live"]["entry_admission_enabled"] = True
    html = cp._render_momentum_open_positions(bot)
    assert html.count('<strong>SCNI</strong>') == 3 and '<td>LIVE Webull</td>' in html


def test_off_held_live_position_visible_until_flat_without_data_change():
    live = ledger([], strategy_code="momentum_warn_live", entry_admission_enabled=False,
                  positions=[{"ticker": "SCNI", "quantity": "3", "remaining_quantity": "2",
                              "entry_price": "1.25", "entry_time": None, "account_name": "live:orb"}])
    bot = {**ledger([]), "momentum_warning": ledger([]), "momentum_live": live}
    before = deepcopy(bot)
    html = cp._render_momentum_open_positions(bot)
    assert html.count('<strong>SCNI</strong>') == 1 and '<td>LIVE Webull</td>' in html
    assert '<td style="text-align:right">2<br>' in html
    assert html.count('<h3>Open Positions') == 1 and 'No open positions' not in html
    assert '1970' not in html and bot == before


@pytest.mark.parametrize("positions", [[], [{"ticker": "SCNI", "quantity": "3", "remaining_quantity": "0"}]])
def test_off_flat_live_shows_no_open_positions(positions):
    bot = {**ledger([]), "momentum_warning": ledger([]),
           "momentum_live": ledger([], positions=positions, entry_admission_enabled=False)}
    before = deepcopy(bot)
    html = cp._render_momentum_open_positions(bot)
    assert 'No open positions' in html and '<strong>SCNI</strong>' not in html
    assert '<td>LIVE' not in html and 'open positions unreadable' not in html
    assert html.count('<h3>Open Positions') == 1 and bot == before


@pytest.mark.parametrize("live", [
    {"status": "UNKNOWN", "positions": []},
    {"status": "UNREADABLE", "positions": []},
    {"status": "READABLE", "positions": None},
    {"status": "READABLE"},
])
def test_off_unreadable_live_never_claims_no_open_positions(live):
    bot = {**ledger([]), "momentum_warning": ledger([]),
           "momentum_live": {**live, "entry_admission_enabled": False}}
    before = deepcopy(bot)
    html = cp._render_momentum_open_positions(bot)
    assert html.count('open positions unreadable') == 1
    assert 'No open positions' not in html and '<td>LIVE' not in html
    assert html.count('<h3>Open Positions') == 1 and bot == before


def test_absent_live_ledger_does_not_invent_unreadable_or_held_position():
    html = cp._render_momentum_open_positions({**ledger([]), "momentum_warning": ledger([])})
    assert 'No open positions' in html and 'open positions unreadable' not in html


def test_actual_retained_warning_fill_controlled_open_replay(monkeypatch):
    path = os.environ.get("MOMPAGE3_REPLAY_INPUT")
    if not path:
        pytest.skip("Actual retained payload packet is local-only; controlled open replay, not a live-open claim")
    packet = json.loads(Path(path).read_text())
    original = next(row for signal in packet["signals"] for row in signal["own_paper_transitions"]
                    if row["event_type"] == "FILLED" and row["strategy_code"] == "momentum_30s_warn")
    _, factory = make_app(monkeypatch, records=False)
    with factory() as session:
        session.add(MomentumPaperEvent(**{key: original[key] for key in
                    ("event_key", "logical_id", "strategy_code", "event_type", "symbol", "payload")},
                    session_date=date.fromisoformat(original["session_date"]),
                    observed_at=datetime.fromisoformat(original["observed_at"])))
        session.commit()
    value = cp._load_warning_page_ledger(factory, "momentum_30s_warn", date(2026, 10, 14))
    assert len(value["positions"]) == 1
    assert value["positions"][0]["quantity"] == original["payload"]["quantity"]
    assert value["positions"][0]["entry_price"] == original["payload"]["fill"]["price"]
    html = cp._render_momentum_open_positions({**ledger([]), "momentum_warning": value})
    assert '<td>Warning</td>' in html and '<strong>SCNI</strong>' in html
    assert 'No open positions' not in html and 'open positions unreadable' not in html
