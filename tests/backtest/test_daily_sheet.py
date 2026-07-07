"""Unit tests for the daily-sheet feed classifier — the logic that makes every v2-qualified name
appear with a reason (no silent absence). Cases are the real 07-06/07-07 names."""
from project_mai_tai.backtest.daily_sheet import classify_v2_feed


def test_full_feed_backtestable():          # CLRO 07-07
    label, ok = classify_v2_feed(335, 6539)
    assert ok is True and label.startswith("full")


def test_sparse_feed_backtestable():        # TDTH 07-06
    label, ok = classify_v2_feed(95, 1709)
    assert ok is True and label.startswith("SPARSE")


def test_skip_no_bars():                    # BYAH 07-06
    label, ok = classify_v2_feed(0, 5)
    assert ok is False and "no Schwab bars" in label


def test_skip_no_ticks():                   # TDTH 07-07
    label, ok = classify_v2_feed(70, 0)
    assert ok is False and "no Schwab ticks" in label


def test_skip_insufficient_bars():          # TDIC 07-06 (7 bars)
    label, ok = classify_v2_feed(7, 200)
    assert ok is False and "insufficient" in label
