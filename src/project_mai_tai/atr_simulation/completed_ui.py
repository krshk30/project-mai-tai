"""Pure completed-leg display over ONE validated SIM view; never an exit model."""

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from html import escape


IDENTITY_FIELDS = ("trading_day", "symbol", "broker", "account_id", "strategy", "flip_id", "segment_id")
LEGS = (("LIVE", "ACTUAL"), ("TODAY", "SUPPLIED"),
        ("VOLBODY", "QUEUE_AWARE"), ("VOLBODY", "OPTIMISTIC"))
LABELS = ("LIVE ACTUAL", "SIM TODAY", "SIM VOLBODY QUEUE_AWARE", "SIM VOLBODY OPTIMISTIC")
MAX_PAGE_ROWS = 10000
POSITION_CONTRACT = "ONE_POSITION_CURRENT_VWAP_FIRST_FILL_120S"
CLOSED_SIM_REASONS = {"TARGET_5", "STOP_8", "FLOOR_NEG05_AFTER_2", "TIME_NO_1PCT_120S",
                      "SELL_FLIP", "SESSION_END", "SAME_AS_LIVE"}
UNFILLED = {"NO_MODELLED_ENTRY", "VB_WAIT_NEVER_FILLED", "VB_SKIP_TO_SELL_FLIP"}


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() and (not positive or result > 0) else None


def _fact(fact, *, live):
    if not isinstance(fact, Mapping) or type(fact.get("quantity")) is not int or fact["quantity"] <= 0:
        return False
    clock = fact.get("at")
    if not isinstance(clock, Mapping) or type(clock.get("event_ns")) is not int or clock["event_ns"] <= 0:
        return False
    if _number(fact.get("price"), positive=True) is None:
        return False
    return not live or isinstance(fact.get("fill_id"), str) and bool(fact["fill_id"])


def _leg(row, *, sim_binding=False, copy_receipts=True):
    if row is None:
        return {"status": "MISSING_UNKNOWN", "completed": False, "known_pnl": None, "receipt": None,
                "completion_issues": ["NO_LEG_IN_KNOWN_PAGE"]}
    receipt = deepcopy(row) if copy_receipts else row
    live = row["mode"] == "LIVE"
    entries = row.get("entries" if live else "fills", [])
    exits = row.get("exits", [])
    if not isinstance(entries, (list, tuple)) or not isinstance(exits, (list, tuple)):
        raise ValueError("validated_fact_sequences_required")
    decision = row.get("decision")
    explicit_open = any(isinstance(item, Mapping) and item.get("reason") == "OPEN" for item in exits)
    problems = []
    if not live and not sim_binding:
        problems.append("UNKNOWN_STORED_SIM_ENGINE_BINDING")
    if row.get("issues") not in ([], ()) or decision == "UNKNOWN":
        problems.append("UNKNOWN_OR_ISSUED_RECEIPT")
    if live:
        coverage = row.get("coverage")
        if (not isinstance(coverage, Mapping) or coverage.get("complete") is not True
                or coverage.get("gaps") not in ([], ()) or not coverage.get("sources")):
            problems.append("UNKNOWN_LIVE_COVERAGE")
        if decision != "BOUGHT":
            problems.append("NO_AFFIRMATIVE_LIVE_ENTRY")
    if not entries or not all(_fact(item, live=True) for item in entries):
        problems.append("NO_ACCEPTED_POSITIVE_ENTRY_FACTS")
    if not exits or not all(_fact(item, live=live) for item in exits):
        problems.append("NO_KNOWN_CLOSED_EXIT_FACTS")
    if explicit_open:
        problems.append("OPEN_EXIT")
    if _number(row.get("gross_pnl")) is None:
        problems.append("UNKNOWN_PNL")
    if not problems:
        entry_ids = [item["fill_id"] for item in entries]
        if len(set(entry_ids)) != len(entry_ids):
            problems.append("DUPLICATE_ACCEPTED_FILL_ID")
        if live and len({item["fill_id"] for item in exits}) != len(exits):
            problems.append("DUPLICATE_EXIT_FILL_ID")
        if live and set(entry_ids).intersection(item["fill_id"] for item in exits):
            problems.append("SAME_FILL_ID_USED_FOR_ENTRY_AND_EXIT")
        entry_qty, exit_qty = sum(item["quantity"] for item in entries), sum(item["quantity"] for item in exits)
        if entry_qty != exit_qty:
            problems.append("UNRESOLVED_CLOSURE_QUANTITY")
        entry_times, exit_times = [item["at"]["event_ns"] for item in entries], [item["at"]["event_ns"] for item in exits]
        if min(exit_times) < min(entry_times) or max(exit_times) < max(entry_times):
            problems.append("NONCAUSAL_CLOSED_FACTS")
        if not live:
            if (len(exits) != 1 or exits[0].get("reason") not in CLOSED_SIM_REASONS
                    or exits[0].get("issues") not in ([], ())):
                problems.append("NO_CANONICAL_WHOLE_POSITION_EXIT")
            if (type(row.get("filled_qty")) is not int or row["filled_qty"] != entry_qty
                    or type(row.get("first_fill_ts")) is not int or row["first_fill_ts"] != min(entry_times)
                    or _number(row.get("vwap"), positive=True) is None):
                problems.append("UNRESOLVED_CANONICAL_POSITION")
            position = row.get("position")
            if (row.get("position_contract") != POSITION_CONTRACT or not isinstance(position, Mapping)
                    or any(position.get(name) != row.get(name) for name in (
                        "first_fill_ts", "fills", "vwap", "filled_qty", "remainder_cancel_reason"))):
                problems.append("CANONICAL_POSITION_RECEIPT_NOT_BOUND")
    completed = not problems
    if completed:
        status = "COMPLETED"
    elif explicit_open:
        status = "OPEN"
    elif (sim_binding and decision in UNFILLED and not entries and not exits and row.get("issues") in ([], ())
            and type(row.get("filled_qty")) is int and row["filled_qty"] == 0):
        status = decision
    elif live and decision == "NO_BUY" and not entries and not exits and (
            isinstance(row.get("coverage"), Mapping) and row["coverage"].get("complete") is True
            and row["coverage"].get("gaps") in ([], ()) and row["coverage"].get("sources")
            and row.get("issues") in ([], ())):
        status = "NO_BUY"
    else:
        status = "UNKNOWN" if (decision == "UNKNOWN" or row.get("issues")
            or any(problem.startswith("UNKNOWN_") for problem in problems)) else "INCOMPLETE"
    return {"status": status, "completed": completed, "known_pnl": row.get("gross_pnl") if completed else None,
            "receipt": receipt, "completion_issues": problems}


def completed_comparison_view(view, *, copy_receipts=True):
    """Display closure checks only; owned-view callers may borrow receipts.

    Public default retains the original detached receipt tree. Borrowing is only
    for immediate rendering/validation inside one private projection boundary.
    """
    if not isinstance(view, Mapping):
        raise ValueError("validated_simulation_view_required")
    source, page = view.get("source"), view.get("rows")
    if not isinstance(page, (list, tuple)) or len(page) > MAX_PAGE_ROWS:
        raise ValueError("bounded_validated_page_required")
    output = {"label": "COMPLETED - SIMULATION / NOT REAL TRADES", "live_totals_included": False,
        "scope": None, "rows": [], "known_page_counts": None,
        "coverage_status": deepcopy(view.get("coverage_status", "UNKNOWN")),
        "truncated": view.get("truncated"), "population_denominator": "UNKNOWN",
        "read_status": view.get("read_status", "UNKNOWN_SOURCE"),
        "completion_basis": "AT_LEAST_ONE_EXPLICITLY_COMPLETED_LEG_NOT_ALL_FOUR"}
    if (view.get("read_status") != "AVAILABLE" or not isinstance(source, Mapping)
            or not all(isinstance(source.get(key), str) and source[key] for key in ("source_id", "policy_version"))):
        output["coverage_status"] = "UNKNOWN"
        return output
    if type(view.get("truncated")) is not bool:
        raise ValueError("explicit_page_truncation_required")
    scope = {key: source[key] for key in ("source_id", "policy_version")}
    output["scope"] = deepcopy(scope)
    binding, coverage = view.get("position_api_binding_head"), source.get("coverage")
    sim_binding = (isinstance(binding, str) and bool(binding) and isinstance(coverage, Mapping)
        and coverage.get("engine_dependency_head") == binding)
    groups = {}
    for row in page:
        if not isinstance(row, Mapping) or not isinstance(row.get("identity"), Mapping):
            raise ValueError("full_flip_identity_required")
        identity = row["identity"]
        if not all(isinstance(identity.get(key), str) and identity[key] for key in IDENTITY_FIELDS):
            raise ValueError("full_flip_identity_required")
        if any(key in row and row[key] != scope[key] for key in scope):
            raise ValueError("row_conflicts_with_source_policy")
        key = tuple(scope.values()) + tuple(identity[name] for name in IDENTITY_FIELDS)
        leg_key = (row.get("mode"), row.get("model"))
        if leg_key not in LEGS:
            raise ValueError("canonical_mode_model_required")
        group = groups.setdefault(key, {"identity": {name: identity[name] for name in IDENTITY_FIELDS}, "legs": {}})
        if leg_key in group["legs"]:
            raise ValueError("duplicate_leg_in_source_policy_identity")
        group["legs"][leg_key] = _leg(row, sim_binding=sim_binding, copy_receipts=copy_receipts)
    completed_counts, states = Counter(), Counter()
    unanchored = []
    for key in sorted(groups):
        group = groups[key]
        legs = [group["legs"].get(pair) or _leg(None) for pair in LEGS]
        for label, leg in zip(LABELS, legs):
            states[label + ":" + leg["status"]] += 1
            if leg["completed"]:
                completed_counts[label] += 1
        if any(leg["completed"] for leg in legs):
            output["rows"].append({"scope": deepcopy(scope), "identity": deepcopy(group["identity"]), "legs": legs})
        else:
            unanchored.append(deepcopy(group["identity"]))
    output["known_page_counts"] = {"scope": "KNOWN_PAGE_ONLY_NOT_WATCHED_POPULATION", "fetched_rows": len(page),
        "identities": len(groups), "completed_anchor_rows": len(output["rows"]),
        "completed_legs": {label: completed_counts[label] for label in LABELS}, "leg_states": dict(states),
        "unanchored_identities": unanchored}
    return output


def render_completed_section(completed):
    """Pure rendering, with raw receipt values escaped and never recalculated."""
    def text(value):
        return escape("UNKNOWN" if value is None else str(value), quote=True)

    def facts(items):
        return "; ".join(str(item.get("quantity")) + " @ " + str(item.get("price"))
            + " / event_ns=" + str((item.get("at") or {}).get("event_ns"))
            for item in items if isinstance(item, Mapping)) or "UNKNOWN / no facts"

    rendered = []
    for group in completed["rows"]:
        identity = group["identity"]
        cells = [text(" / ".join(identity[key] for key in IDENTITY_FIELDS))]
        for leg in group["legs"]:
            receipt = leg["receipt"]
            if receipt is None:
                cells.append(text(leg["status"]))
                continue
            entries = receipt.get("entries" if receipt["mode"] == "LIVE" else "fills", [])
            exits = receipt.get("exits", [])
            reasons = [{key: item.get(key) for key in ("reason", "raw_reason", "mapped_reason")} for item in exits]
            cells.append("<strong>" + text(leg["status"]) + "</strong><br>Decision: " + text(receipt.get("decision"))
                + "<br>Entry: " + text(facts(entries)) + "<br>Exit: " + text(facts(exits))
                + "<br>Known completed PnL: " + text(leg["known_pnl"]) + "<br>Exit reasons: " + text(reasons)
                + "<br>LIVE raw/mapped: " + text(receipt.get("live_raw_reasons")) + " / "
                + text(receipt.get("live_mapped_reasons"))
                + "<br>Receipt issues: " + text(receipt.get("issues"))
                + "<br>Exit issues (receipt order): " + text([item.get("issues") for item in exits])
                + "<br>Closure checks: " + text(leg["completion_issues"]))
            if receipt["mode"] != "LIVE":
                cells[-1] += ("<br>Clock: " + text(receipt.get("clock_label"))
                    + "<br>Conditions: " + text(receipt.get("condition_coverage"))
                    + "<br>Policy: " + text(receipt.get("execution_policy_label"))
                    + " / " + text((receipt.get("execution_policy") or {}).get("source"))
                    + "<br>Lag limitation: " + text(receipt.get("live_lag_note")))
        rendered.append("<tr>" + "".join("<td>" + cell + "</td>" for cell in cells) + "</tr>")
    if not rendered:
        rendered.append('<tr><td colspan="5">No proven completed anchors in this known page; '
            'not proof of zero trades or NO_BUY.</td></tr>')
    return ('<section id="atr-simulation-completed"><h2>COMPLETED - SIMULATION / NOT REAL TRADES</h2>'
        '<p>At least one leg is explicitly completed. Companion OPEN/UNKNOWN/unfilled legs remain visible; '
        'not all four are necessarily completed. Never included in live PnL.</p>'
        '<div style="overflow-x:auto"><table><thead><tr><th>Full Flip Identity</th>'
        + "".join("<th>" + label + "</th>" for label in LABELS) + '</tr></thead><tbody>'
        + "".join(rendered) + '</tbody></table></div><p>Source/policy: ' + text(completed["scope"])
        + '; read status: ' + text(completed["read_status"]) + '; truncated: ' + text(completed["truncated"])
        + '; coverage: ' + text(completed["coverage_status"]) + '. Counts: ' + text(completed["known_page_counts"])
        + '. Page counts are not the watched-population denominator. All-status API/raw receipts remain unchanged.'
        '</p></section>')
