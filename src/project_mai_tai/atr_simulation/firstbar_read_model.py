"""Read-only FIRSTBAR display projection from stored canonical engine facts."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .firstbar_binding import FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION
from .firstbar_display import DISPLAY_CONTRACT, WORLDS, firstbar_tables
from .firstbar_projection import FIRSTBAR_SEED_LABEL
from .firstbar_ui_reader import seed_run_is_bound
from .service_contract import canonical_position_fields
from .ui_schema import validate_stored_view


ET = ZoneInfo("America/New_York")


def _time(ns):
    if ns is None:
        return "UNKNOWN"
    if type(ns) is not int or not 0 < ns <= 2**63 - 1:
        raise ValueError("firstbar_display_integer_event_clock_required")
    instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=ns // 1_000_000_000, microseconds=(ns % 1_000_000_000) // 1000)
    return instant.astimezone(ET).isoformat(timespec="seconds")


def _money(value):
    return f"${value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):+,.2f}"


def firstbar_display_view(stored, *, source_id):
    """Never upgrades a stored run's dependency based on the current import.

Only presentation percentage and known-row subtotal formatting are derived.
Entry/exit/VWAP/quantity/PnL, decisions and reasons come from stored receipts.
"""
    validate_stored_view(stored)
    if len(stored["rows"]) > 10000:
        raise ValueError("bounded_firstbar_stored_page_required")
    run = stored["run"]
    coverage = {} if run is None else run["coverage"]
    bound = (run is not None and run.get("source_id") == source_id
        and run.get("policy_version") == FIRSTBAR_RESULT_VERSION
        and coverage.get("engine_dependency_head") == FIRSTBAR_ENGINE_HEAD
        and coverage.get("engine_contract_version") == FIRSTBAR_RESULT_VERSION)
    population_label = (FIRSTBAR_SEED_LABEL if bound and seed_run_is_bound(run)
        else "replay, in-sample; UNQUALIFIED_REPLAY")
    view = {"contract": DISPLAY_CONTRACT,
        "source": {"source_id": source_id,
            "policy_version": FIRSTBAR_RESULT_VERSION if bound else "UNKNOWN",
            "engine_dependency_head": FIRSTBAR_ENGINE_HEAD if bound else "UNKNOWN",
            "population_label": population_label, "qualification": "UNQUALIFIED_REPLAY"},
        "coverage_status": "COMPLETE" if bound and coverage.get("complete") is True
            and coverage.get("gaps") in ([], ()) else "UNKNOWN",
        "truncated": stored["truncated"], "outcomes": [], "totals": {}}
    sums, known = {mode: Decimal(0) for mode, _ in WORLDS}, {mode: 0 for mode, _ in WORLDS}
    if bound:
        for row in stored["rows"]:
            if row["mode"] not in sums:
                raise ValueError("firstbar_run_contains_other_world")
            if (row.get("engine_dependency_head") != FIRSTBAR_ENGINE_HEAD
                    or row.get("version") != FIRSTBAR_RESULT_VERSION):
                raise ValueError("firstbar_row_stored_dependency_mismatch")
            outcome = {"mode": row["mode"], "model": row["model"],
                "population_label": population_label,
                "identity": row["identity"], "outcome_id": row["outcome_id"],
                "issues": row["issues"]}
            fills, exits = row["fills"], row["exits"]
            if fills:
                if canonical_position_fields(row["position"]) != canonical_position_fields(row):
                    raise ValueError("firstbar_stored_canonical_position_mismatch")
                exit_known = (len(exits) == 1 and exits[0]["price"] is not None
                    and exits[0]["at"] is not None and exits[0]["reason"] != "OPEN")
                closed = (row["decision"] == "TRADE_CLOSED" and row["gross_pnl"] is not None
                    and not row["issues"] and exit_known and not exits[0]["issues"])
                explicit_open = any(exit["reason"] == "OPEN" for exit in exits)
                status = "CLOSED" if closed else "OPEN" if explicit_open else "UNKNOWN"
                pnl = Decimal(row["gross_pnl"]) if closed else None
                quantity, vwap = row["filled_qty"], Decimal(row["vwap"])
                if quantity != sum(fill["quantity"] for fill in fills):
                    raise ValueError("firstbar_stored_quantity_mismatch")
                if row["first_fill_ts"] != min(fill["at"]["event_ns"] for fill in fills):
                    raise ValueError("firstbar_stored_first_fill_mismatch")
                if closed and exits[0]["quantity"] != quantity:
                    raise ValueError("firstbar_stored_exit_quantity_mismatch")
                reason = " / ".join(exit["reason"] for exit in exits)
                raw_reasons = [exit["live_exit"]["raw_reason"] for exit in exits
                               if exit.get("live_exit") is not None]
                raw_reason_labels = ["UNKNOWN" if reason is None else reason for reason in raw_reasons]
                exit_issues = [issue for exit in exits for issue in exit["issues"]]
                outcome.update(kind="TRADE", trade_id=row["trade_id"], status=status,
                    accepted_quantity=quantity, allocation_refs=[fill["fill_id"] for fill in fills],
                    issues=list(dict.fromkeys([*row["issues"], *exit_issues])),
                    row={"ticker": row["identity"]["symbol"],
                        "path": row["identity"]["broker"] + ":" + row["identity"]["account_id"],
                        "quantity": str(quantity), "entry_time": _time(row["first_fill_ts"]),
                        "entry_price": "$" + str(vwap),
                        "exit_time": _time(exits[0]["at"]["event_ns"]) if exit_known else "UNKNOWN",
                        "exit_price": "$" + exits[0]["price"] if exit_known else "UNKNOWN",
                        "pnl": str(pnl) if closed else None,
                        "pnl_label": _money(pnl) if closed else "UNKNOWN",
                        "pnl_pct_label": f"{pnl / (vwap * quantity) * 100:+.1f}%" if closed else "UNKNOWN",
                        "pnl_label_known": closed,
                        "summary": reason + (" / raw: " + "; ".join(raw_reason_labels) if raw_reasons else "")})
                if closed:
                    sums[row["mode"]] += pnl
                    known[row["mode"]] += 1
            elif row.get("outcome_kind") == "SKIP" and row["decision"].startswith("SKIPPED_") and not row["issues"]:
                outcome.update(kind="SKIP", reason=row["decision"])
            else:
                outcome.update(kind="UNKNOWN")
            view["outcomes"].append(outcome)
    for mode, _ in WORLDS:
        view["totals"][mode] = {"known_pnl_label": _money(sums[mode]) if known[mode] else "UNKNOWN",
            "known_closed_trades": known[mode], "complete_requested_window": False}
    # Keep all totals explicitly known-page-only until the read port proves the
    # requested world/identity denominator, not just a nontruncated SQL result.
    firstbar_tables(view)
    return view
