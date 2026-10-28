"""KEEPWATCH1 card (operator 10-22): a dropped stock keeps its bars and line, but no new buys while off the list."""
from project_mai_tai.strategy_core.keepwatch import KeepWatchSession

ANCHOR = 1_793_174_400_000  # 2026-10-28 04:00 ET


def test_live_policy_dropped_name_is_watched_but_not_buyable():
    session = KeepWatchSession()
    session.update(ANCHOR, {"ELPW", "GCTK"}, ANCHOR + 60_000, {}, retain_entries=False)
    assert session.allows_entry("ELPW") and session.allows_entry("GCTK")
    # GCTK 07:24 / ELPW 07:37: the scanner drops both (confirmed=0).
    session.update(ANCHOR, set(), ANCHOR + 120_000, {}, retain_entries=False)
    assert not session.allows_entry("ELPW") and not session.allows_entry("GCTK")
    assert session.retained(ANCHOR) == {"ELPW", "GCTK"}  # still watched all day


def test_readd_restores_buying_after_a_sell_flip_as_before():
    session = KeepWatchSession()
    session.update(ANCHOR, {"GCTK"}, ANCHOR + 60_000, {}, retain_entries=False)
    session.update(ANCHOR, set(), ANCHOR + 120_000, {}, retain_entries=False)
    session.update(ANCHOR, {"GCTK"}, ANCHOR + 180_000, {"GCTK": "long"}, retain_entries=False)
    assert not session.allows_entry("GCTK")  # waits for the next SELL flip (unchanged rule)
    session.observe_line("GCTK", "short", ANCHOR + 240_000)
    assert session.allows_entry("GCTK")


def test_service_passes_the_card_policy():
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / "src/project_mai_tai/services/schwab_1m_v2_bot.py"
    text = source.read_text()
    assert "retain_entries=False, excluded=frozenset(protected_exclude | ineligible_exclude)" in text
    assert "retain_entries=True, excluded=" not in text
