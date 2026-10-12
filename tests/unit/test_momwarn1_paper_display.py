from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

from project_mai_tai.db.models import MomentumPaperEvent
from project_mai_tai import momentum_page_history as history
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_mompage1 import make_app


def add_warning(factory, code="momentum_30s_warn"):
    with factory.begin() as session:
        session.add(MomentumPaperEvent(
            event_key=code + ":one:FINAL", logical_id=code + ":one", strategy_code=code,
            event_type="FINAL", session_date=date(2026, 10, 7), symbol="WARN",
            observed_at=datetime(2026, 10, 7, 12, 52, tzinfo=UTC),
            payload={"fill": {"price": "10", "sip_ts_ms": 1791377410000},
                     "exit": {"price": "10.50", "sip_ts_ms": 1791377420000},
                     "quantity": 50, "exit_reason": "TARGET"},
        ))


@pytest.mark.parametrize("code", ["momentum_30s_warn", "momentum_60s_warn"])
def test_warning_reader_is_separate_and_snapshot_totals_identical(monkeypatch, code):
    _, factory = make_app(monkeypatch)
    before = history.load_completed_today(factory, "momentum_30s", date(2026, 10, 7))
    add_warning(factory, code)
    assert history.load_completed_today(factory, "momentum_30s", date(2026, 10, 7)) == before
    rows, pnl = history.load_warning_completed_today(factory, code, date(2026, 10, 7))
    assert len(rows) == 1 and rows[0]["ticker"] == "WARN" and pnl == Decimal("25")
    other = "momentum_60s_warn" if code == "momentum_30s_warn" else "momentum_30s_warn"
    assert history.load_warning_completed_today(factory, other, date(2026, 10, 7)) == ([], Decimal("0"))


@pytest.mark.parametrize("code", ["momentum_30s", "momentum_60s", "orb", "arbitrary"])
def test_warning_reader_rejects_every_other_ledger(monkeypatch, code):
    _, factory = make_app(monkeypatch)
    with pytest.raises(ValueError, match="labelled warning"):
        history.load_warning_completed_today(factory, code, date(2026, 10, 7))


def test_snapshot_reader_still_refuses_warning_codes(monkeypatch):
    _, factory = make_app(monkeypatch)
    with pytest.raises(ValueError, match="two Momentum"):
        history.load_completed_today(factory, "momentum_30s_warn", date(2026, 10, 7))


def test_panel_does_not_mutate_input_or_merge_totals():
    warning = {"strategy_code": "momentum_30s_warn", "status": "READABLE",
               "closed_today": [], "daily_pnl": "25"}
    before = deepcopy(warning)
    panel = cp._render_momentum_warning_panel(warning)
    assert warning == before
    assert "Separate Paper Ledger" in panel and "Never included in the Snapshot Momentum totals" in panel
    assert "$+25.00" in panel


def test_unreadable_panel_never_labels_unknown_as_zero():
    panel = cp._render_momentum_warning_panel({"strategy_code": "momentum_30s_warn", "status": "UNKNOWN"})
    assert "UNKNOWN" in panel and "No total calculated" in panel
    assert "$+0.00" not in panel and "0 completed" not in panel


@pytest.mark.parametrize("value", [None, {}, {"strategy_code": "orb", "status": "READABLE"}])
def test_panel_rejects_missing_or_foreign_ledger(value):
    assert cp._render_momentum_warning_panel(value) == ""


def test_warning_page_row_never_changes_legacy_pnl(monkeypatch):
    monkeypatch.setattr(Settings, "market_data_momentum_warning_enabled", property(lambda self: True), raising=False)
    app, factory = make_app(monkeypatch)
    add_warning(factory)
    rendered = []
    original = cp._render_bot_detail_page
    def capture(data, strategy_code, **kwargs):
        rendered.append(deepcopy(cp._find_bot_view(data, strategy_code)))
        return original(data, strategy_code, **kwargs)
    monkeypatch.setattr(cp, "_render_bot_detail_page", capture)
    with TestClient(app) as client:
        page = client.get("/bot/momentum-30")
    assert page.status_code == 200
    assert "$+31.80" in page.text and "$+25.00" in page.text
    assert "Separate Paper Ledger" in page.text and "WARN" in page.text
    assert "Frozen rule:</strong> +20%" in page.text
    assert rendered[0]["daily_pnl"] == float(Decimal("31.8044"))
    assert len(rendered[0]["closed_today"]) == 5


def test_warning_read_failure_preserves_snapshot_page(monkeypatch):
    monkeypatch.setattr(Settings, "market_data_momentum_warning_enabled", property(lambda self: True), raising=False)
    app, _ = make_app(monkeypatch)
    def unavailable(*args):
        raise RuntimeError("controlled missing warning read")
    monkeypatch.setattr(cp, "load_warning_completed_today", unavailable)
    with TestClient(app) as client:
        page = client.get("/bot/momentum-30")
    assert page.status_code == 200
    assert "$+31.80" in page.text and "Warning ledger unreadable: UNKNOWN" in page.text
