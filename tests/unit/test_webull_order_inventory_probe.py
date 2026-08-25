from __future__ import annotations

from collections import deque
import ast
import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/webull_order_inventory_probe.py"
SPEC = importlib.util.spec_from_file_location("webull_order_inventory_probe", SCRIPT)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def _control_group(target, sequence: int):
    stop = str(target.expected_stop or probe.Decimal("1.23"))
    return {
        "combo_order_id": target.identifier,
        "combo_type": "OCO",
        "orders": [
            {
                "client_order_id": f"fixture-{sequence}-T",
                "order_id": f"broker-{sequence}-T",
                "symbol": target.expected_symbol,
                "status": "CANCELLED",
                "order_type": "LIMIT",
                "total_quantity": "10",
                "filled_quantity": "0",
                "place_time_at": "2026-08-21T17:00:00.000Z",
            },
            {
                "client_order_id": f"fixture-{sequence}-S",
                "order_id": f"broker-{sequence}-S",
                "symbol": target.expected_symbol,
                "status": "SUBMITTED",
                "order_type": "STOP_LOSS",
                "total_quantity": "10",
                "filled_quantity": "0",
                "stop_price": stop,
                "place_time_at": "2026-08-21T17:00:00.000Z",
            },
        ],
    }


def _all_controls():
    return [_control_group(target, index) for index, target in enumerate(probe.KNOWN_POSITIVES)]


class FakeReader:
    def __init__(self, pages, details=None):
        self.pages = deque(pages)
        self.details = details or {}
        self.history_calls = []
        self.detail_calls = []

    def get_history(self, **kwargs):
        self.history_calls.append(kwargs)
        value = self.pages.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    def get_detail(self, client_order_id):
        self.detail_calls.append(client_order_id)
        value = self.details[client_order_id]
        if isinstance(value, Exception):
            raise value
        return value

    def cost(self):
        return {
            "history_requests": len(self.history_calls),
            "detail_requests": len(self.detail_calls),
            "total_requests": len(self.history_calls) + len(self.detail_calls),
        }


def _run(reader, *, page_size=100, max_pages=10, extra_targets=()):
    events = []
    report = probe.run_probe(
        reader,
        start_date="2026-08-21",
        end_date="2026-08-21",
        page_size=page_size,
        max_pages=max_pages,
        extra_targets=extra_targets,
        emit=events.append,
    )
    return report, events


def test_all_five_controls_make_complete_short_sweep_found():
    reader = FakeReader([_all_controls()])

    report, _events = _run(reader)

    assert report["verdict"] == probe.Verdict.FOUND.value
    assert report["history_pages"] == 1
    assert report["history_groups"] == 5
    assert report["history_order_records"] == 10
    assert reader.detail_calls == []


def test_missing_known_positive_is_void_even_if_detail_finds_it():
    missing = probe.KNOWN_POSITIVES[-1]
    history = _all_controls()[:-1]
    detail = _control_group(missing, 99)
    reader = FakeReader([history], {missing.identifier: detail})

    report, _events = _run(reader)

    assert report["verdict"] == probe.Verdict.VOID.value
    assert reader.detail_calls == [missing.identifier]
    result = report["targets"][-1]
    assert result["verdict"] == probe.Verdict.FOUND.value
    assert "cannot rescue control" in result["control_failure"]


def test_missing_combo_detail_not_found_cannot_confirm_wrong_namespace():
    missing = probe.KNOWN_POSITIVES[-1]
    history = _all_controls()[:-1]
    failure = probe.CallFailure(
        "detail", "ORDER_NOT_FOUND", code="ORDER_NOT_FOUND", order_not_found=True
    )
    reader = FakeReader([history], {missing.identifier: failure})

    report, _events = _run(reader)

    assert report["verdict"] == probe.Verdict.VOID.value
    assert report["targets"][-1]["verdict"] == probe.Verdict.COULD_NOT_TELL.value
    assert "not combo_order_id" in report["targets"][-1]["reason"]


def test_client_id_absence_requires_authoritative_detail_not_found():
    target = probe.Target(
        "operator-client-id",
        probe.IdentifierKind.CLIENT_ORDER_ID,
        "operator target",
    )
    failure = probe.CallFailure(
        "detail", "ORDER_NOT_FOUND", code="ORDER_NOT_FOUND", order_not_found=True
    )
    reader = FakeReader([_all_controls()], {target.identifier: failure})

    report, _events = _run(reader, extra_targets=[target])

    assert report["verdict"] == probe.Verdict.CONFIRMED_ABSENT.value
    assert report["exit_code"] == 3
    assert report["targets"][-1]["source"] == "detail"


def test_detail_transport_failure_is_could_not_tell_not_absent():
    target = probe.Target(
        "operator-client-id",
        probe.IdentifierKind.CLIENT_ORDER_ID,
        "operator target",
    )
    failure = probe.CallFailure("detail", "timeout", code="TIMEOUT")
    reader = FakeReader([_all_controls()], {target.identifier: failure})

    report, _events = _run(reader, extra_targets=[target])

    assert report["verdict"] == probe.Verdict.COULD_NOT_TELL.value
    assert "timeout" in report["targets"][-1]["reason"]


def test_full_page_is_followed_until_a_short_page_and_counts_pages():
    controls = _all_controls()
    first_page = controls[:4]  # eight child order records: exactly page_size
    second_page = controls[4:]  # two records: the required short terminal page
    reader = FakeReader([first_page, second_page])

    report, events = _run(reader, page_size=8)

    assert report["verdict"] == probe.Verdict.FOUND.value
    assert report["history_pages"] == 2
    assert reader.history_calls[1]["last_client_order_id"] == "fixture-3-S"
    page_events = [event for event in events if event["event"] == "history_page"]
    assert page_events[0]["count_vs_page_size"] == "AT_OR_ABOVE_CONTINUE"
    assert page_events[1]["count_vs_page_size"] == "SHORT_TERMINAL"


def test_combo_page_over_page_size_is_not_mistaken_for_terminal():
    controls = _all_controls()
    # Four child records exceed page_size=3, so the probe must request another page.
    reader = FakeReader([controls[:2], controls[2:], []])

    report, _events = _run(reader, page_size=3)

    assert report["history_pages"] == 3
    assert report["verdict"] == probe.Verdict.FOUND.value


def test_exactly_full_last_data_page_needs_empty_short_terminal_page():
    controls = _all_controls()
    reader = FakeReader([controls, []])

    report, _events = _run(reader, page_size=10)

    assert report["history_pages"] == 2
    assert report["history_complete"] is True


def test_repeated_page_is_could_not_tell_not_clean_total():
    first = _all_controls()
    reader = FakeReader([first, first])

    report, _events = _run(reader, page_size=10)

    assert report["verdict"] == probe.Verdict.COULD_NOT_TELL.value
    assert report["history_complete"] is False
    assert "repeated" in report["history_error"]


def test_history_transport_failure_is_could_not_tell_without_detail_fanout():
    failure = probe.CallFailure("history", "timeout", code="TIMEOUT")
    reader = FakeReader([failure])

    report, _events = _run(reader)

    assert report["verdict"] == probe.Verdict.COULD_NOT_TELL.value
    assert report["history_complete"] is False
    assert reader.detail_calls == []


def test_control_symbol_or_external_stop_mismatch_is_void():
    groups = _all_controls()
    groups[-1]["orders"][1]["stop_price"] = "7.59"
    reader = FakeReader([groups])

    report, _events = _run(reader)

    assert report["verdict"] == probe.Verdict.VOID.value
    assert "expected stop 7.5905" in report["targets"][-1]["reason"]


def test_history_source_is_not_declared_live_trustworthy_from_old_controls():
    reader = FakeReader([_all_controls()])

    report, _events = _run(reader)

    assessment = report["history_source_assessment"]
    assert assessment["known_positive_controls_reproduced"] is True
    assert assessment["combo_children_visible"] is True
    assert assessment["freshness_bound_proven"] is False
    assert assessment["partial_fill_semantics_proven"] is False
    assert assessment["trustworthy_as_live_reconciliation_source"] is False


def test_request_pacer_never_issues_two_calls_as_a_burst():
    clock = [10.0]
    sleeps = []

    def monotonic():
        return clock[0]

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    pacer = probe.RequestPacer(monotonic=monotonic, sleep=sleep)
    pacer.before_call()
    pacer.before_call()
    pacer.before_call()

    assert sleeps == [2.1, 2.1]
    snapshot = pacer.snapshot()
    assert snapshot["total_requests"] == 3
    assert snapshot["pacing_floor_seconds"] == 4.2


def test_live_code_contains_no_mutating_sdk_calls():
    """⛔⭐⭐ AN ALLOWLIST CLAIM NEEDS AN ALLOWLIST CHECK (§273, 2026-08-24).

    This test WAS a denylist of four literal substrings — `.place_order(`, `.replace_order(`,
    `.cancel_order(`, `.preview_order(`. The module docstring makes an ALLOWLIST claim ("can call
    only two SDK methods"), and a denylist cannot enforce it: it constrains the four names someone
    thought of, and says nothing about the fifth.

    ⛔ MEASURED, both mutants SURVIVED the old test:
      * `self.operation.batch_place_order(...)` — a REAL mutating method on the very class this
        probe instantiates. `.place_order(` does not match `.batch_place_order(`, because the
        character before `place_order` is `_`, not `.`. The same token-boundary blind spot as B32,
        in the guard that stands between a diagnostic and a live real-money account.
      * `self.operation.api_client.post("/openapi/trade/order/place", {})` — a raw HTTP write that
        bypasses every SDK wrapper, so no method-name denylist could ever see it.

    ⇒ The check is now an AST ALLOWLIST over the calls actually made on the SDK objects, plus a
    ban on raw HTTP verbs. It is complete by construction rather than by recall: a new mutating
    method added to the SDK next month is refused without anyone remembering to add its name.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))

    ALLOWED = {"get_order_history", "get_order_detail"}
    SDK_RECEIVERS = {"operation", "client", "api_client", "adapter"}
    LOCAL_PARSERS = {"_body", "_response_status", "_get_client"}
    HTTP_VERBS = {"post", "put", "patch", "delete"}
    MUTATING = {
        "place_order", "replace_order", "cancel_order", "preview_order",
        "batch_place_order", "place_option", "cancel_option", "replace_option",
        "preview_option", "submit_order",
    }

    # ⛔⭐⭐ AN SDK OBJECT MAY ONLY APPEAR AS THE RECEIVER OF AN ALLOWED CALL. NOTHING ELSE.
    # The previous rule checked the receiver NAME at each call site, so it saw
    # `self.operation.write()` and missed:
    #       op = self.operation
    #       op.write()
    # — the object escaped into a local and the guard never followed it. That contradicted the
    # "complete by construction" claim this test makes, on the guard standing between a diagnostic
    # and a LIVE REAL-MONEY ACCOUNT. Found by codex-2, round 12.
    #
    # ⇒ Chasing aliases through assignments would be endless (containers, returns, closures,
    #   defaults). Instead invert it: forbid the SDK object from being ANYWHERE except immediately
    #   under an allowed call. An escape has no syntax left. `self.operation = ...` in __init__ is
    #   a Store and is fine; every Load must be `<sdk>.<method>(...)`.
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def _is_sdk_ref(node):
        return (
            isinstance(node, ast.Attribute)
            and node.attr in SDK_RECEIVERS
            and isinstance(node.ctx, ast.Load)
        )

    escapes = []
    called_on_sdk = set()
    for node in ast.walk(tree):
        if not _is_sdk_ref(node):
            continue
        parent = parents.get(node)
        # legal shape: Attribute(value=<sdk>, attr=method) whose parent is Call(func=that Attribute)
        if isinstance(parent, ast.Attribute) and parent.value is node:
            grand = parents.get(parent)
            if isinstance(grand, ast.Call) and grand.func is parent:
                called_on_sdk.add(parent.attr)
                continue
            escapes.append(f"{ast.unparse(parent)} (attribute access, not a call)")
            continue
        escapes.append(ast.unparse(parent) if parent is not None else ast.unparse(node))

    assert not escapes, (
        "an SDK object escaped its call site — it may only be the receiver of an allowed "
        f"method call, never aliased, passed or stored: {sorted(set(escapes))[:5]}"
    )

    forbidden = called_on_sdk - ALLOWED - LOCAL_PARSERS
    assert not forbidden, (
        f"the probe calls SDK methods outside the two-method contract: {sorted(forbidden)}"
    )

    http_calls = {
        f"{node.func.value.attr}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in HTTP_VERBS and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr in SDK_RECEIVERS
    }
    assert not http_calls, (
        f"the probe issues a raw mutating HTTP call, bypassing the SDK contract: {sorted(http_calls)}"
    )

    dynamic = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in MUTATING
    }
    # ⛔ NO getattr CLAUSE HERE ANY MORE, AND THAT IS NOT A WEAKENING. `getattr(self.operation,
    # "cancel_order")(...)` puts the SDK object in an ARGUMENT position, so the escape rule above
    # already refuses it. A blanket getattr ban flagged the probe's legitimate defensive field
    # reads (`getattr(response, "body", None)`) — a guard that fires on correct code gets muted,
    # which costs exactly what a silent one does.
    assert not dynamic, (
        "the probe names a mutating SDK method as a STRING or dispatches dynamically, "
        f"which no static guard can follow: {sorted(dynamic)}"
    )

    # ⛔ Keep the literal denylist too. The AST walk covers calls; this covers a mutating name
    # appearing anywhere else (a getattr string, a dispatch table, a comment that becomes code).
    # Belt and braces on the one guard that stands in front of a real-money account.
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden_name in (
        "place_order", "replace_order", "cancel_order", "preview_order",
        "batch_place_order", "place_option", "cancel_option", "replace_option",
        "preview_option", "submit_order",
    ):
        assert f".{forbidden_name}(" not in source, f"mutating SDK call present: {forbidden_name}"
