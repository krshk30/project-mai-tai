"""Schwab-only pre-window anchor controls; no provider or database writes."""
from datetime import timedelta

import pytest

from project_mai_tai.orb_schwab_fill import merge_macd_fill
from tests.unit.test_orbfill1 import CUTOFF, NOW, START, bar, candle, filler


def test_last_schwab_anchor_carries_only_forward_inside_fill_window():
    source = [candle(START - timedelta(minutes=86), 3.99),
              candle(START - timedelta(minutes=85), 4.01),
              candle(START + timedelta(minutes=28), 5)]
    merged = merge_macd_fill([], source, START, CUTOFF)
    assert len(merged) == 58
    assert merged[0].timestamp == START and merged[-1].timestamp == CUTOFF
    assert all(b.close == 4.01 for b in merged[:28])
    assert all(b.close == 5 for b in merged[28:])


def test_exact_0927_excludes_0927_provider_candle():
    now = NOW - timedelta(minutes=1)
    source = [candle(START - timedelta(minutes=86), 3.99),
              candle(START + timedelta(minutes=28), 4), candle(CUTOFF, 900)]
    result = filler(source, clock=lambda: now).supplement("TEST", [], now)
    assert len(result) == 57 and result[-1].timestamp == CUTOFF - timedelta(minutes=1)
    assert result[-1].close == 4 and all(b.close != 900 for b in result)


def test_saved_wins_anchor_duplicate_but_latest_observed_anchor_wins_age():
    saved = [bar(START - timedelta(minutes=2), 3)]
    source = [candle(saved[0].timestamp, 999), candle(START + timedelta(minutes=5), 10)]
    result = merge_macd_fill(saved, source, START, CUTOFF)
    assert all(b.close == 3 for b in result[1:6])
    later = merge_macd_fill(saved, [*source, candle(START - timedelta(minutes=1), 4)], START, CUTOFF)
    assert all(b.close == 4 for b in later[1:6])


@pytest.mark.parametrize("anchor", [START - timedelta(minutes=91), START - timedelta(days=1)])
def test_before_0700_or_wrong_day_anchor_cannot_backfill(anchor):
    result = merge_macd_fill([], [candle(anchor, 3), candle(START + timedelta(minutes=28), 4)],
                             START, CUTOFF)
    assert len(result) == 30 and result[0].timestamp == START + timedelta(minutes=28)


def test_anchor_only_payload_still_fails_scoped_provider_proof(caplog):
    fill = filler([candle(START - timedelta(minutes=1), 3)])
    for _ in range(2):
        with pytest.raises(ValueError, match="schwab_fill_unavailable"):
            fill.supplement("TEST", [], NOW)
    assert "reason=empty_scoped_schwab_fill" in caplog.text


def test_later_price_without_anchor_is_never_backfilled():
    result = merge_macd_fill([], [candle(START + timedelta(minutes=28), 4)], START, CUTOFF)
    assert len(result) == 30 and result[0].timestamp == START + timedelta(minutes=28)
