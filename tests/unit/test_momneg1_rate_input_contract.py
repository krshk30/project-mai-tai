"""Fast authored shape controls, NOT the strict60 execution receipt."""

from collections import Counter
from decimal import Decimal

import pytest

from tests.unit import test_momneg1_signal_native_rate as rate


def test_synthetic_rate_only_has_disjoint_explicit_branch_coverage():
    packet, sha = rate.load_synthetic_rate_only()
    assert packet["kind"] == "SYNTHETIC_RATE_ONLY"
    assert sha == rate.SYNTHETIC_RATE_SHA256
    assert "NOT P1" in packet["provenance"]
    cases = packet["cases"]
    assert len(cases) == len({row["case_id"] for row in cases}) == 24
    assert len({row["record"]["symbol"] for row in cases}) == 24
    assert Counter(row["broker"] for row in cases) == {"schwab": 8, "webull": 16}
    assert Counter(row["record"]["strategy_code"] for row in cases) == {
        "momentum_30s_warn": 12, "momentum_60s_warn": 12}
    shapes = Counter()
    for row in cases:
        assert row["case_id"].startswith("SYNTHETIC-")
        assert type(row["day_volume"]) is int and row["day_volume"] >= 50000
        payload = row["record"]["payload"]
        assert type(payload["observed_decision_ns"]) is int
        prior = payload["prior_close"]
        if prior is None:
            shapes["unknown"] += 1
        else:
            assert isinstance(prior, str)
            price, prior = Decimal(payload["detect"]["price"]), Decimal(prior)
            shapes["down" if price < prior else "equal" if price == prior else "up"] += 1
    assert shapes == {"unknown": 6, "down": 6, "equal": 6, "up": 6}


def test_synthetic_dataset_pin_cannot_silently_change(monkeypatch):
    monkeypatch.setattr(rate, "SYNTHETIC_RATE_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="controlled input changed"):
        rate.load_synthetic_rate_only()
