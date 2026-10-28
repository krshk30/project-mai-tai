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


class _FallbackHarness:
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService as _S
    _keepwatch_entry_allowed = _S._keepwatch_entry_allowed

    def __init__(self, *, scanner, watchlist, retained_before):
        from types import SimpleNamespace
        from project_mai_tai.settings import Settings
        self.settings = Settings(_env_file=None)
        self.strategy = SimpleNamespace(_now_ms=lambda: ANCHOR + 3_600_000)
        self._keepwatch = KeepWatchSession()
        self._keepwatch.update(ANCHOR, set(scanner), ANCHOR + 60_000, {}, retain_entries=False)
        self._watchlist = set(watchlist)
        self._entry_watch_symbols = set(scanner)
        self._fallback = {"retained_entry_before": retained_before}

    def _keepwatch_boot_excluded_symbols(self):
        return set()

    def _keepwatch_current_publication(self, symbol):
        return None

    def _line_pg_poll_fallback_current(self, symbol):
        return self._fallback


def test_pg_fallback_off_list_held_or_inflight_name_cannot_buy():
    # Dropped by the scanner but still in _watchlist because it is held / in flight.
    harness = _FallbackHarness(scanner=set(), watchlist={"GCTK"}, retained_before=True)
    assert harness._keepwatch_entry_allowed("GCTK") is False


def test_pg_fallback_on_list_name_still_buys():
    harness = _FallbackHarness(scanner={"ELPW"}, watchlist={"ELPW"}, retained_before=False)
    assert harness._keepwatch_entry_allowed("ELPW") is True
