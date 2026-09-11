"""Golden replay of the live TNON 2026-09-11 #933 regression.

The fixture is exported from ``strategy_bar_history`` and the running v2 process after the close.
It is not a synthetic indicator sequence. Reverting the ownerless idle-SELL release must make the
fixed half suppress on the same 08:58 ET bar and turn this control red.
"""

from scripts.replay_v2_seed_cap_incident import (
    REQUIRED_LIVE_SETTINGS,
    _et_minute,
    fixture_path,
    load_tape,
    prove_tape,
)


def test_tnon_real_tape_suppresses_before_fix_and_places_after_fix() -> None:
    tape = load_tape(fixture_path("TNON", "2026-09-11"))

    assert tape.symbol == "TNON"
    assert tape.session_date_et == "2026-09-11"
    assert tape.bar_source == "strategy_bar_history:schwab_1m_v2:60"
    assert tape.settings_source.startswith("/proc/")
    assert tape.live_seed_cap_minute_et == "06:51"
    assert len(tape.bars) > 250
    assert {bar.source for bar in tape.bars} <= {"live", "rest"}
    assert {
        key: tape.settings[key] for key in REQUIRED_LIVE_SETTINGS
    } == REQUIRED_LIVE_SETTINGS

    unfixed, fixed = prove_tape(tape)

    assert _et_minute(unfixed.sell_bar_ms + 60_000) == "08:55"
    assert _et_minute(fixed.sell_bar_ms + 60_000) == "08:55"
    assert _et_minute(unfixed.decision_event_ms) == "08:58"
    assert _et_minute(fixed.decision_event_ms) == "08:58"
    assert unfixed.action == "suppressed"
    assert "[V2-RESTING-SLOT-CONSUMED] TNON" in unfixed.marker
    assert fixed.action == "placed"
    assert "[V2-RESTING-EH-ARM] TNON" in fixed.marker
