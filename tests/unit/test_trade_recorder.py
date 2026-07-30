"""The recorder's what-if maths, and the honesty guards around it.

⭐ WHY THE RECORDER EXISTS: reconstructing 2026-07-29 from the DB gave three different answers, two
wrong — FIFO pairing invented a -8.40% trade, and coid pairing exposed 5 exits dated before their own
entry. Attribution must be CAPTURED, not inferred. These tests cover the part that still involves
judgement: the what-if exit maths, and whether it admits its own uncertainty.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ops.health.trade_recorder import TARGET_PCT, TIER_PRICE, analyse

T0 = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


class _Bar:
    def __init__(self, i, high, low):
        self.bar_time = T0 + timedelta(minutes=i)
        self.high_price = high
        self.low_price = low


def test_mfe_and_mae_come_from_the_bar_path() -> None:
    bars = [_Bar(0, 5.10, 4.95), _Bar(1, 5.30, 5.00), _Bar(2, 5.05, 4.70)]
    out = analyse(5.00, bars, actual_ret=1.0)
    assert out["mfe_pct"] == 6.0          # 5.30 high
    assert out["mae_pct"] == -6.0         # 4.70 low
    assert out["n_bars"] == 3


def test_floor_rule_never_books_less_than_the_target_once_touched() -> None:
    """The operator's rule: reach +2%, then guarantee +2% and trail for more."""
    bars = [_Bar(0, 5.10, 4.99), _Bar(1, 5.02, 4.60)]      # touches +2%, then fades hard
    out = analyse(5.00, bars, actual_ret=-8.0)
    assert out["touched_target"] is True
    for w in (2, 3, 5):
        assert out["whatif_floor2_trail%d_pct" % w] >= TARGET_PCT


def test_floor_rule_does_NOT_engage_if_the_target_was_never_touched() -> None:
    """⛔ It must not invent a +2% on a trade that never got there — that would flatter every loser."""
    bars = [_Bar(0, 5.05, 4.60)]                            # never reaches 5.10
    out = analyse(5.00, bars, actual_ret=-8.0)
    assert out["touched_target"] is False
    for w in (2, 3, 5):
        assert out["whatif_floor2_trail%d_pct" % w] == -8.0  # falls back to what really happened


def test_trail_captures_upside_beyond_the_target() -> None:
    """A real runner should beat a flat +2%, otherwise the trail is pointless."""
    bars = [_Bar(0, 5.10, 5.00), _Bar(1, 6.00, 5.50)]       # MFE +20%
    out = analyse(5.00, bars, actual_ret=2.0)
    assert out["whatif_floor2_trail2_pct"] == 18.0
    assert out["whatif_floor2_trail5_pct"] == 15.0


def test_tier_boundary_is_the_3_dollar_price() -> None:
    """Below $3 keeps the wide stop; at/above $3 uses the tight one."""
    deep = [_Bar(0, 2.05, 1.80)]                            # -10% on a $2 stock
    assert analyse(2.00, deep, actual_ret=-10.0)["tier_used_pct"] == 5.0
    assert analyse(TIER_PRICE, deep, actual_ret=-10.0)["tier_used_pct"] == 3.0


def test_tier_stop_converts_a_winner_when_the_stop_comes_first() -> None:
    """The known failure mode: a tighter stop intercepts a trade that later won."""
    bars = [_Bar(0, 5.02, 4.80), _Bar(1, 5.20, 5.05)]       # -4% dip BEFORE the +2%
    out = analyse(5.00, bars, actual_ret=+2.0)
    assert out["whatif_tier_stop_pct"] == -3.0


def test_tier_stop_leaves_a_clean_winner_alone() -> None:
    bars = [_Bar(0, 5.15, 4.98), _Bar(1, 5.20, 5.10)]       # never touches -3%
    assert analyse(5.00, bars, actual_ret=+2.0)["whatif_tier_stop_pct"] == 2.0


def test_it_flags_its_own_intrabar_ambiguity() -> None:
    """⛔ THE HONESTY GUARD. When a stop and a target share ONE 1-minute bar their order is
    unknowable, and a what-if that hides that is worse than no what-if at all."""
    both = [_Bar(0, 5.20, 4.80)]                            # +4% high AND -4% low, same bar
    assert analyse(5.00, both, actual_ret=+2.0)["intrabar_ambiguous"] is True
    clean = [_Bar(0, 5.20, 5.00)]
    assert analyse(5.00, clean, actual_ret=+2.0)["intrabar_ambiguous"] is False


def test_no_bars_is_survivable() -> None:
    """A sparse-feed symbol must not crash the recorder mid-session."""
    out = analyse(5.00, [], actual_ret=1.5)
    assert out["n_bars"] == 0 and out["touched_target"] is False
    assert out["whatif_floor2_trail3_pct"] == 1.5
