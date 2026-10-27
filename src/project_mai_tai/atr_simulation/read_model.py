"""Read-only SIMULATION projection. Never mutates or totals a live dashboard."""

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal

from .repository import json_value
from .service_contract import (
    PARTIAL_POSITION_ENGINE_HEAD,
    POSITION_CONTRACT,
    POSITION_RECEIPT_FIELDS,
    POSITION_TIMESTAMP_UNIT,
    canonical_position_fields,
)


SIMULATION_LABEL = "SIMULATION - NOT REAL TRADES"


def result_rows(result):
    identity, live = json_value(result.identity), json_value(result.live)
    from .policy_io import POLICY_LABEL

    policy = getattr(result, "execution_policy", None)
    # Old stored/result shapes stay old; a current import does not upgrade their provenance.
    simulation_metadata = {} if not hasattr(result, "clock_label") else {
        "clock_label": result.clock_label,
        "condition_coverage": json_value(getattr(result, "condition_coverage", None)),
        "execution_policy": json_value(policy),
        "execution_policy_label": None if policy is None else POLICY_LABEL,
        "live_lag_note": getattr(result, "live_lag_note", None)}
    known = live["coverage"]["complete"] is True and not live["coverage"].get("gaps") and not live["issues"]
    bought = live["bought"]
    decision = "BOUGHT" if bought is True else "NO_BUY" if bought is False and known else "UNKNOWN"
    entry_qty = sum(entry["quantity"] for entry in live["entries"])
    exit_qty = sum(exit["quantity"] for exit in live["exits"])
    live_pnl = None
    if known and bought is True and entry_qty > 0 and entry_qty == exit_qty:
        live_pnl = str(sum((Decimal(exit["price"]) * exit["quantity"] for exit in live["exits"]), Decimal(0))
            - sum((Decimal(entry["price"]) * entry["quantity"] for entry in live["entries"]), Decimal(0)))
    live_row = {"identity": identity, "mode": "LIVE", "model": "ACTUAL", "decision": decision,
        "entries": live["entries"], "exits": live["exits"], "gross_pnl": live_pnl,
        "coverage": live["coverage"], "issues": live["issues"], "comparison": "LIVE_FACTS_ONLY"}
    output = [live_row]
    live_reasons = [exit["mapped_reason"] for exit in live["exits"]]
    live_entry_events = [(item["at"]["event_ns"], Decimal(item["price"]), item["quantity"])
                         for item in live["entries"]]
    live_exit_events = [(item["at"]["event_ns"], Decimal(item["price"]), item["quantity"])
                        for item in live["exits"]]
    for scenario in (result.today, result.volbody_queue_aware, result.volbody_optimistic):
        item = json_value(scenario)
        position = item.get("position")
        if not isinstance(position, dict):
            raise ValueError("canonical_position_receipt_unbound:position")
        # Queue allocations and accepted position facts have different provenance.
        item["fill_allocations"] = item["fills"]
        item.update(canonical_position_fields(position), position_contract=POSITION_CONTRACT)
        item["engine_gross_pnl"] = item["gross_pnl"]
        if item["decision"] == "UNKNOWN" or item["issues"]:
            item["gross_pnl"] = None
        reasons = [exit["reason"] for exit in item["exits"]]
        if len(reasons) == len(live_reasons) == 1:
            equivalent = {"TARGET_5": "TARGET", "STOP_8": "STOP", "SELL_FLIP": "SELL_FLIP",
                "FLOOR_NEG05_AFTER_2": "FLOOR"}
            match = equivalent.get(reasons[0]) == live_reasons[0] if reasons[0] in equivalent else None
        else:
            match = None
        entry_events = [(fill["at"]["event_ns"], Decimal(fill["price"]), fill["quantity"])
                        for fill in item["fills"]]
        exit_events = [(exit["at"]["event_ns"], Decimal(exit["price"]), exit["quantity"])
                       for exit in item["exits"] if exit["at"] is not None and exit["price"] is not None]
        differences = []
        if live_entry_events != entry_events:
            differences.append("ENTRY_TIME_PRICE_QUANTITY")
        if live_exit_events != exit_events:
            differences.append("EXIT_TIME_PRICE_QUANTITY")
        if match is False:
            differences.append("EXIT_REASON")
        if item["gross_pnl"] is not None and live_pnl is not None and Decimal(item["gross_pnl"]) != Decimal(live_pnl):
            differences.append("PNL")
        item.update(identity=identity, model=item["model"] or "SUPPLIED",
            live_raw_reasons=[exit["raw_reason"] for exit in live["exits"]],
            live_mapped_reasons=live_reasons, reason_pair={"live": live_reasons, "sim": reasons},
            reason_match=match, what_changed=differences,
            comparison="UNKNOWN" if item["gross_pnl"] is None or live_pnl is None or match is None
            else "MATCH" if not differences else "CHANGED",
            pnl_difference=None if item["gross_pnl"] is None or live_pnl is None
            else str(Decimal(item["gross_pnl"]) - Decimal(live_pnl)))
        item.update(simulation_metadata)
        output.append(item)
    return tuple(output)


def simulation_view(stored, *, now=None, _detached_snapshot=None):
    if _detached_snapshot is not None:
        from .ui_schema import _DetachedSimulationSnapshot

        if (type(_detached_snapshot) is not _DetachedSimulationSnapshot
                or _detached_snapshot.snapshot is not stored):
            raise ValueError("owned_validated_simulation_snapshot_required")
    now = now or datetime.now(timezone.utc)
    cohorts, totals, pairs = Counter(), {}, Counter()
    pair_differences, outcome_counts = {}, Counter()
    for row in stored["rows"]:
        key = row["mode"] + ":" + row["model"]
        cohorts[key + ":" + row["decision"]] += 1
        if row["gross_pnl"] is not None:
            totals[key] = totals.get(key, Decimal(0)) + Decimal(row["gross_pnl"])
        if "reason_pair" in row:
            pair = row["reason_pair"]
            pair_key = key + ":" + str(pair)
            pairs[pair_key] += 1
            if row.get("pnl_difference") is not None:
                pair_differences[pair_key] = pair_differences.get(pair_key, Decimal(0)) + Decimal(row["pnl_difference"])
            live_pnl = None if row.get("pnl_difference") is None else Decimal(row["gross_pnl"]) - Decimal(row["pnl_difference"])
            if live_pnl is not None and row["gross_pnl"] is not None:
                simulated_pnl = Decimal(row["gross_pnl"])
                if live_pnl > 0 and simulated_pnl <= 0:
                    outcome_counts[key + ":WINNER_LOST"] += 1
                if live_pnl < 0 and simulated_pnl >= 0:
                    outcome_counts[key + ":LOSER_AVOIDED"] += 1
    workers = []
    for status in stored["consumers"]:
        stamp = status["updated_at"]
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (now - stamp).total_seconds()
        workers.append({"consumer_id": status["consumer_id"], "age_seconds": age,
            "online": 0 <= age <= 30, "status": status["status"], "issues": status["issues"]})
    run = stored["run"]
    coverage = run.get("coverage") if isinstance(run, Mapping) else None
    binding = ({"position_api_binding_head": PARTIAL_POSITION_ENGINE_HEAD}
        if isinstance(coverage, Mapping)
        and coverage.get("engine_dependency_head") == PARTIAL_POSITION_ENGINE_HEAD else {})
    result = {"label": SIMULATION_LABEL, "live_totals_included": False,
        "position_contract": POSITION_CONTRACT, "position_receipt_fields": POSITION_RECEIPT_FIELDS,
        "position_timestamp_unit": POSITION_TIMESTAMP_UNIT,
        **binding,
        "coverage_status": "UNKNOWN" if stored["run"] is None else stored["run"]["coverage"],
        "rows": stored["rows"], "truncated": stored["truncated"], "workers": workers,
        "worker_online": any(worker["online"] for worker in workers),
        "counts": dict(cohorts), "known_row_pnl_subtotals": None if stored["truncated"] else totals,
        "subtotals_are_not_full_denominator": True, "reason_pair_counts": dict(pairs),
        "known_reason_pair_pnl_differences": None if stored["truncated"] else pair_differences,
        "known_outcome_counts": dict(outcome_counts),
        "source": stored["run"]}
    if _detached_snapshot is None:
        return json_value(result)
    # This private tree was detached, normalized and validated before projection.
    # Normalize computed metadata without walking large raw observations again.
    result["rows"] = None
    normalized = json_value(result)
    normalized["rows"] = stored["rows"]
    return normalized

