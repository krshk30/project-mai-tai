from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ops.health.phantom_managed_rows import (
    BROKER_TRUTH_STALE_SECONDS,
    FRESH_FILL_GRACE_SECONDS,
    PersistedManagedRow,
    evaluate_rows,
    evaluate_stable_population,
    print_report,
)

NOW = datetime(2026, 8, 25, 18, 0, tzinfo=UTC)


def _row(
    *,
    broker_qty: Decimal | None = Decimal("0"),
    truth_present: bool = True,
    truth_age_seconds: int = 30,
    entry_age_seconds: int = 3600,
) -> PersistedManagedRow:
    return PersistedManagedRow(
        row_id="daic-row",
        strategy_code="orb",
        account="live:orb",
        provider="webull",
        environment="live",
        account_active=True,
        symbol="DAIC",
        managed_qty=Decimal("1"),
        entry_price=Decimal("3.20"),
        entry_path="fanout",
        entry_time=NOW - timedelta(seconds=entry_age_seconds),
        managed_updated_at=NOW - timedelta(minutes=5),
        truth_present=truth_present,
        broker_qty=broker_qty,
        truth_source_updated_at=NOW - timedelta(seconds=truth_age_seconds),
    )


def test_thresholds_are_positive_and_staleness_matches_preflight() -> None:
    assert BROKER_TRUTH_STALE_SECONDS == 300
    assert BROKER_TRUTH_STALE_SECONDS > 0
    assert FRESH_FILL_GRACE_SECONDS > 0


def test_known_positive_DAIC_shape_is_confirmed_from_fresh_persisted_zero() -> None:
    report = evaluate_rows([_row()], now=NOW)
    assert report.verdict == "CONFIRMED_PHANTOM"
    assert report.exit_code == 2
    assert report.phantoms == 1
    assert "fresh persisted broker quantity is zero" in report.results[0].reason


def test_clean_control_nonzero_persisted_broker_quantity_is_backed() -> None:
    report = evaluate_rows([_row(broker_qty=Decimal("1"))], now=NOW)
    assert report.verdict == "CLEAN_MEASURED_ZERO"
    assert report.exit_code == 0
    assert report.backed == 1


def test_zero_managed_rows_is_a_measured_clean_zero() -> None:
    report = evaluate_rows([], now=NOW)
    assert report.verdict == "CLEAN_MEASURED_ZERO"
    assert report.exit_code == 0


def test_missing_broker_truth_row_is_could_not_tell_not_zero() -> None:
    report = evaluate_rows(
        [_row(broker_qty=None, truth_present=False)],
        now=NOW,
    )
    assert report.verdict == "COULD_NOT_TELL"
    assert report.exit_code == 3
    assert report.phantoms == 0
    assert "no persisted account_positions row" in report.results[0].reason


def test_stale_or_missing_source_timestamp_is_could_not_tell() -> None:
    stale = _row(truth_age_seconds=301)
    missing = replace(_row(), truth_source_updated_at=None)
    for row in (stale, missing):
        report = evaluate_rows([row], now=NOW)
        assert report.verdict == "COULD_NOT_TELL"
        assert report.exit_code == 3
        assert report.phantoms == 0


def test_future_source_timestamp_is_could_not_tell() -> None:
    report = evaluate_rows([_row(truth_age_seconds=-1)], now=NOW)
    assert report.verdict == "COULD_NOT_TELL"
    assert "future" in report.results[0].reason


def test_fresh_fill_zero_is_could_not_tell_until_propagation_grace_expires() -> None:
    report = evaluate_rows([_row(entry_age_seconds=60)], now=NOW)
    assert report.verdict == "COULD_NOT_TELL"
    assert "fill-propagation grace" in report.results[0].reason


def test_confirmed_phantom_is_not_hidden_by_an_unknown_sibling() -> None:
    unknown = replace(
        _row(),
        row_id="other",
        account="live:other",
        account_active=False,
    )
    report = evaluate_rows([_row(), unknown], now=NOW)
    assert report.verdict == "CONFIRMED_PHANTOM"
    assert report.phantoms == 1
    assert report.unknown == 1


def test_population_or_truth_change_during_read_is_could_not_tell() -> None:
    before = [_row()]
    after = [replace(_row(), broker_qty=Decimal("1"))]
    report = evaluate_stable_population(before, after, now=NOW)
    assert report.verdict == "COULD_NOT_TELL"
    assert report.exit_code == 3
    assert report.population_error
    assert report.phantoms == 0


def test_stable_population_runs_normal_verdict_logic() -> None:
    row = _row()
    report = evaluate_stable_population([row], [row], now=NOW)
    assert report.verdict == "CONFIRMED_PHANTOM"


def test_report_names_persisted_evidence_and_row_identity(capsys) -> None:
    report = evaluate_rows([_row()], now=NOW)
    print_report(report, now=NOW)
    output = capsys.readouterr().out
    assert "source=oms_managed_positions+account_positions" in output
    assert "id=daic-row" in output
    assert "strategy=orb" in output
    assert "account=live:orb" in output
    assert "symbol=DAIC" in output
    assert "persisted_broker_qty=0" in output
    assert f"source_updated_at={(NOW - timedelta(seconds=30)).isoformat()}" in output


def test_workflow_runs_checker_with_only_DSN_not_sourced_credentials() -> None:
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "phantom-managed-row-watch.yml"
    ).read_text(encoding="utf-8")
    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert 'env -i PATH=/usr/bin:/bin MAI_TAI_DATABASE_URL=\\"\\$dsn\\"' in workflow
    assert 'source "$ENVFILE"' not in workflow
    assert "broker_adapters" not in workflow
