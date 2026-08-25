#!/usr/bin/env python3
"""Count current phantom OMS-managed rows from persisted broker truth.

This is deliberately DB-only. It SELECTs ``oms_managed_positions`` and the OMS-maintained
``account_positions`` mirror; it imports no broker adapter or SDK, receives no broker credential,
and makes no venue call. The reconciler already detects the wider
``position_quantity_mismatch`` population. This check is a narrow, field-level counter for the
specific stale-open managed-row shape and records the persisted evidence needed to investigate it.

Verdicts (and exit codes):
  CLEAN_MEASURED_ZERO (0)  no confirmed phantom in a fresh, completely measured population
  CONFIRMED_PHANTOM  (2)   managed quantity > 0 and fresh persisted broker quantity == 0
  COULD_NOT_TELL     (3)   DB failure, missing/stale broker truth, or population changed mid-read
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import os

import psycopg
from psycopg.rows import dict_row

BROKER_TRUTH_STALE_SECONDS = 300
FRESH_FILL_GRACE_SECONDS = 120


@dataclass(frozen=True)
class PersistedManagedRow:
    row_id: str
    strategy_code: str
    account: str
    provider: str
    environment: str
    account_active: bool | None
    symbol: str
    managed_qty: Decimal
    entry_price: Decimal
    entry_path: str
    entry_time: datetime
    managed_updated_at: datetime
    truth_present: bool
    broker_qty: Decimal | None
    truth_source_updated_at: datetime | None


@dataclass(frozen=True)
class RowVerdict:
    row: PersistedManagedRow
    verdict: str
    reason: str


@dataclass(frozen=True)
class Report:
    verdict: str
    exit_code: int
    results: tuple[RowVerdict, ...]
    population_error: str = ""

    @property
    def phantoms(self) -> int:
        return sum(result.verdict == "CONFIRMED_PHANTOM" for result in self.results)

    @property
    def unknown(self) -> int:
        return sum(result.verdict == "COULD_NOT_TELL" for result in self.results)

    @property
    def backed(self) -> int:
        return sum(result.verdict == "BACKED" for result in self.results)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def population_signature(rows: list[PersistedManagedRow]) -> tuple[tuple[object, ...], ...]:
    """Everything used by the verdict, so a concurrent sync/close cannot become false evidence."""
    return tuple(
        sorted(
            (
                row.row_id,
                row.account,
                row.symbol,
                row.managed_qty,
                row.account_active,
                row.truth_present,
                row.broker_qty,
                row.truth_source_updated_at,
            )
            for row in rows
        )
    )


def evaluate_rows(
    rows: list[PersistedManagedRow],
    *,
    now: datetime,
    stale_after_seconds: float = BROKER_TRUTH_STALE_SECONDS,
    fresh_fill_grace_seconds: float = FRESH_FILL_GRACE_SECONDS,
) -> Report:
    """Classify persisted rows. Missing or stale account truth is never equivalent to zero."""
    now = _utc(now)
    results: list[RowVerdict] = []
    for row in rows:
        if row.account_active is not True:
            results.append(
                RowVerdict(row, "COULD_NOT_TELL", "broker account is missing or inactive")
            )
            continue
        if not row.truth_present:
            results.append(
                RowVerdict(
                    row,
                    "COULD_NOT_TELL",
                    "no persisted account_positions row exists for account and symbol",
                )
            )
            continue
        if row.broker_qty is None:
            results.append(
                RowVerdict(row, "COULD_NOT_TELL", "persisted broker quantity is NULL")
            )
            continue
        if row.truth_source_updated_at is None:
            results.append(
                RowVerdict(row, "COULD_NOT_TELL", "broker truth has no source_updated_at")
            )
            continue

        truth_age = (now - _utc(row.truth_source_updated_at)).total_seconds()
        if truth_age < 0:
            results.append(
                RowVerdict(
                    row,
                    "COULD_NOT_TELL",
                    f"broker truth timestamp is {abs(truth_age):.0f}s in the future",
                )
            )
            continue
        if truth_age > stale_after_seconds:
            results.append(
                RowVerdict(
                    row,
                    "COULD_NOT_TELL",
                    f"persisted broker truth is {truth_age:.0f}s old (>{stale_after_seconds:.0f}s)",
                )
            )
            continue

        if row.broker_qty != 0:
            results.append(
                RowVerdict(
                    row,
                    "BACKED",
                    "fresh account_positions quantity is non-zero; no absent-row phantom proven",
                )
            )
            continue

        entry_age = (now - _utc(row.entry_time)).total_seconds()
        if entry_age < 0:
            results.append(
                RowVerdict(row, "COULD_NOT_TELL", "managed entry_time is in the future")
            )
            continue
        if entry_age < fresh_fill_grace_seconds:
            results.append(
                RowVerdict(
                    row,
                    "COULD_NOT_TELL",
                    f"fresh zero is inside the {fresh_fill_grace_seconds:.0f}s fill-propagation grace",
                )
            )
            continue
        results.append(
            RowVerdict(
                row,
                "CONFIRMED_PHANTOM",
                "managed quantity is non-zero while fresh persisted broker quantity is zero",
            )
        )

    phantoms = sum(result.verdict == "CONFIRMED_PHANTOM" for result in results)
    unknown = sum(result.verdict == "COULD_NOT_TELL" for result in results)
    if phantoms:
        return Report("CONFIRMED_PHANTOM", 2, tuple(results))
    if unknown:
        return Report("COULD_NOT_TELL", 3, tuple(results))
    return Report("CLEAN_MEASURED_ZERO", 0, tuple(results))


def evaluate_stable_population(
    before: list[PersistedManagedRow],
    after: list[PersistedManagedRow],
    *,
    now: datetime,
    stale_after_seconds: float = BROKER_TRUTH_STALE_SECONDS,
    fresh_fill_grace_seconds: float = FRESH_FILL_GRACE_SECONDS,
) -> Report:
    if population_signature(before) != population_signature(after):
        return Report(
            "COULD_NOT_TELL",
            3,
            (),
            f"managed/account-position population changed during read: before={len(before)} after={len(after)}",
        )
    return evaluate_rows(
        after,
        now=now,
        stale_after_seconds=stale_after_seconds,
        fresh_fill_grace_seconds=fresh_fill_grace_seconds,
    )


_POPULATION_SQL = """
SELECT m.id::text AS row_id,
       m.strategy_code,
       m.broker_account_name,
       coalesce(ba.provider, '') AS provider,
       coalesce(ba.environment, '') AS environment,
       ba.is_active AS account_active,
       m.symbol,
       m.current_quantity AS managed_qty,
       m.entry_price,
       m.entry_path,
       m.entry_time,
       m.updated_at AS managed_updated_at,
       (ap.id IS NOT NULL) AS truth_present,
       ap.quantity AS broker_qty,
       ap.source_updated_at AS truth_source_updated_at
FROM oms_managed_positions m
LEFT JOIN broker_accounts ba
  ON ba.name = m.broker_account_name
LEFT JOIN account_positions ap
  ON ap.broker_account_id = ba.id
 AND ap.symbol = m.symbol
WHERE m.status = 'open'
  AND m.current_quantity > 0
ORDER BY m.broker_account_name, m.symbol, m.entry_time
"""


def _row(mapping: dict[str, object]) -> PersistedManagedRow:
    return PersistedManagedRow(
        row_id=str(mapping["row_id"]),
        strategy_code=str(mapping["strategy_code"]),
        account=str(mapping["broker_account_name"]),
        provider=str(mapping["provider"] or "").lower(),
        environment=str(mapping["environment"] or "").lower(),
        account_active=(
            bool(mapping["account_active"])
            if mapping["account_active"] is not None
            else None
        ),
        symbol=str(mapping["symbol"]).upper(),
        managed_qty=Decimal(str(mapping["managed_qty"])),
        entry_price=Decimal(str(mapping["entry_price"])),
        entry_path=str(mapping["entry_path"] or ""),
        entry_time=_utc(mapping["entry_time"]),  # type: ignore[arg-type]
        managed_updated_at=_utc(mapping["managed_updated_at"]),  # type: ignore[arg-type]
        truth_present=bool(mapping["truth_present"]),
        broker_qty=(
            Decimal(str(mapping["broker_qty"]))
            if mapping["broker_qty"] is not None
            else None
        ),
        truth_source_updated_at=(
            _utc(mapping["truth_source_updated_at"])  # type: ignore[arg-type]
            if mapping["truth_source_updated_at"] is not None
            else None
        ),
    )


def _select_population(connection: psycopg.Connection) -> list[PersistedManagedRow]:
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(_POPULATION_SQL)
        return [_row(dict(mapping)) for mapping in cursor.fetchall()]


def _normalize_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def load_stable_population(dsn: str) -> tuple[list[PersistedManagedRow], list[PersistedManagedRow]]:
    with psycopg.connect(
        _normalize_dsn(dsn),
        connect_timeout=5,
        options="-c statement_timeout=5000",
    ) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        before = _select_population(connection)
        after = _select_population(connection)
    return before, after


def _safe(value: object) -> str:
    return str(value).replace("\n", " ").replace("\r", " ").replace("|", "/")


def print_report(report: Report, *, now: datetime) -> None:
    if report.population_error:
        print(f"POPULATION COULD_NOT_TELL evidence={_safe(report.population_error)}")
    else:
        print(
            f"POPULATION open_positive_managed_rows={len(report.results)} "
            f"observed_at={_utc(now).isoformat()} "
            "source=oms_managed_positions+account_positions"
        )
    for result in report.results:
        row = result.row
        truth_at = (
            row.truth_source_updated_at.isoformat()
            if row.truth_source_updated_at is not None
            else "missing"
        )
        broker_qty = "missing" if row.broker_qty is None else str(row.broker_qty)
        print(
            f"ROW verdict={result.verdict} id={_safe(row.row_id)} "
            f"strategy={_safe(row.strategy_code)} account={_safe(row.account)} "
            f"provider={_safe(row.provider or '<missing>')} "
            f"environment={_safe(row.environment or '<missing>')} active={row.account_active} "
            f"symbol={_safe(row.symbol)} managed_qty={row.managed_qty} "
            f"persisted_broker_qty={broker_qty} source_updated_at={truth_at} "
            f"entry_price={row.entry_price} entry_path={_safe(row.entry_path or '-')} "
            f"entry_time={row.entry_time.isoformat()} "
            f"managed_updated_at={row.managed_updated_at.isoformat()} "
            f"evidence={_safe(result.reason)}"
        )
    print(
        f"VERDICT: {report.verdict} confirmed_phantoms={report.phantoms} "
        f"backed={report.backed} could_not_tell={report.unknown} "
        f"population={len(report.results) if not report.population_error else '?'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stale-seconds", type=float, default=BROKER_TRUTH_STALE_SECONDS)
    parser.add_argument(
        "--fresh-fill-grace-seconds", type=float, default=FRESH_FILL_GRACE_SECONDS
    )
    args = parser.parse_args()
    if args.stale_seconds <= 0 or args.fresh_fill_grace_seconds < 0:
        parser.error("stale-seconds must be > 0 and fresh-fill-grace-seconds must be >= 0")

    dsn = os.environ.get("MAI_TAI_DATABASE_URL", "").strip()
    if not dsn:
        print("POPULATION COULD_NOT_TELL evidence=MAI_TAI_DATABASE_URL is missing")
        print(
            "VERDICT: COULD_NOT_TELL confirmed_phantoms=0 backed=0 "
            "could_not_tell=1 population=?"
        )
        return 3

    now = datetime.now(UTC)
    try:
        before, after = load_stable_population(dsn)
    except Exception as exc:  # noqa: BLE001 - a failed SELECT is unknown, never a clean zero
        print(
            f"POPULATION COULD_NOT_TELL evidence=database read failed: "
            f"{type(exc).__name__}: {_safe(exc)}"
        )
        print(
            "VERDICT: COULD_NOT_TELL confirmed_phantoms=0 backed=0 "
            "could_not_tell=1 population=?"
        )
        return 3

    report = evaluate_stable_population(
        before,
        after,
        now=now,
        stale_after_seconds=args.stale_seconds,
        fresh_fill_grace_seconds=args.fresh_fill_grace_seconds,
    )
    print_report(report, now=now)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
