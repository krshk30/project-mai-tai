"""Controlled page fixtures, not retained live trades or installation proof."""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from html.parser import HTMLParser

from fastapi.testclient import TestClient
import pytest

from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_mompage1 import make_app
from tests.unit.test_momwarn1_warning_page_metrics import MetricsRedis, entry, sample


NOW = int(datetime(2026, 10, 14, 13, 7, tzinfo=UTC).timestamp() * 1e9)


def closed(ticker, filled, *, pnl="28.80", detected=None):
    return dict(ticker=ticker, symbol=ticker, quantity=50, entry_price="1.00",
                exit_price="1.05", entry_time=f"2026-10-14T{filled}-04:00",
                exit_time="2026-10-14T08:02:00-04:00", pnl=pnl, pnl_pct="5",
                exit_reason="TARGET", detected_at=detected)


def feed(**changes):
    return sample(observed_ns=NOW, coverage_status="provider_event_observed",
                  A_subscription_requested_ns=NOW - 3_600_000_000_000,
                  watchdog_mode="log_only", **changes)


def page(monkeypatch, route, *, snapshot=None, warning=None, metrics=None, enabled=True):
    snapshot = snapshot if snapshot is not None else [closed("SCNI", "08:01:23", pnl="28.80")]
    warning = warning if warning is not None else [closed("SCNI", "08:00:34", pnl="49.40", detected="08:00:30")]
    app, _ = make_app(monkeypatch, records=False)
    monkeypatch.setattr(cp, "utcnow", lambda: datetime.fromtimestamp(NOW / 1e9, UTC))
    monkeypatch.setattr(cp.time, "time_ns", lambda: NOW)
    monkeypatch.setattr(Settings, "market_data_momentum_warning_enabled", property(lambda self: enabled), raising=False)

    def ledger(rows):
        if isinstance(rows, Exception):
            raise rows
        return rows, sum((Decimal(row["pnl"]) for row in rows), Decimal("0"))

    monkeypatch.setattr(cp, "load_completed_today", lambda *args: ledger(snapshot))
    # Historical fixture parameter `snapshot` now supplies the 60s reference;
    # no snapshot-method loader is used by the canonical page.
    def warning_ledger(factory, code, day):
        rows, total = ledger(snapshot if code == "momentum_60s_warn" else warning)
        return {"strategy_code": code, "status": "READABLE", "closed_today": rows,
                "daily_pnl": str(total), "positions": [], "recent_decisions": []}
    monkeypatch.setattr(cp, "_load_warning_page_ledger", warning_ledger)
    client = MetricsRedis([entry(metrics or feed())])
    with TestClient(app) as http:
        app.state.repository.redis = client
        result = http.get(route)
    assert result.status_code == 200
    return result.text, client


class LedgerTables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.current = None
        self.cell = None

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
            if self.current and self.current[:3] == ["Ticker", "Path", "Qty"]:
                self.tables.append(self.current)
            self.current = None


@pytest.mark.parametrize("route", ["/bot/momentum-30", "/bot/momentum-60"])
def test_reference_ledgers_restored_but_never_enter_live_total(monkeypatch, route):
    html, client = page(monkeypatch, route)
    assert "LIVE (real orders)" in html
    assert "Warning method 60 s" in html and "Warning method 30 s" in html
    assert html.count('>REFERENCE</span>') == 2
    assert "Snapshot method" not in html
    assert "Day total: $+49.40" not in html and "Day total: $+28.80" not in html
    assert "Day total: $+78.20" not in html
    assert html.count('<td><strong>SCNI</strong>') == 2
    assert 'Signal reference' in html
    assert html.count('<td style="text-align:right">50</td>') == 2
    assert "caught 49s" not in html
    assert "caught 53s" not in html
    parser = LedgerTables()
    parser.feed(html)
    columns = ["Ticker", "Path", "Qty", "Entry Time", "Entry", "Exit Time", "Exit", "P&L", "Exit Summary"]
    assert parser.tables == [columns, columns, columns]
    assert '<td style="white-space:nowrap;">08:00:34 AM ET</td>' in html
    assert '<td style="white-space:nowrap;">08:01:23 AM ET</td>' in html
    assert html.count('<td style="white-space:nowrap;">08:02:00 AM ET</td>') == 2
    assert '2026-10-14 08:00:34 AM ET' not in html
    assert '$+49.40' in html and '$+28.80' in html
    assert "Warning feed: ON since 08:07 ET" in html
    assert "bars in/out 1811/1806" in html
    assert "delay max 134.5 ms" not in html
    assert any(f"delay max {value} ms" in html for value in (134, 135))
    assert '<details class="panel-copy warning-feed-details"><summary>Feed details</summary>' in html
    assert "<details open" not in html and "existing safety auto-revert: YES" not in html
    assert "Paper bot &mdash; no broker orders." not in html
    assert "Broker Qty<br>Current Price" in html  # ONE shared ATR-style open table.
    assert html.count('<h3>Open Positions') == 1 and 'No open positions' in html
    assert len(client.metric_calls) == 1


@pytest.mark.parametrize("delta,expected", [(49, "earlier"), (-49, "later")])
def test_comparison_is_signed_and_uses_closest_unambiguous_same_day_entry(delta, expected):
    row = closed("SCNI", "08:00:34" if delta > 0 else "08:01:23")
    nearest = closed("SCNI", "08:01:23" if delta > 0 else "08:00:34")
    far = closed("SCNI", "12:00:00")
    assert cp._momentum_entry_comparison(row, [far, nearest], "Warning") == (
        f"Warning caught 49s {expected} (filled entry times)")


@pytest.mark.parametrize("other", [[], [closed("OTHER", "08:01:23")],
    [{**closed("SCNI", "08:01:23"), "entry_time": "2026-10-13T08:01:23-04:00"}],
    [{**closed("SCNI", "08:01:23"), "entry_time": "08:01:23 AM ET"}],
])
def test_no_fabricated_comparison_from_missing_other_ticker_day_or_date(other):
    assert cp._momentum_entry_comparison(closed("SCNI", "08:00:34"), other, "Warning") == ""


def test_ambiguous_closest_match_and_multiple_trades_are_not_collapsed():
    row = closed("SCNI", "08:00:34")
    other = [closed("SCNI", "08:01:23"), closed("SCNI", "08:01:23")]
    assert "UNKNOWN: ambiguous" in cp._momentum_entry_comparison(row, other, "Warning")
    ledger = {"closed_today": other, "daily_pnl": "57.60", "status": "READABLE"}
    before = deepcopy(ledger)
    html = cp._render_momentum_ledger(ledger, title="Snapshot", method="Snapshot", other_rows=[])
    assert html.count("<td><strong>SCNI</strong>") == 2 and "Day total: $+57.60" in html
    assert html.count('$+28.80 (+5.0%)') == 2
    assert ledger == before


@pytest.mark.parametrize("pnl,percent,color", [("28.80", "5", "#00c853"), ("-28.80", "-5", "#ff1744")])
def test_carded_styling_preserves_signed_cash_percent_quantity_and_row(pnl, percent, color):
    row = {**closed("SCNI", "08:00:34", pnl=pnl), "pnl_pct": percent, "quantity": 7,
           "exit_reason": "CONTROLLED EXIT"}
    ledger = {"closed_today": [row], "daily_pnl": pnl, "status": "READABLE"}
    before = deepcopy(ledger)
    html = cp._render_momentum_ledger(ledger, title="Warning", method="Warning", other_rows=[])
    assert html.count('<td><strong>SCNI</strong>') == 1
    assert '<td style="text-align:right">7</td>' in html
    assert '<td style="text-align:right">$1.00</td>' in html
    assert '<td style="text-align:right">$1.05</td>' in html
    assert f'color:{color};white-space:nowrap;">${Decimal(pnl):+.2f} ({Decimal(percent):+.1f}%)' in html
    assert f'Day total: ${Decimal(pnl):+.2f}' in html and '1 completed paper trades' in html
    assert '<td style="white-space:nowrap;">08:00:34 AM ET</td>' in html
    assert '<td style="white-space:nowrap;">08:02:00 AM ET</td>' in html
    assert 'CONTROLLED EXIT</td>' in html
    assert ledger == before


def test_carded_ambiguity_note_does_not_choose_or_merge_comparison_rows():
    row = closed("SCNI", "08:00:34")
    others = [closed("SCNI", "08:01:23"), closed("SCNI", "08:01:23")]
    ledger = {"closed_today": [row], "daily_pnl": "28.80", "status": "READABLE"}
    before = deepcopy((ledger, others))
    html = cp._render_momentum_ledger(ledger, title="Warning", method="Warning", other_rows=others)
    assert 'Entry comparison UNKNOWN: ambiguous same-day ticker match' in html
    assert 'caught 49s' not in html
    assert html.count('<td><strong>SCNI</strong>') == 1
    assert 'Day total: $+28.80' in html and '$+28.80 (+5.0%)' in html
    assert (ledger, others) == before


@pytest.mark.parametrize("route", ["/bot/momentum-30", "/bot/momentum-60"])
@pytest.mark.parametrize("ledger", ["snapshot", "warning"])
def test_unreadable_ledger_total_unknown_other_total_unchanged(monkeypatch, route, ledger):
    kwargs = {ledger: OSError("CONTROLLED unreadable")}
    html, _ = page(monkeypatch, route, **kwargs)
    assert "Warning history UNKNOWN" in html
    assert "Signal reference ledger unreadable: UNKNOWN" in html
    assert "Day total: $+0.00 &middot; 0 completed broker trades" in html
    assert html.count('Day total:') == 1
    assert ("$+49.40" if ledger == "snapshot" else "$+28.80") in html
    assert "Warning feed: ON since 08:07 ET" in html


def test_readable_empty_ledger_can_zero_without_suppressing_other_method(monkeypatch):
    html, _ = page(monkeypatch, "/bot/momentum-30", snapshot=[], warning=[])
    assert html.count("Day total: $+0.00") == 1
    assert html.count("No completed positions") == 3
    assert html.count('0 completed references') == 2


@pytest.mark.parametrize("observed", [NOW - 20_000_000_001, NOW + 1])
def test_stale_future_metrics_are_unknown_not_on_or_zero(monkeypatch, observed):
    metrics = feed()
    metrics["observed_ns"] = observed
    html, _ = page(monkeypatch, "/bot/momentum-60", metrics=metrics)
    assert "Warning feed: UNKNOWN" in html and "Warning feed: ON" not in html
    assert "bars in/out UNKNOWN/UNKNOWN" in html and "delay max UNKNOWN ms" in html


@pytest.mark.parametrize("coverage,reason", [("off_by_window", "window closed"),
    ("inactive", "not subscribed"), ("reverted", "controlled-revert")])
def test_current_off_evidence_reports_actual_reason(monkeypatch, coverage, reason):
    metrics = feed()
    metrics.update(coverage_status=coverage, watchdog_revert_reason="controlled-revert")
    html, _ = page(monkeypatch, "/bot/momentum-30", metrics=metrics)
    assert f"Warning feed: OFF &mdash; {reason}" in html


def test_missing_request_timestamp_unknown_not_hardcoded_since(monkeypatch):
    metrics = feed()
    metrics.pop("A_subscription_requested_ns")
    html, _ = page(monkeypatch, "/bot/momentum-30", metrics=metrics)
    assert "Warning feed: ON since UNKNOWN ET" in html and "since 07:52" not in html


def test_reference_history_remains_when_metrics_disabled(monkeypatch):
    html, client = page(monkeypatch, "/bot/momentum-30", enabled=False)
    assert not client.metric_calls and "Warning feed: UNKNOWN" in html
    assert "$+49.40" in html and "$+28.80" in html
    assert html.count('Day total:') == 1
