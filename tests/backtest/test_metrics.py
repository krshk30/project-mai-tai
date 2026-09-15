from decimal import Decimal
from pathlib import Path

from project_mai_tai.backtest.metrics import kaufman_efficiency_ratio


def test_kaufman_efficiency_ratio_preserves_the_existing_census_formula() -> None:
    values = [Decimal("10"), Decimal("12"), Decimal("11"), Decimal("13")]

    assert kaufman_efficiency_ratio(values) == Decimal("3") / Decimal("5")


def test_kaufman_efficiency_ratio_distinguishes_unknown_from_stationary() -> None:
    assert kaufman_efficiency_ratio([Decimal("10")]) is None
    assert kaufman_efficiency_ratio([Decimal("10"), Decimal("10")]) == 0


def test_operator_census_reuses_the_shared_kaufman_metric() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "orb_operator_filter_census.py"
    ).read_text(encoding="utf-8")

    assert "from project_mai_tai.backtest.metrics import kaufman_efficiency_ratio" in source
    assert "efficiency = kaufman_efficiency_ratio(closes)" in source
