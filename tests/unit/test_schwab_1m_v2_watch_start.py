"""A flip only counts if we were WATCHING when it happened.

⭐⭐ WHY THIS EXISTS (2026-07-30, live money). `_cap_reconstructed_segment` screened armed segments
against one global `_boot_ms`. That is the right idea against the wrong clock: it screens segments
older than the PROCESS and says nothing about a symbol the scanner promoted mid-session. When a new
symbol is confirmed, its REST warmup replays history and re-arms from flips that happened before we
were watching — and those passed, because they are newer than boot.

    APLX  flip bar 09:16 ET | joined the watchlist 09:38 ET | bought 10:00 at +23.7% | stopped out
    SNDG  flip bar 09:23 ET | joined the watchlist 09:34 ET | bought 10:00 at +18.9% | stopped out

v2 had booted 07-28 19:05 ET, so both flips were two days after boot and ~20 minutes before we
started watching. Operator's rule: a NEWLY CONFIRMED stock must wait for a fresh flip; stocks held
since 07:00 are exempt because we saw their flips happen.
"""
from __future__ import annotations

from project_mai_tai.services import schwab_1m_v2_bot as botmod


class _State:
    def __init__(self, arm_ts: int) -> None:
        self.cw_armed = True
        self.cw_arm_bar_ts = arm_ts
        self.cw_entries_this_flip = 0


class _Strat:
    _cw_armed_segment_safety_enabled = True
    _cw_v2_max_entries_per_flip = 2

    def __init__(self, boot_ms: int, state: _State) -> None:
        self._boot_ms = boot_ms
        self._state = state

    def watchlist_state(self, symbol: str) -> _State:
        return self._state


def _bot(boot_ms: int, state: _State, watch_start: dict[str, int]):
    """A bare instance — we exercise the two methods directly, not the whole service."""
    bot = object.__new__(botmod.SchwabV2BotService)
    bot.strategy = _Strat(boot_ms, state)
    bot._watch_start_ms = watch_start
    return bot


BOOT = 1_000_000
CAPPED = 2          # == _cw_v2_max_entries_per_flip


def test_a_flip_from_BEFORE_the_symbol_joined_is_disqualified() -> None:
    """THE REGRESSION. APLX's shape: flip long after boot, but before we were watching."""
    st = _State(arm_ts=BOOT + 500)                     # well after boot...
    bot = _bot(BOOT, st, {"APLX": BOOT + 900})         # ...but before the join
    bot._cap_reconstructed_segment("APLX", stage="rest-warmup")
    assert st.cw_entries_this_flip == CAPPED, "a pre-watch flip must not be enterable"


def test_a_flip_AFTER_the_symbol_joined_is_kept() -> None:
    """The whole point — a genuinely fresh flip on a newly promoted symbol still trades."""
    st = _State(arm_ts=BOOT + 5_000)
    bot = _bot(BOOT, st, {"APLX": BOOT + 900})
    bot._cap_reconstructed_segment("APLX", stage="rest-warmup")
    assert st.cw_entries_this_flip == 0, "a fresh post-join flip must survive"


def test_a_symbol_held_since_boot_is_EXEMPT_and_behaves_as_before() -> None:
    """Operator's exemption: symbols watched since 07:00 saw their own flips. No watch-start entry
    means fall back to boot — the pre-2026-07-30 behaviour, unchanged."""
    st = _State(arm_ts=BOOT + 5_000)                   # post-boot live flip
    bot = _bot(BOOT, st, {})                           # never stamped => present since boot
    bot._cap_reconstructed_segment("STKH", stage="db-seed")
    assert st.cw_entries_this_flip == 0


def test_a_pre_boot_flip_is_still_capped_for_a_boot_symbol() -> None:
    """The original P1.3 protection must survive the change — a restart still cannot re-issue the
    per-segment cap on a segment reconstructed from before the process existed."""
    st = _State(arm_ts=BOOT - 5_000)
    bot = _bot(BOOT, st, {})
    bot._cap_reconstructed_segment("STKH", stage="db-seed")
    assert st.cw_entries_this_flip == CAPPED


def test_the_boundary_is_INCLUSIVE_because_a_bar_ts_is_the_bar_OPEN() -> None:
    """⛔ Pins `<=`, not `<`. A symbol joining at 09:38:30 was not watching when the 09:38 bar
    OPENED, so that bar's flip is not observable-live. Flipping this to `<` turns it red."""
    st = _State(arm_ts=BOOT + 900)
    bot = _bot(BOOT, st, {"APLX": BOOT + 900})         # arm exactly AT the join instant
    bot._cap_reconstructed_segment("APLX", stage="rest-warmup")
    assert st.cw_entries_this_flip == CAPPED


def test_watch_start_falls_back_to_boot_for_an_unknown_symbol() -> None:
    bot = _bot(BOOT, _State(0), {"OTHER": BOOT + 900})
    assert bot._watch_start_for("APLX") == BOOT
    assert bot._watch_start_for("OTHER") == BOOT + 900


def test_an_already_capped_segment_is_left_alone() -> None:
    """Idempotence — this runs after EVERY replay; it must not stomp a segment twice."""
    st = _State(arm_ts=BOOT - 1)
    st.cw_entries_this_flip = CAPPED
    bot = _bot(BOOT, st, {})
    bot._cap_reconstructed_segment("STKH", stage="rest-warmup")
    assert st.cw_entries_this_flip == CAPPED


def test_an_unarmed_state_is_untouched() -> None:
    st = _State(arm_ts=BOOT - 5_000)
    st.cw_armed = False
    bot = _bot(BOOT, st, {})
    bot._cap_reconstructed_segment("STKH", stage="rest-warmup")
    assert st.cw_entries_this_flip == 0


# ------------------------------------------------------------------ REACHABILITY
# ⛔⭐ THE TEST THAT WOULD HAVE CAUGHT THE OTHER THREE. The liquidity floor (#587), the cooldown,
# and the fresh-flip age qualifier were ALL implemented, ALL looked protective, and ALL sat in code
# the live path never reaches. A behavioural test passes happily against a gate nobody calls.
# So: assert the guard is actually WIRED, not merely correct.

def test_the_cap_is_wired_into_every_replay_that_can_arm() -> None:
    """Both replay paths must call it. A new replay path added without this call silently
    reintroduces the 2026-07-30 bug."""
    import inspect

    src = inspect.getsource(botmod.SchwabV2BotService)
    assert src.count("_cap_reconstructed_segment(") >= 3, (
        "expected the definition plus BOTH call sites (db-seed and rest-warmup)"
    )
    assert 'stage="rest-warmup"' in src, "the REST warmup replay must cap"
    assert 'stage="db-seed"' in src, "the DB seed replay must cap"


def test_the_cap_consults_the_per_symbol_watch_start_not_boot() -> None:
    """Pins the CLOCK, not just the outcome. Reverting the body to `strat._boot_ms` leaves every
    behavioural test above passing for boot symbols — this is what catches it."""
    import inspect

    src = inspect.getsource(botmod.SchwabV2BotService._cap_reconstructed_segment)
    assert "_watch_start_for(symbol)" in src
    # The COMPARISON specifically — `_boot_ms` may still appear in the log line for diagnostics,
    # and that is fine; what must never come back is boot as the discriminator.
    assert "<= watch_start" in src, "the cap must compare against the per-symbol watch start"
    assert "< strat._boot_ms" not in src, (
        "reverting the comparison to global boot reintroduces the 2026-07-30 defect"
    )
