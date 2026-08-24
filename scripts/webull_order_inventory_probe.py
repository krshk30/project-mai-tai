#!/usr/bin/env python3
"""Read-only Webull order-history inventory probe.

This program can call only two SDK methods: ``get_order_history`` and
``get_order_detail``. It never previews, places, replaces, or cancels an order.

The five 2026-08-21 combo IDs below are a positive control recovered from the
OMS log. A complete sweep is VOID unless every control is present in the
*history sweep itself* with its expected symbol and at least two child orders.
The independently corroborated USDE control must also carry stop 7.5905. An
incomplete sweep is COULD_NOT_TELL because the control never received a valid assay.

Verdicts are deliberately four-state:

* FOUND
* CONFIRMED_ABSENT_VIA_DETAIL
* COULD_NOT_TELL
* VOID -- the known-positive control did not reproduce

The listing is never allowed to prove absence by itself. Every requested ID
missing from a *complete* history sweep gets a detail query. Because the SDK
detail method accepts ``client_order_id``, ORDER_NOT_FOUND is authoritative only
for a target declared as a client ID; it cannot prove a broker ``combo_order_id``
absent.

The probe emits JSON Lines to stdout, including every raw response. Redirect or
tee stdout on the box to preserve an evidence artifact. It never prints Webull
credentials or the full account ID.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import importlib.metadata
import json
import time
from typing import Any, Protocol


CONTROL_DATE = date(2026, 8, 21)
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 100
# One request every 2.1s uses at most half of Webull's 2 requests / 2 seconds quota. That
# avoids probe-created bursts and leaves one nominal slot for the running OMS/status poller.
REQUEST_SPACING_SECONDS = 2.1


class Verdict(str, Enum):
    FOUND = "FOUND"
    CONFIRMED_ABSENT = "CONFIRMED_ABSENT_VIA_DETAIL"
    COULD_NOT_TELL = "COULD_NOT_TELL"
    VOID = "VOID"


EXIT_CODES = {
    Verdict.FOUND: 0,
    Verdict.CONFIRMED_ABSENT: 3,
    Verdict.COULD_NOT_TELL: 2,
    Verdict.VOID: 4,
}


class IdentifierKind(str, Enum):
    CLIENT_ORDER_ID = "client_order_id"
    COMBO_ORDER_ID = "combo_order_id"


@dataclass(frozen=True)
class Target:
    identifier: str
    kind: IdentifierKind
    label: str
    expected_symbol: str | None = None
    expected_stop: Decimal | None = None
    is_control: bool = False


KNOWN_POSITIVES = (
    Target(
        "31IUL7OCV3K860JRGF0LLE4MI8",
        IdentifierKind.COMBO_ORDER_ID,
        "2026-08-21 SUGP exit pair",
        expected_symbol="SUGP",
        is_control=True,
    ),
    Target(
        "NVHC4FQV179G0KKQS0GAPMA4EA",
        IdentifierKind.COMBO_ORDER_ID,
        "2026-08-21 JUNS exit pair",
        expected_symbol="JUNS",
        is_control=True,
    ),
    Target(
        "JMH2DE9M85S48LBG3IU5HORI4B",
        IdentifierKind.COMBO_ORDER_ID,
        "2026-08-21 USDE exit pair 1",
        expected_symbol="USDE",
        is_control=True,
    ),
    Target(
        "6THU0AUEPQJG6J9ISV6I50GHA9",
        IdentifierKind.COMBO_ORDER_ID,
        "2026-08-21 EXYN exit pair",
        expected_symbol="EXYN",
        is_control=True,
    ),
    Target(
        "VHGU4AR1TEVN2QSSSDAEFAQP09",
        IdentifierKind.COMBO_ORDER_ID,
        "2026-08-21 USDE exit pair 2; broker-screen corroborated",
        expected_symbol="USDE",
        expected_stop=Decimal("7.5905"),
        is_control=True,
    ),
)


IDENTIFIER_KEYS = {
    "client_order_id",
    "clientOrderId",
    "combo_order_id",
    "comboOrderId",
    "order_id",
    "orderId",
}


class ShapeError(RuntimeError):
    pass


class CallFailure(RuntimeError):
    def __init__(
        self,
        endpoint: str,
        reason: str,
        *,
        code: str = "",
        order_not_found: bool = False,
    ) -> None:
        super().__init__(reason)
        self.endpoint = endpoint
        self.reason = reason
        self.code = code
        self.order_not_found = order_not_found


@dataclass(frozen=True)
class ParsedPage:
    groups: list[dict[str, Any]]
    order_records: list[dict[str, Any]]
    client_order_ids: list[str]
    cursor_out: str | None


@dataclass
class HistorySweep:
    complete: bool
    pages: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    order_records: list[dict[str, Any]]
    error: str | None = None


class Reader(Protocol):
    def get_history(
        self,
        *,
        page_size: int,
        start_date: str,
        end_date: str,
        last_client_order_id: str | None,
    ) -> object: ...

    def get_detail(self, client_order_id: str) -> object: ...

    def cost(self) -> dict[str, Any]: ...


def emit_json(event: dict[str, Any]) -> None:
    print(json.dumps(event, default=str, sort_keys=True, separators=(",", ":")), flush=True)


def walk_dicts(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def identifiers_in(value: object) -> set[str]:
    identifiers: set[str] = set()
    for node in walk_dicts(value):
        for key in IDENTIFIER_KEYS:
            raw = node.get(key)
            if raw not in (None, ""):
                identifiers.add(str(raw))
    return identifiers


def parse_history_page(body: object) -> ParsedPage:
    if not isinstance(body, list):
        raise ShapeError(
            "history body is not the documented top-level array "
            f"(received {type(body).__name__})"
        )

    groups: list[dict[str, Any]] = []
    order_records: list[dict[str, Any]] = []
    for index, raw_group in enumerate(body):
        if not isinstance(raw_group, dict):
            raise ShapeError(f"history group {index} is not an object")
        orders = raw_group.get("orders")
        if not isinstance(orders, list):
            raise ShapeError(f"history group {index} has no orders array")
        if not orders:
            raise ShapeError(f"history group {index} has an empty orders array")
        if any(not isinstance(order, dict) for order in orders):
            raise ShapeError(f"history group {index} contains a non-object order")
        groups.append(raw_group)
        order_records.extend(orders)

    client_order_ids = [
        str(order["client_order_id"])
        for order in order_records
        if order.get("client_order_id") not in (None, "")
    ]
    cursor_out = None
    if order_records:
        last_record_cursor = order_records[-1].get("client_order_id")
        if last_record_cursor not in (None, ""):
            cursor_out = str(last_record_cursor)
    return ParsedPage(groups, order_records, client_order_ids, cursor_out)


def enumerate_history(
    reader: Reader,
    *,
    start_date: str,
    end_date: str,
    page_size: int,
    max_pages: int,
    emit: Callable[[dict[str, Any]], None] = emit_json,
) -> HistorySweep:
    pages: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    seen_client_order_ids: set[str] = set()
    seen_page_fingerprints: set[tuple[str, ...]] = set()
    used_cursors: set[str] = set()
    cursor: str | None = None

    for page_number in range(1, max_pages + 1):
        try:
            body = reader.get_history(
                page_size=page_size,
                start_date=start_date,
                end_date=end_date,
                last_client_order_id=cursor,
            )
        except CallFailure as exc:
            error = f"history request failed: code={exc.code or 'unknown'} reason={exc.reason}"
            emit({"event": "history_failure", "page": page_number, "error": error})
            return HistorySweep(False, pages, groups, records, error)

        emit({"event": "history_page_raw", "page": page_number, "body": body})
        try:
            parsed = parse_history_page(body)
        except ShapeError as exc:
            error = f"page {page_number} shape error: {exc}"
            emit({"event": "history_shape_failure", "page": page_number, "error": error})
            return HistorySweep(False, pages, groups, records, error)

        fingerprint = tuple(sorted(identifiers_in(parsed.groups)))
        if fingerprint in seen_page_fingerprints and fingerprint:
            error = f"page {page_number} repeated an earlier page fingerprint"
            emit({"event": "pagination_failure", "page": page_number, "error": error})
            return HistorySweep(False, pages, groups, records, error)
        seen_page_fingerprints.add(fingerprint)

        duplicate_client_ids = seen_client_order_ids.intersection(parsed.client_order_ids)
        if duplicate_client_ids:
            error = (
                f"page {page_number} repeated client_order_id values across pages: "
                f"{sorted(duplicate_client_ids)}"
            )
            emit({"event": "pagination_failure", "page": page_number, "error": error})
            return HistorySweep(False, pages, groups, records, error)
        seen_client_order_ids.update(parsed.client_order_ids)

        record_count = len(parsed.order_records)
        is_short = record_count < page_size
        page_evidence = {
            "page": page_number,
            "page_size": page_size,
            "group_count": len(parsed.groups),
            "order_record_count": record_count,
            "count_vs_page_size": "SHORT_TERMINAL" if is_short else "AT_OR_ABOVE_CONTINUE",
            "cursor_in": cursor,
            "cursor_out": parsed.cursor_out,
        }
        pages.append(page_evidence)
        groups.extend(parsed.groups)
        records.extend(parsed.order_records)
        emit({"event": "history_page", **page_evidence})

        if is_short:
            return HistorySweep(True, pages, groups, records)
        if parsed.cursor_out is None:
            error = f"page {page_number} was not short but has no last child client_order_id cursor"
            emit({"event": "pagination_failure", "page": page_number, "error": error})
            return HistorySweep(False, pages, groups, records, error)
        if parsed.cursor_out == cursor or parsed.cursor_out in used_cursors:
            error = f"page {page_number} produced a repeated cursor {parsed.cursor_out}"
            emit({"event": "pagination_failure", "page": page_number, "error": error})
            return HistorySweep(False, pages, groups, records, error)
        used_cursors.add(parsed.cursor_out)
        cursor = parsed.cursor_out

    error = f"reached max_pages={max_pages} without a short terminal page"
    emit({"event": "pagination_failure", "page": max_pages, "error": error})
    return HistorySweep(False, pages, groups, records, error)


def groups_matching(groups: Iterable[dict[str, Any]], identifier: str) -> list[dict[str, Any]]:
    return [group for group in groups if identifier in identifiers_in(group)]


def strings_for_key(value: object, key: str) -> set[str]:
    return {
        str(node[key])
        for node in walk_dicts(value)
        if node.get(key) not in (None, "")
    }


def decimals_for_key(value: object, key: str) -> set[Decimal]:
    values: set[Decimal] = set()
    for node in walk_dicts(value):
        raw = node.get(key)
        if raw in (None, ""):
            continue
        try:
            values.add(Decimal(str(raw)))
        except (InvalidOperation, ValueError):
            continue
    return values


def order_records_in(groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for group in groups:
        orders = group.get("orders")
        if isinstance(orders, list):
            records.extend(order for order in orders if isinstance(order, dict))
    return records


def history_evidence(matches: list[dict[str, Any]]) -> dict[str, Any]:
    records = order_records_in(matches)
    return {
        "observed_symbols": sorted(
            {symbol.upper() for symbol in strings_for_key(matches, "symbol")}
        ),
        "observed_combo_types": sorted(strings_for_key(matches, "combo_type")),
        "observed_statuses": sorted(strings_for_key(records, "status")),
        "observed_stop_prices": sorted(
            str(stop) for stop in decimals_for_key(matches, "stop_price")
        ),
        "observed_place_times": sorted(
            strings_for_key(records, "place_time_at")
            | strings_for_key(records, "place_time")
        ),
        "observed_fill_times": sorted(
            strings_for_key(records, "filled_time_at")
            | strings_for_key(records, "filled_time")
        ),
        "child_client_order_ids": sorted(strings_for_key(records, "client_order_id")),
        "child_broker_order_ids": sorted(strings_for_key(records, "order_id")),
        "total_quantities": sorted(strings_for_key(records, "total_quantity")),
        "filled_quantities": sorted(strings_for_key(records, "filled_quantity")),
    }


def validate_history_match(target: Target, matches: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    symbols = {symbol.upper() for symbol in strings_for_key(matches, "symbol")}
    records = order_records_in(matches)
    if target.expected_symbol and target.expected_symbol.upper() not in symbols:
        failures.append(
            f"expected symbol {target.expected_symbol} not present (observed={sorted(symbols)})"
        )
    if target.is_control and len(records) < 2:
        failures.append(f"known exit pair exposed only {len(records)} child order record(s)")
    if target.expected_stop is not None:
        stops = decimals_for_key(matches, "stop_price")
        if target.expected_stop not in stops:
            failures.append(
                f"expected stop {target.expected_stop} not present "
                f"(observed={sorted(str(stop) for stop in stops)})"
            )
    return not failures, failures


def _detail_result(
    reader: Reader,
    target: Target,
    *,
    emit: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    try:
        body = reader.get_detail(target.identifier)
    except CallFailure as exc:
        if exc.order_not_found and target.kind == IdentifierKind.CLIENT_ORDER_ID:
            return {
                "verdict": Verdict.CONFIRMED_ABSENT.value,
                "source": "detail",
                "reason": "detail returned authoritative ORDER_NOT_FOUND for client_order_id",
                "detail_code": exc.code,
            }
        namespace_note = ""
        if exc.order_not_found and target.kind == IdentifierKind.COMBO_ORDER_ID:
            namespace_note = (
                "; ORDER_NOT_FOUND cannot confirm absence because SDK detail accepts "
                "client_order_id, not combo_order_id"
            )
        return {
            "verdict": Verdict.COULD_NOT_TELL.value,
            "source": "detail",
            "reason": f"detail failed: code={exc.code or 'unknown'} {exc.reason}{namespace_note}",
        }

    emit({"event": "detail_raw", "target": target.identifier, "body": body})
    if target.identifier in identifiers_in(body):
        return {
            "verdict": Verdict.FOUND.value,
            "source": "detail",
            "reason": "detail response echoed the requested identifier",
        }
    return {
        "verdict": Verdict.COULD_NOT_TELL.value,
        "source": "detail",
        "reason": "detail returned success but did not echo the requested identifier",
    }


def evaluate_targets(
    reader: Reader,
    sweep: HistorySweep,
    targets: Iterable[Target],
    *,
    emit: Callable[[dict[str, Any]], None] = emit_json,
) -> tuple[list[dict[str, Any]], bool]:
    results: list[dict[str, Any]] = []
    controls_reproduced = True
    page_count = len(sweep.pages)

    for target in targets:
        matches = groups_matching(sweep.groups, target.identifier)
        history_valid = False
        match_failures: list[str] = []
        if matches:
            history_valid, match_failures = validate_history_match(target, matches)

        if history_valid:
            result = {
                "identifier": target.identifier,
                "identifier_kind": target.kind.value,
                "label": target.label,
                "is_control": target.is_control,
                "verdict": Verdict.FOUND.value,
                "source": "history",
                "history_group_occurrences": len(matches),
                "history_child_orders": len(order_records_in(matches)),
                "history_pages": page_count,
                "reason": "identifier and expected fixture fields reproduced in history",
                "history_evidence": history_evidence(matches),
            }
        elif matches:
            result = {
                "identifier": target.identifier,
                "identifier_kind": target.kind.value,
                "label": target.label,
                "is_control": target.is_control,
                "verdict": Verdict.COULD_NOT_TELL.value,
                "source": "history",
                "history_group_occurrences": len(matches),
                "history_child_orders": len(order_records_in(matches)),
                "history_pages": page_count,
                "reason": "; ".join(match_failures),
                "history_evidence": history_evidence(matches),
            }
        elif not sweep.complete:
            result = {
                "identifier": target.identifier,
                "identifier_kind": target.kind.value,
                "label": target.label,
                "is_control": target.is_control,
                "verdict": Verdict.COULD_NOT_TELL.value,
                "source": "history",
                "history_group_occurrences": 0,
                "history_child_orders": 0,
                "history_pages": page_count,
                "reason": "history sweep was incomplete, so absence was not established",
            }
        else:
            # A miss in a complete listing never proves absence. Detail is mandatory.
            result = {
                "identifier": target.identifier,
                "identifier_kind": target.kind.value,
                "label": target.label,
                "is_control": target.is_control,
                "history_group_occurrences": 0,
                "history_child_orders": 0,
                "history_pages": page_count,
                **_detail_result(reader, target, emit=emit),
            }

        if target.is_control and not history_valid and sweep.complete:
            controls_reproduced = False
            result["control_failure"] = (
                "known positive did not reproduce in the history sweep; detail cannot rescue control"
            )
        elif target.is_control and not history_valid:
            controls_reproduced = False
            result["control_unresolved"] = "history sweep was incomplete"
        results.append(result)
        emit({"event": "target_verdict", **result})

    return results, controls_reproduced


def assess_history_source(
    sweep: HistorySweep,
    target_results: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    controls = [result for result in target_results if result.get("is_control")]
    controls_reproduced = bool(controls) and all(
        result.get("source") == "history" and result.get("verdict") == Verdict.FOUND.value
        for result in controls
    )
    combo_children_visible = controls_reproduced and all(
        int(result.get("history_child_orders", 0)) >= 2 for result in controls
    )

    missing_fields: dict[str, list[str]] = {}
    partial_fill_rows = 0
    for result in controls:
        matches = groups_matching(sweep.groups, str(result["identifier"]))
        for index, record in enumerate(order_records_in(matches), start=1):
            required_fields = {
                "client_order_id",
                "order_id",
                "symbol",
                "status",
                "total_quantity",
            }
            missing = sorted(field for field in required_fields if record.get(field) in (None, ""))
            if record.get("place_time_at") in (None, "") and record.get("place_time") in (None, ""):
                missing.append("place_time_at|place_time")
            status = str(record.get("status") or "").upper()
            if status == "PARTIAL_FILLED":
                partial_fill_rows += 1
            if status in {"PARTIAL_FILLED", "FILLED"}:
                for field in ("filled_quantity", "filled_price"):
                    if record.get(field) in (None, ""):
                        missing.append(field)
                if (
                    record.get("filled_time_at") in (None, "")
                    and record.get("filled_time") in (None, "")
                ):
                    missing.append("filled_time_at|filled_time")
            if missing:
                missing_fields[f"{result['identifier']}#{index}"] = sorted(missing)

    return {
        "pagination_complete_on_short_page": sweep.complete,
        "known_positive_controls_reproduced": controls_reproduced,
        "combo_children_visible": combo_children_visible,
        "identity_status_quantity_fields_complete": not missing_fields,
        "missing_required_fields": missing_fields,
        "partial_fill_rows_observed": partial_fill_rows,
        "freshness_bound_proven": False,
        "partial_fill_semantics_proven": False,
        "trustworthy_as_live_reconciliation_source": False,
        "why_not_yet": [
            "the fixed 2026-08-21 control measures historical coverage, not current list lag",
            "no known partial fill is supplied to prove cumulative quantity/status semantics",
        ],
    }


def choose_overall_verdict(
    *,
    sweep: HistorySweep,
    target_results: Iterable[dict[str, Any]],
    controls_reproduced: bool,
) -> Verdict:
    results = list(target_results)
    if not sweep.complete:
        return Verdict.COULD_NOT_TELL
    if not controls_reproduced:
        return Verdict.VOID
    if any(result["verdict"] == Verdict.COULD_NOT_TELL.value for result in results):
        return Verdict.COULD_NOT_TELL
    if any(result["verdict"] == Verdict.CONFIRMED_ABSENT.value for result in results):
        return Verdict.CONFIRMED_ABSENT
    return Verdict.FOUND


def run_probe(
    reader: Reader,
    *,
    start_date: str,
    end_date: str,
    page_size: int,
    max_pages: int,
    extra_targets: Iterable[Target] = (),
    emit: Callable[[dict[str, Any]], None] = emit_json,
) -> dict[str, Any]:
    targets = [*KNOWN_POSITIVES, *extra_targets]
    sweep = enumerate_history(
        reader,
        start_date=start_date,
        end_date=end_date,
        page_size=page_size,
        max_pages=max_pages,
        emit=emit,
    )
    target_results, controls_reproduced = evaluate_targets(reader, sweep, targets, emit=emit)
    verdict = choose_overall_verdict(
        sweep=sweep,
        target_results=target_results,
        controls_reproduced=controls_reproduced,
    )
    source_assessment = assess_history_source(sweep, target_results)
    report = {
        "event": "final",
        "verdict": verdict.value,
        "exit_code": EXIT_CODES[verdict],
        "history_complete": sweep.complete,
        "history_error": sweep.error,
        "history_pages": len(sweep.pages),
        "history_groups": len(sweep.groups),
        "history_order_records": len(sweep.order_records),
        "page_size": page_size,
        "targets": target_results,
        "request_cost": reader.cost(),
        "history_source_assessment": source_assessment,
    }
    emit(report)
    return report


class RequestPacer:
    def __init__(
        self,
        *,
        spacing_seconds: float = REQUEST_SPACING_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.spacing_seconds = spacing_seconds
        self.monotonic = monotonic
        self.sleep = sleep
        self.request_count = 0
        self.slept_seconds = 0.0
        self.first_call_at: float | None = None
        self.last_call_at: float | None = None

    def before_call(self) -> None:
        now = self.monotonic()
        if self.last_call_at is not None:
            wait = self.spacing_seconds - (now - self.last_call_at)
            if wait > 0:
                self.sleep(wait)
                self.slept_seconds += wait
                now = self.monotonic()
        if self.first_call_at is None:
            self.first_call_at = now
        self.last_call_at = now
        self.request_count += 1

    def snapshot(self) -> dict[str, Any]:
        elapsed = 0.0
        if self.first_call_at is not None:
            elapsed = self.monotonic() - self.first_call_at
        return {
            "total_requests": self.request_count,
            "minimum_spacing_seconds": self.spacing_seconds,
            "pacing_floor_seconds": max(0, self.request_count - 1) * self.spacing_seconds,
            "actual_pacing_sleep_seconds": round(self.slept_seconds, 3),
            "elapsed_since_first_request_seconds": round(elapsed, 3),
        }


def _exception_code(exc: BaseException) -> str:
    for attr in ("code", "error_code", "status_code"):
        value = getattr(exc, attr, None)
        if value not in (None, ""):
            return str(value)
    getter = getattr(exc, "get_error_code", None)
    if callable(getter):
        try:
            value = getter()
            if value not in (None, ""):
                return str(value)
        except Exception:  # noqa: BLE001
            pass
    return ""


def _exception_reason(exc: BaseException) -> str:
    value = getattr(exc, "error_msg", None)
    if value in (None, ""):
        getter = getattr(exc, "get_error_msg", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:  # noqa: BLE001
                value = None
    text = str(value if value not in (None, "") else exc)
    return " ".join(text.split())[:500]


def _contains_order_not_found(value: object) -> bool:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return "ORDER_NOT_FOUND" in text.upper()


class WebullSdkReader:
    def __init__(self, adapter: object, account_id: str, pacer: RequestPacer) -> None:
        from webull.trade.trade.v3.order_opration_v3 import OrderOperationV3

        self.adapter = adapter
        self.account_id = account_id
        self.pacer = pacer
        self.operation = OrderOperationV3(adapter._get_client())
        self.history_requests = 0
        self.detail_requests = 0

    def _call(self, endpoint: str, call: Callable[[], object]) -> object:
        self.pacer.before_call()
        try:
            response = call()
        except Exception as exc:  # noqa: BLE001 -- SDK exceptions are deliberately normalized
            code = _exception_code(exc)
            reason = _exception_reason(exc)
            raise CallFailure(
                endpoint,
                reason,
                code=code,
                order_not_found=(
                    "ORDER_NOT_FOUND" in code.upper() or "ORDER_NOT_FOUND" in reason.upper()
                ),
            ) from exc

        status = self.adapter._response_status(response)
        body = self.adapter._body(response)
        if not 200 <= status < 300:
            raise CallFailure(
                endpoint,
                f"HTTP {status}",
                code=str(status),
                order_not_found=_contains_order_not_found(body),
            )
        if _contains_order_not_found(body):
            raise CallFailure(
                endpoint,
                "response body reports ORDER_NOT_FOUND",
                code="ORDER_NOT_FOUND",
                order_not_found=True,
            )
        return body

    def get_history(
        self,
        *,
        page_size: int,
        start_date: str,
        end_date: str,
        last_client_order_id: str | None,
    ) -> object:
        self.history_requests += 1
        return self._call(
            "history",
            lambda: self.operation.get_order_history(
                self.account_id,
                page_size=page_size,
                start_date=start_date,
                end_date=end_date,
                last_client_order_id=last_client_order_id,
            ),
        )

    def get_detail(self, client_order_id: str) -> object:
        self.detail_requests += 1
        return self._call(
            "detail",
            lambda: self.operation.get_order_detail(self.account_id, client_order_id),
        )

    def cost(self) -> dict[str, Any]:
        return {
            **self.pacer.snapshot(),
            "history_requests": self.history_requests,
            "detail_requests": self.detail_requests,
            "formula": "total = history pages requested + one detail request per history miss",
        }


def _masked_account_id(account_id: str) -> str:
    return f"...{account_id[-4:]}" if len(account_id) >= 4 else "(configured)"


def build_live_reader(account_name: str) -> tuple[WebullSdkReader, dict[str, Any]]:
    from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
    from project_mai_tai.settings import get_settings

    settings = get_settings()
    account_id = str(settings.webull_account_id or "").strip()
    if not account_id:
        raise RuntimeError("MAI_TAI_WEBULL_ACCOUNT_ID is not configured")
    adapter = WebullBrokerAdapter(
        settings,
        accounts_by_name={account_name: WebullAccountConfig(account_id=account_id)},
    )
    reader = WebullSdkReader(adapter, account_id, RequestPacer())
    try:
        sdk_version = importlib.metadata.version("webull-openapi-python-sdk")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = "unknown"
    identity = {
        "account_name": account_name,
        "account_id_masked": _masked_account_id(account_id),
        "host": adapter.host or "SDK default",
        "region": adapter.region_id,
        "sdk_version": sdk_version,
    }
    return reader, identity


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default="live:orb")
    parser.add_argument("--start-date", type=parse_iso_date, default=CONTROL_DATE)
    parser.add_argument("--end-date", type=parse_iso_date, default=CONTROL_DATE)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument(
        "--target-client-id",
        action="append",
        default=[],
        help="additional client_order_id to find or confirm absent through Order Detail",
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    if not 1 <= args.page_size <= 100:
        parser.error("--page-size must be between 1 and 100")
    if args.max_pages < 1:
        parser.error("--max-pages must be positive")
    if args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    if not args.start_date <= CONTROL_DATE <= args.end_date:
        parser.error("date window must include the known-positive control date 2026-08-21")

    extra_targets = [
        Target(
            str(identifier).strip(),
            IdentifierKind.CLIENT_ORDER_ID,
            "operator-supplied client order ID",
        )
        for identifier in args.target_client_id
        if str(identifier).strip()
    ]
    maximum_requests = args.max_pages + len(KNOWN_POSITIVES) + len(extra_targets)
    emit_json(
        {
            "event": "contract",
            "started_at": datetime.now(UTC).isoformat(),
            "read_only": True,
            "sdk_methods_allowed": ["get_order_history", "get_order_detail"],
            "verdicts": [verdict.value for verdict in Verdict],
            "start_date": args.start_date.isoformat(),
            "end_date": args.end_date.isoformat(),
            "page_size": args.page_size,
            "max_pages": args.max_pages,
            "known_positive_controls": [asdict(target) for target in KNOWN_POSITIVES],
            "rate_plan": {
                "venue_limit": "2 requests per 2 seconds",
                "enforced_spacing_seconds": REQUEST_SPACING_SECONDS,
                "cost_formula": "P history pages + M detail confirmations for M history misses",
                "expected_control_path": "P history requests; M=0 if all controls reproduce",
                "configured_maximum_requests": maximum_requests,
                "configured_maximum_pacing_floor_seconds": (
                    max(0, maximum_requests - 1) * REQUEST_SPACING_SECONDS
                ),
            },
        }
    )

    try:
        reader, identity = build_live_reader(args.account)
    except Exception as exc:  # noqa: BLE001 -- setup failure is an explicit couldn't-tell verdict
        reason = f"{type(exc).__name__}: {_exception_reason(exc)}"
        emit_json(
            {
                "event": "final",
                "verdict": Verdict.COULD_NOT_TELL.value,
                "exit_code": EXIT_CODES[Verdict.COULD_NOT_TELL],
                "reason": f"probe setup failed: {reason}",
                "request_cost": {"total_requests": 0},
            }
        )
        return EXIT_CODES[Verdict.COULD_NOT_TELL]

    emit_json({"event": "reader_identity", **identity})
    report = run_probe(
        reader,
        start_date=args.start_date.isoformat(),
        end_date=args.end_date.isoformat(),
        page_size=args.page_size,
        max_pages=args.max_pages,
        extra_targets=extra_targets,
    )
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
