from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Iterable

from project_mai_tai.momentum_paper.models import MomentumGrade


def _measured(rows: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if str(row.get("status")) == "FINAL"
        and str(row.get("exit_reason")) in {"TARGET", "STOP", "TIME"}
        and row.get("pnl_pct") is not None
    ]


def _rates(rows: list[dict[str, object]]) -> tuple[Decimal, Decimal]:
    wins = sum(Decimal(str(row["pnl_pct"])) > 0 for row in rows)
    win_rate = Decimal(wins) * Decimal("100") / Decimal(len(rows))
    average = sum(Decimal(str(row["pnl_pct"])) for row in rows) / Decimal(len(rows))
    return win_rate, average


def grade_strategy(rows: Iterable[dict[str, object]], *, complete_sessions: int) -> MomentumGrade:
    source = list(rows)
    measured = _measured(source)
    counts = Counter(str(row.get("status", "UNKNOWN")) for row in source)
    counts.update(
        {
            f"exit_{reason.lower()}": sum(row.get("exit_reason") == reason for row in measured)
            for reason in ("TARGET", "STOP", "TIME")
        }
    )
    sample_met = complete_sessions >= 20 and len(measured) >= 40
    if not measured:
        return MomentumGrade(
            verdict="UNEXERCISED" if not source else "COULD_NOT_TELL",
            sessions=complete_sessions,
            filled_gradable=0,
            wins=0,
            win_rate_pct=None,
            average_pnl_pct=None,
            sample_met=sample_met,
            win_rate_met=None,
            average_met=None,
            drop_session_met=None,
            drop_symbol_met=None,
            counts=dict(counts),
        )
    win_rate, average = _rates(measured)
    reduced: list[dict[str, object]] = []
    session_checks: list[bool] = []
    for session_date in sorted({str(row["session_date"]) for row in measured}):
        sample = [row for row in measured if str(row["session_date"]) != session_date]
        if not sample:
            continue
        sample_win, sample_avg = _rates(sample)
        passed = sample_win >= Decimal("70") and sample_avg >= Decimal("1.0")
        session_checks.append(passed)
        reduced.append(
            {
                "drop": f"session:{session_date}",
                "count": len(sample),
                "win_rate_pct": str(sample_win),
                "average_pnl_pct": str(sample_avg),
                "passed": passed,
            }
        )
    symbol_checks: list[bool] = []
    for symbol in sorted({str(row["symbol"]) for row in measured}):
        sample = [row for row in measured if str(row["symbol"]) != symbol]
        if not sample:
            continue
        sample_win, sample_avg = _rates(sample)
        passed = sample_win >= Decimal("70") and sample_avg >= Decimal("1.0")
        symbol_checks.append(passed)
        reduced.append(
            {
                "drop": f"symbol:{symbol}",
                "count": len(sample),
                "win_rate_pct": str(sample_win),
                "average_pnl_pct": str(sample_avg),
                "passed": passed,
            }
        )
    win_rate_met = win_rate >= Decimal("70")
    average_met = average >= Decimal("1.0")
    drop_session_met = bool(session_checks) and all(session_checks)
    drop_symbol_met = bool(symbol_checks) and all(symbol_checks)
    verdict = (
        "PASS"
        if sample_met and win_rate_met and average_met and drop_session_met and drop_symbol_met
        else "FAIL"
    )
    return MomentumGrade(
        verdict=verdict,
        sessions=complete_sessions,
        filled_gradable=len(measured),
        wins=sum(Decimal(str(row["pnl_pct"])) > 0 for row in measured),
        win_rate_pct=win_rate,
        average_pnl_pct=average,
        sample_met=sample_met,
        win_rate_met=win_rate_met,
        average_met=average_met,
        drop_session_met=drop_session_met,
        drop_symbol_met=drop_symbol_met,
        counts=dict(counts),
        reduced_samples=tuple(reduced),
    )
