"""File-only ATR capture denominator; NOT an order-decision or fill replay.

Reuse the reviewed SOURCE21 chunk reader, pinned by file SHA, without modifying
its MOM/ORB owner. No application imports, database, provider or subprocess port.
"""

import argparse
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo


VERSION = "ATR_LIVE_CAPTURE_DENOMINATOR_V1"
MANIFEST_SHA = "c2e0b426792fa3b63f85d2fbd70ef856ce0633fdf9a2bb2c0917fd9758291cbf"
READER_SHA = "024cfffacf98716235793e345e424e4721a761a20e1d16be7001b477fd14583f"
OLD = "9c10a7de1cd2fc7a7df2b0e587fe132d46faa4a7"
NEW = "39e737b728400e559461d190f1bf1e4aaa435691"
APP_EQUIVALENT_NEW = "724d36c5010f3e544e0d29d509060528662f5f82"
ATR = {"schwab_1m_v2", "schwab_1m"}
ET = ZoneInfo("America/New_York")
COUNTS = {"broker_accounts": 12, "strategies": 17, "broker_orders": 85174,
          "fills": 20733, "trade_intents": 155881}


def ns(value):
    """Integer nanoseconds, including fractional cutoff edges; no float clocks."""
    if not isinstance(value, str):
        raise ValueError("timestamp_not_recorded")
    match = re.fullmatch(r"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        raise ValueError("exact_explicit_offset_timestamp_required")
    at = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
    delta = at.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1000000000 + int((match[2] or "").ljust(9, "0"))


def membership(value, cutoff_ns):
    try:
        at = ns(value)
    except ValueError:
        return "UNKNOWN_TIME"
    if at > cutoff_ns:
        return "UNKNOWN_FUTURE_CLOCK"
    return "IN_WINDOW" if at >= ns("2026-10-12T00:00:00-04:00") else "BEFORE_WINDOW"


def clock_group(value):
    try:
        at = datetime.fromtimestamp(ns(value) // 1000000000, UTC).astimezone(ET)
    except (ValueError, OverflowError):
        return "UNKNOWN_DAY", "UNKNOWN_SESSION"
    minute = at.hour * 60 + at.minute
    session = ("PM_0400_0930" if 240 <= minute < 570 else
               "RTH_0930_1600" if 570 <= minute < 960 else
               "AH_1600_2000" if 960 <= minute < 1200 else "OUTSIDE_0400_2000")
    return at.date().isoformat(), session


def retained(row, pointer):
    """Keep exact original JSON text and SQL-NULL annotations, not float re-JSON."""
    texts, nulls = row.get("__l12_json_text"), row.get("__l12_sql_null_columns")
    if not isinstance(texts, dict) or not isinstance(nulls, list):
        raise ValueError("original_json_text_and_sql_null_annotations_required")
    if any(not isinstance(k, str) or (v is not None and not isinstance(v, str))
           for k, v in texts.items()):
        raise ValueError("original_json_text_type")
    return dict(source=pointer,
        scalar_columns={k: v for k, v in row.items()
                        if k not in texts and not k.startswith("__l12_")},
        original_json_text=texts, sql_null_columns=nulls,
        json_text_sha256={k: hashlib.sha256(v.encode()).hexdigest() if v is not None else None
                          for k, v in texts.items()})


def reader(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("unsafe_reader_source")
    if hashlib.sha256(path.read_bytes()).hexdigest() != READER_SHA:
        raise ValueError("frozen_shared_reader_source_changed")
    spec = importlib.util.spec_from_file_location("atr_frozen_source21_reader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Capture


def census(capture):
    cutoff = capture.manifest["asof_ms"]
    if type(cutoff) is not int or cutoff < 0:
        raise ValueError("integer_captured_asof_required")
    cutoff_ns = cutoff * 1000000
    accounts, strategies = {}, {}
    source_refs = {}
    for table, target in (("broker_accounts", accounts), ("strategies", strategies)):
        for row, pointer in capture.rows(table):
            if row["id"] in target:
                raise ValueError("duplicate_scope_identity")
            target[row["id"]] = row
            source_refs[table + ":" + row["id"]] = retained(row, pointer)

    def scope(row):
        account = accounts.get(row.get("broker_account_id"))
        strategy = strategies.get(row.get("strategy_id"))
        if account is None or strategy is None:
            return None
        if strategy["code"] not in ATR:
            return False
        name = account.get("name")
        if isinstance(name, str) and name and not name.startswith("live:"):
            return False
        return account, strategy

    def group(row, clock):
        account, strategy = scope(row)
        day, session = clock_group(row.get(clock))
        return "|".join((day, strategy["code"], account.get("name") or "UNKNOWN_ACCOUNT",
                         account.get("provider") or "UNKNOWN_PROVIDER",
                         session, str(row.get("side"))))

    def live_scope_issue(row, pointer):
        account, strategy = scope(row)
        if (strategy.get("execution_mode") != "live"
                or not isinstance(account.get("name"), str) or not account["name"]
                or account.get("provider") not in {"schwab", "webull"}):
            uncertain.append(dict(id=row["id"], source=pointer,
                                  reason="LIVE_ACCOUNT_STRATEGY_MODE_OR_PROVIDER_UNKNOWN"))

    order_index, atr_orders, selected, uncertain, issues = {}, {}, set(), [], []
    order_counts, fill_counts, executed_orders = Counter(), Counter(), {}
    for row, pointer in capture.rows("broker_orders"):
        order_index[row["id"]] = {k: row.get(k) for k in (
            "id", "intent_id", "broker_account_id", "strategy_id", "symbol", "side")}
        selected_scope = scope(row)
        if selected_scope is None:
            issues.append(dict(table="broker_orders", id=row["id"], reason="SCOPE_FK_UNKNOWN", source=pointer))
        if not selected_scope:
            continue
        live_scope_issue(row, pointer)
        status = membership(row.get("submitted_at"), cutoff_ns)
        atr_orders[row["id"]] = dict(raw=retained(row, pointer), row=row,
                                     submitted_membership=status, fills=[])
        if status != "BEFORE_WINDOW":
            selected.add(row["id"])
            order_counts[status + "|" + group(row, "submitted_at")] += 1
            if status != "IN_WINDOW":
                uncertain.append(dict(table="broker_orders", id=row["id"], reason=status, source=pointer))
    unbound = []
    for row, pointer in capture.rows("fills"):
        selected_scope = scope(row)
        if selected_scope is None:
            issues.append(dict(table="fills", id=row["id"], reason="SCOPE_FK_UNKNOWN", source=pointer))
        if not selected_scope:
            continue
        live_scope_issue(row, pointer)
        status = membership(row.get("filled_at"), cutoff_ns)
        if status == "BEFORE_WINDOW":
            continue
        key = status + "|" + group(row, "filled_at")
        fill_counts[key] += 1
        if status != "IN_WINDOW":
            uncertain.append(dict(table="fills", id=row["id"], reason=status, source=pointer))
        original = order_index.get(row.get("order_id"))
        identity_ok = original is not None and all(row.get(k) == original.get(k)
            for k in ("broker_account_id", "strategy_id", "symbol", "side"))
        item = dict(raw=retained(row, pointer), membership=status)
        if not identity_ok or row["order_id"] not in atr_orders:
            issues.append(dict(table="fills", id=row["id"], reason="FILL_ORDER_IDENTITY_UNKNOWN", source=pointer))
            unbound.append(item)
            continue
        selected.add(row["order_id"])
        atr_orders[row["order_id"]]["fills"].append(item)
        try:
            if isinstance(row["quantity"], bool):
                raise ValueError("boolean_quantity_not_execution")
            quantity = Decimal(row["quantity"])
            eligible_count = quantity.is_finite() and quantity > 0 and row["side"] in {"buy", "sell"}
        except (KeyError, TypeError, ValueError, ArithmeticError):
            eligible_count = False
        if not eligible_count:
            issues.append(dict(table="fills", id=row["id"], reason="POSITIVE_EXECUTED_QUANTITY_OR_SIDE_UNKNOWN", source=pointer))
        if status == "IN_WINDOW" and eligible_count:
            executed_orders.setdefault(key, set()).add(row["order_id"])
    needed = {order_index[k]["intent_id"] for k in selected}
    intents, intent_only, intent_counts = {}, [], Counter()
    linked_intents = {o["intent_id"] for o in order_index.values()}
    for row, pointer in capture.rows("trade_intents"):
        if row["id"] in needed:
            intents[row["id"]] = retained(row, pointer)
        selected_scope = scope(row)
        if selected_scope is None:
            issues.append(dict(table="trade_intents", id=row["id"], reason="SCOPE_FK_UNKNOWN", source=pointer))
        if not selected_scope:
            continue
        live_scope_issue(row, pointer)
        status = membership(row.get("created_at"), cutoff_ns)
        if status == "BEFORE_WINDOW":
            continue
        intent_counts[status + "|" + group(row, "created_at")] += 1
        if row["id"] not in linked_intents:
            intent_only.append(dict(raw=retained(row, pointer), membership=status))
        if status != "IN_WINDOW":
            uncertain.append(dict(table="trade_intents", id=row["id"], reason=status, source=pointer))
    cases = []
    for oid in sorted(selected):
        source = atr_orders[oid]
        row = source["row"]
        intent = intents.get(row["intent_id"])
        if intent is None:
            issues.append(dict(table="broker_orders", id=oid, reason="INTENT_NOT_FOUND"))
        elif any(row.get(k) != intent["scalar_columns"].get(k) for k in (
                "broker_account_id", "strategy_id", "symbol", "side")):
            issues.append(dict(table="broker_orders", id=oid, reason="INTENT_IDENTITY_CONFLICT"))
        account, strategy = scope(row)
        cases.append(dict(order=source["raw"], intent=intent, fills=source["fills"],
            order_id=oid, bot=strategy["code"], account=account.get("name"), provider=account.get("provider"),
            strategy_mode=strategy["execution_mode"], submitted_membership=source["submitted_membership"],
            prewindow_parent_retained=source["submitted_membership"] == "BEFORE_WINDOW",
            original_dispatch_provenance="UNKNOWN_NOT_PROVEN_BY_ATR_LABEL_OR_RECONSTRUCTED_ORDER",
            historical_reproduction="UNRUN_NOT_INFERRED_FROM_RECORDED_FILLS"))
    if capture.counts != COUNTS:
        raise ValueError("exact_five_source_table_counts_required")
    return dict(version=VERSION, old_head=OLD, new_head=NEW,
        new_app_equivalent_head=APP_EQUIVALENT_NEW,
        manifest_sha256=MANIFEST_SHA, shared_reader_sha256=READER_SHA,
        capture_asof_ms=cutoff, capture_cutoff_ns=cutoff_ns,
        since="2026-10-12T00:00:00-04:00", current_completeness="UNKNOWN_AFTER_CAPTURE_CUTOFF",
        source_counts=capture.counts, scope_rows=source_refs,
        order_request_counts=dict(sorted(order_counts.items())),
        fill_row_counts=dict(sorted(fill_counts.items())),
        filled_order_counts_by_fill_session={k: len(v) for k, v in sorted(executed_orders.items())},
        intent_counts=dict(sorted(intent_counts.items())),
        order_count=len(cases), cases=cases, intent_only_rows=intent_only,
        unbound_fills=unbound, identity_issues=issues, uncertain_membership=uncertain,
        actual_orders_reproduced=0, actual_fills_reproduced=0,
        old_equals_new="UNRUN", changed_order_count=None, historical_replay_qualified=False,
        limitations=["FILLS_AND_ORDERS_ARE_EVIDENCE_NOT_DECISION_REPLAY",
            "ATR_LABELLED_REPAIR_OR_MANUAL_ORDER_NOT_PROVEN_ORIGINAL_BOT_DISPATCH",
            "SESSION_EXECUTED_ORDER_COUNTS_NOT_ADDITIVE_ACROSS_PARTIAL_FILL_SESSIONS",
            "ORIGINAL_NATIVE_ASOF_QUOTE_CLOCK_OWNER_INPUTS_MUST_BE_BOUND_SEPARATELY"])


def write_new(path, result):
    path = Path(path)
    if (not path.is_absolute() or not path.is_relative_to("/private/tmp")
            or path.parent.resolve(strict=True) != path.parent
            or path.parent.stat().st_uid != os.getuid()
            or path.parent.stat().st_mode & 0o777 != 0o700):
        raise ValueError("fresh_private_0700_parent_required")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("census",))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--parts", type=Path, required=True)
    parser.add_argument("--reader-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    capture = reader(args.reader_source)(args.manifest, args.parts, expected_sha=MANIFEST_SHA)
    write_new(args.output, census(capture))


if __name__ == "__main__":
    main()
