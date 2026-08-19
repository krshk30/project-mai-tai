"""P12 — a bracket whose TARGET is at or below the FILL is born already triggered.

⛔⭐⭐ IT CAN ONLY BE CAUGHT AT THE FILL. The bracket is priced off the entry REFERENCE and emitted
WITH the entry, before any fill exists — so at submit there is nothing to compare. The fill is the
one moment the defect is observable at all.

MEASURED, 30 days to 2026-08-19: 7 of 457 bracketed fills (1.5%) had target <= fill. ALL 7 were
MARKET entries whose slippage (1.78–3.63%) outran the +2% target; zero on STOP_LIMIT (152) or
LIMIT (43). SIX exited within 5–39 SECONDS on the target leg (−0.62, −0.34, 0.00, 0.00, 0.00,
−0.42 per cent; median 0.00) — the entry converted into an immediate scratch.

⭐ The path that produced them is gone: #674's band cap bounds the fill at level×(1+0.5%) and
0.5% < the 2% target. Market-entry bracketed fills ran 262 up to 08-12, then ZERO. This guard exists
because that is a property of the CURRENT CONFIGURATION, not of the code.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from project_mai_tai.oms import service as svc


def _svc():
    s = svc.OmsRiskService.__new__(svc.OmsRiskService)
    s.logger = logging.getLogger("test-born-triggered")
    return s


def _md(target: str | None = "10.20", bracket: str = "true") -> dict:
    md = {"native_oco_bracket": bracket}
    if target is not None:
        md["bracket_target_price"] = target
    return md


def test_target_below_the_fill_is_flagged(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        hit = _svc()._check_bracket_born_triggered(
            symbol="FCUV",
            broker_account_name="live:schwab_1m_v2",
            metadata=_md("15.76"),
            fill_price=15.9992,
        )
    assert hit is True
    assert "[OCO-TARGET-BELOW-FILL]" in caplog.text
    assert "BORN TRIGGERED" in caplog.text
    assert "FCUV" in caplog.text and "live:schwab_1m_v2" in caplog.text


def test_target_EQUAL_to_the_fill_is_flagged() -> None:
    """⛔ CLRO 08-06 and both HUIZ fills landed EXACTLY on the target and exited at 0.00%.
    A strict `<` would have missed three of the seven."""
    assert (
        _svc()._check_bracket_born_triggered(
            symbol="CLRO", broker_account_name="live:orb", metadata=_md("9.51"), fill_price=9.51
        )
        is True
    )


def test_a_healthy_bracket_is_silent(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        hit = _svc()._check_bracket_born_triggered(
            symbol="TNON",
            broker_account_name="live:schwab_1m_v2",
            metadata=_md("12.35"),
            fill_price=12.17,
        )
    assert hit is False
    assert caplog.text == ""


def test_an_unbracketed_fill_is_not_examined() -> None:
    """The bare Webull mirror has no target at all — §167 counts that one, not this."""
    assert (
        _svc()._check_bracket_born_triggered(
            symbol="XHG",
            broker_account_name="live:orb",
            metadata={"native_oco_bracket": "false"},
            fill_price=2.45,
        )
        is False
    )


def test_a_missing_or_unparseable_target_does_not_crash_the_fill_path() -> None:
    """⛔ This runs ON the fill path. It must never raise — a fill is not the place to fail."""
    s = _svc()
    assert (
        s._check_bracket_born_triggered(
            symbol="A", broker_account_name="x", metadata=_md(None), fill_price=1.0
        )
        is False
    )
    assert (
        s._check_bracket_born_triggered(
            symbol="A", broker_account_name="x", metadata=_md("not-a-number"), fill_price=1.0
        )
        is False
    )
    assert (
        s._check_bracket_born_triggered(
            symbol="A", broker_account_name="x", metadata=_md("0"), fill_price=1.0
        )
        is False
    )


def test_it_fires_for_the_SCHWAB_primary_too() -> None:
    """⛔⭐ NO BROKER SCOPE. A target below the fill is an instant-loss exit at either venue, so the
    rule does not differ by broker — and adding a scope where it does not differ is its own defect
    (§164). FCUV 07-31 was the Schwab primary."""
    assert (
        _svc()._check_bracket_born_triggered(
            symbol="FCUV",
            broker_account_name="live:schwab_1m_v2",
            metadata=_md("15.76"),
            fill_price=15.9992,
        )
        is True
    )


def test_the_count_is_per_ET_session_and_resets(monkeypatch) -> None:
    s = _svc()
    monkeypatch.setattr(svc, "utcnow", lambda: datetime(2026, 8, 19, 18, 0, tzinfo=UTC))
    s._check_bracket_born_triggered(
        symbol="A", broker_account_name="x", metadata=_md("1.0"), fill_price=2.0
    )
    s._check_bracket_born_triggered(
        symbol="B", broker_account_name="x", metadata=_md("1.0"), fill_price=2.0
    )
    assert s._born_triggered_count == 2
    monkeypatch.setattr(svc, "utcnow", lambda: datetime(2026, 8, 20, 18, 0, tzinfo=UTC))
    s._check_bracket_born_triggered(
        symbol="C", broker_account_name="x", metadata=_md("1.0"), fill_price=2.0
    )
    assert s._born_triggered_count == 1, "a new ET session must restart the count"


def test_the_session_date_is_on_the_line(caplog, monkeypatch) -> None:
    monkeypatch.setattr(svc, "utcnow", lambda: datetime(2026, 8, 19, 18, 0, tzinfo=UTC))
    with caplog.at_level(logging.WARNING):
        _svc()._check_bracket_born_triggered(
            symbol="A", broker_account_name="x", metadata=_md("1.0"), fill_price=2.0
        )
    assert "2026-08-19" in caplog.text


def test_it_is_a_WARNING(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        _svc()._check_bracket_born_triggered(
            symbol="A", broker_account_name="x", metadata=_md("1.0"), fill_price=2.0
        )
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_the_check_runs_at_the_fill_not_at_submit() -> None:
    """⛔ Placed on the managed-open fill path — at submit the fill price does not exist yet."""
    import inspect

    whole = inspect.getsource(svc)
    idx = whole.index("[OMS-V2-MANAGED-OPEN]")
    assert "_check_bracket_born_triggered" in whole[idx : idx + 800]
