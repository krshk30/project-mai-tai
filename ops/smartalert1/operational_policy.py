"""SMARTALERT1 operational SHADOW policy generator (Main composition item).

Deterministic: the same inputs give byte-identical output. SHADOW only; the
emitted policy can never page (delivery NONE / paging_eligible False upstream).

What it binds, and from where:
- category_map: the reviewed literal map (claude-1 rulings 10-29 03:05 + 03:34 ET;
  operator-approved SMARTALERT1 card 10-28). Every reason code is checked against
  the L2/L3 source text at generation time.
- source_bindings: the given source head plus sha256 of trade.py (L2) and
  system_shadow.py (L3) read from the repo.
- close_calendar: EXTENDED 04:00-20:00 ET windows per date in [from, to].
  Trading days come from time_utils.US_MARKET_HOLIDAYS plus weekends; no
  early-close list exists in the repo, and by ruling an early close keeps the
  20:00 window. All windows share ONE session proof: the time_utils.py sha.
- producer_review: APPROVED_SHADOW_OPERANDS only when every operand is supplied
  (upstream L2/L3 policies and the acquisition/schedule/source-closure refs from
  the C6 exporter). Otherwise its status says what is missing, and the producer
  refuses with ``producer_operands_needs_ruling``. Nothing is invented.

The upstream L2/L3 policies carry per-class freshness/wait windows. By ruling
(03:34) those numbers stay UNBOUND until measured, so they are an input file,
never generated here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .delivery import FRAME_BYTES, SCHEMA, encoded, fingerprint, initial_state, policy_binding

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]
TRADE_SOURCE = "ops/smartalert1/trade.py"
SYSTEM_SOURCE = "ops/smartalert1/system_shadow.py"
SYSTEM_RULES = "ops/smartalert1/system.py"
HOLIDAY_SOURCE = "src/project_mai_tai/strategy_core/time_utils.py"

POLICYVERSION = "smartalert1-L4-operational-shadow-v1"
UPSTREAM_POLICYVERSIONS = {"L2": "smartalert1-L2-shadow-v1", "L3": "smartalert1-L3-shadow-v1"}
APPROVAL_REF = "operator:20261028:SMARTALERT1-card"
CALIBRATION_REF = "UNCALIBRATED-shadow-only"
CATEGORY_MAP_REF = "claude1:20261029:0305-0334:category-map"
DIGEST_SCOPES = ["L2-trade-classes-A1-A7", "L3-system-classes-B"]

# The trade/system findings that mean money at risk or a system failure page;
# everything else is DIGEST. A3/A7 use the source-bound TRUE rule instead.
_L2_PAGE = {
    "A1": ("owned_quantity_unprotected", "stops_shadow_bound_protection_row_absent",
           "stops_shadow_exit_handoff_recorded", "stops_shadow_insufficient_managed_quantity",
           "stops_shadow_no_protection_row", "stops_shadow_positive_native_protection_absent",
           "stops_shadow_recorded_pair_not_resting"),
    "A2": ("eligible_opportunity_without_order",),
    "A4": ("lifecycle_ack_missing_or_mismatched", "lifecycle_attach_child_identity_mismatch",
           "lifecycle_attach_exit_or_cancel_unresolved", "lifecycle_attach_quantity_mismatch",
           "lifecycle_attempt_base_ambiguous", "lifecycle_attempt_identity_ambiguous",
           "lifecycle_competing_attempt_unresolved", "lifecycle_entry_association_unproven",
           "lifecycle_intent_identity_mismatch", "lifecycle_no_matching_attach_contract",
           "lifecycle_owner_identity_mismatch", "lifecycle_owner_row_proven_missing",
           "positive_orphan_mismatch"),
    "A5": ("exit_quantity_still_at_risk",),
    "A6": ("eligible_opportunity_without_order",),
}
_L3_PAGE = {
    # C1 10-29 09:57 / pkg-1029 C6 binding: classification only, SHADOW stays NONE.
    "B_keepwatch_stuck": ("keepwatch_rehold_or_frozen",),
    "B_bot_reading_lag": ("bot_reading_lag_180s",),
    "B_bar_lag": ("bar_lag_4min",),
    "B_DB_write_error": ("actual_error_count_measured",),
    "B_OMS_stall": ("oms_functional_progress_measured",),
    "B_broker_API_error": ("actual_error_count_measured",),
    "B_checker_blindness": ("checker_coverage_measured",),
    "B_guard_STOP": ("live_guard_stop",),
    "B_quote_stale": ("quote_age_measured",),
    "B_restart_loop": ("restart_delta_measured",),
    "B_scanner_silent": ("scanner_progress_measured",),
    "B_service_down": ("service_down",),
    "B_token_dead_or_near_expiry": ("refresh_expiry_measured", "token_dead"),
}


def _sha(relative: str) -> str:
    return hashlib.sha256((REPO / relative).read_bytes()).hexdigest()


def category_map(trade_sha256: str) -> dict:
    sources = {"L2": (REPO / TRADE_SOURCE).read_text(),
               "L3": (REPO / SYSTEM_RULES).read_text() + (REPO / SYSTEM_SOURCE).read_text()}
    exact = {}
    for lane, table in (("L2", _L2_PAGE), ("L3", _L3_PAGE)):
        for cls, reasons in table.items():
            for reason in reasons:
                if f'"{reason}"' not in sources[lane] and f"'{reason}'" not in sources[lane]:
                    raise ValueError(f"reason code not in {lane} source: {cls}/{reason}")
            exact.setdefault(lane, {})[cls] = {reason: "PAGE" for reason in reasons}
    rule = lambda field: {"category": "PAGE", "reason_field": field, "source_file_sha256": trade_sha256}
    return {"schema_version": "smartalert1.category.v1", "default": "DIGEST", "exact": exact,
            "source_bound_true": {"L2": {"A3": rule("refusal_code"), "A7": rule("block_reason")}}}


def trading_day(day: date) -> bool:
    from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS
    return day.weekday() < 5 and day not in US_MARKET_HOLIDAYS


def close_calendar(first: date, last: date, holiday_sha256: str) -> dict:
    proof = "time-utils-holidays:" + holiday_sha256[:16]
    windows, day = [], first
    while day <= last:
        entry = {"window_id": "extended:" + day.isoformat(), "trading_date": day.isoformat(),
                 "open_session": trading_day(day), "session_proof_ref": proof}
        if entry["open_session"]:
            entry.update(close_kind="EXTENDED",
                start=datetime.combine(day, time(4), ET).isoformat(),
                end=datetime.combine(day, time(20), ET).isoformat())
        windows.append(entry)
        day += timedelta(days=1)
    return {"schema_version": "smartalert1.close-calendar.v1", "timezone": "America/New_York",
            "late_policy": "NO_BACKDATED_DIGEST", "calendar_ref": proof,
            "approval_ref": CATEGORY_MAP_REF, "windows": windows}


def build(*, source_head: str, first: date, last: date, upstream: dict | None = None,
          acquisition_ref: str | None = None, schedule_ref: str | None = None,
          source_closure_ref: str | None = None) -> tuple[dict, dict]:
    """Return (policy, evidence_refs). evidence_refs maps every ref the policy
    cites to the sha256 the C6 manifest must carry for it."""
    trade_sha, system_sha, holiday_sha = _sha(TRADE_SOURCE), _sha(SYSTEM_SOURCE), _sha(HOLIDAY_SOURCE)
    selected = category_map(trade_sha)
    policy = {"schema_version": SCHEMA, "enabled": True, "mode": "SHADOW", "policyversion": POLICYVERSION,
        "approval_ref": APPROVAL_REF, "calibration_ref": CALIBRATION_REF,
        "source_bindings": {"L2": {"source_head": source_head, "file_sha256": trade_sha},
                            "L3": {"source_head": source_head, "file_sha256": system_sha}},
        "upstream_policyversions": dict(UPSTREAM_POLICYVERSIONS), "category_map": selected,
        "required_digest_coverage": list(DIGEST_SCOPES),
        "close_calendar": close_calendar(first, last, holiday_sha)}
    operands = {"acquisition_ref": acquisition_ref, "schedule_ref": schedule_ref,
                "source_closure_ref": source_closure_ref}
    missing = [k for k, v in operands.items() if v is None] + ([] if upstream else ["upstream_policies"])
    review = {"status": "APPROVED_SHADOW_OPERANDS" if not missing else "OPERANDS_UNBOUND:" + ",".join(missing),
              "approval_ref": APPROVAL_REF, "calibration_ref": CALIBRATION_REF,
              "category_map_sha256": fingerprint(selected, FRAME_BYTES)}
    review.update({k: v for k, v in operands.items() if v is not None})
    if upstream:
        for lane in ("L2", "L3"):
            if upstream[lane].get("policyversion") != UPSTREAM_POLICYVERSIONS[lane]:
                raise ValueError(f"upstream {lane} policyversion mismatch")
        review["upstream_policy_sha256"] = {lane: fingerprint(upstream[lane], FRAME_BYTES) for lane in ("L2", "L3")}
    policy["producer_review"] = review
    policy_binding(policy)  # refuse to emit anything delivery would refuse
    calendar = policy["close_calendar"]
    evidence = {APPROVAL_REF: _file_sha(Path.home() / ".claude/projects/-Users-velkris/memory/"
                                        "project_mai_tai_smartalert1_1028.md"),
                calendar["calendar_ref"]: holiday_sha, CATEGORY_MAP_REF: fingerprint(selected, FRAME_BYTES)}
    return policy, {k: v for k, v in sorted(evidence.items()) if v is not None}


def _file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None  # the caller's manifest must supply it; never invented here


def dump(value: dict) -> bytes:
    return encoded(value, FRAME_BYTES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SMARTALERT1 operational SHADOW policy (no phones)")
    parser.add_argument("--source-head", required=True)
    parser.add_argument("--trading-date", help="first date of the calendar (default: today ET)")
    parser.add_argument("--days", type=int, default=14, help="calendar length in days (one shadow campaign)")
    parser.add_argument("--upstream-policies", help="JSON {L2: policy, L3: policy} with measured windows")
    parser.add_argument("--acquisition-ref")
    parser.add_argument("--schedule-ref")
    parser.add_argument("--source-closure-ref")
    parser.add_argument("--out", required=True)
    parser.add_argument("--init-out", help="also write the pristine ledger initialization state here")
    parser.add_argument("--campaign-id", default="smartalert1-shadow")
    args = parser.parse_args(argv)
    first = date.fromisoformat(args.trading_date) if args.trading_date else datetime.now(ET).date()
    upstream = json.loads(Path(args.upstream_policies).read_text()) if args.upstream_policies else None
    policy, evidence = build(source_head=args.source_head, first=first,
        last=first + timedelta(days=args.days - 1), upstream=upstream, acquisition_ref=args.acquisition_ref,
        schedule_ref=args.schedule_ref, source_closure_ref=args.source_closure_ref)
    raw = dump(policy)
    Path(args.out).write_bytes(raw)
    Path(args.out + ".evidence-refs.json").write_bytes(dump(evidence))
    out = {"policy_sha256": hashlib.sha256(raw).hexdigest(), "review_status": policy["producer_review"]["status"]}
    if args.init_out:
        state = initial_state(campaign_id=args.campaign_id, ledger_epoch=args.campaign_id + ":" + first.isoformat(),
                              policy=policy)
        init_raw = dump(state)
        Path(args.init_out).write_bytes(init_raw)
        out["initialization_sha256"] = hashlib.sha256(init_raw).hexdigest()
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
