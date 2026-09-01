from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "health" / "reject_classes.py"
SPEC = importlib.util.spec_from_file_location("reject_classes", SCRIPT)
assert SPEC and SPEC.loader
reject_classes = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reject_classes
SPEC.loader.exec_module(reject_classes)


def test_query_counts_rejected_intents_from_the_durable_payload() -> None:
    assert "FROM trade_intents ti" in reject_classes.QUERY
    assert "ti.status = 'rejected'" in reject_classes.QUERY
    assert "refusal_origin" in reject_classes.QUERY
    assert "refusal_code" in reject_classes.QUERY
    assert "broker_order_events" not in reject_classes.QUERY
    assert "event_source" not in reject_classes.QUERY


def test_report_keeps_the_three_required_origins_distinct() -> None:
    today = date(2026, 9, 1)
    rows = [
        ("live:orb", "client_abort", "MISSING_ACCOUNT", "open", today, 2),
        ("live:orb", "broker_reject", "VENUE_REFUSED", "open", today, 3),
        ("live:orb", "skipped_before_submit", "COLLISION", "open", today, 5),
    ]

    report, pages, _known = reject_classes.render(rows, today=today, days=10, known=set())

    assert "[client_abort]" in report
    assert "[broker_reject]" in report
    assert "[skipped_before_submit]" in report
    assert len(pages) == 3


def test_unlabelled_history_is_could_not_tell_not_broker_reject() -> None:
    grouped = reject_classes.aggregate(
        [("paper:polygon_30s", None, "missing reference_price", "open", date(2026, 8, 31), 56)]
    )

    assert list(grouped) == [
        ("paper:polygon_30s", "could_not_tell", "missing reference_price", "open")
    ]


def test_legacy_known_class_does_not_mass_page_when_origin_is_added() -> None:
    today = date(2026, 9, 1)
    rows = [("live:orb", "broker_reject", "VENUE_REFUSED", "open", today, 3)]
    legacy_key = "live:orb|VENUE_REFUSED|open"

    _report, pages, known = reject_classes.render(
        rows,
        today=today,
        days=10,
        known={legacy_key},
    )

    assert pages == []
    assert "live:orb|broker_reject|VENUE_REFUSED|open" in known
