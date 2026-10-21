"""The software close was selling shares its OWN resting exit pair had reserved.

⛔⭐⭐ THE DEFECT (live 2026-08-13, `live:orb`). A resting exit leg RESERVES the position at the
broker. The v2 ladder then sends its own market sell for the same shares, Webull sees
available-to-sell = 0 and 417s it as a naked short:
    NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K   39
    ORDER_NOT_SUPPORT_REVERSE_OPTION                              19
56 of the 58 were ONE XHG share; 48 of those inside five minutes.

⭐ THE ASYMMETRY THAT EXPLAINS IT. `-close-` filled 4/62 at Webull against 5/6 at Schwab. Schwab
stands its ladder down while a bracket is armed (`_native_oco_stand_down_active`); Webull exposes
no `fetch_armed_native_oco_symbols`, and `routing.py` fails OPEN, so the ladder fires into its own
reservation.

⛔ WHY THIS IS EVEN POSSIBLE: the OCO children are broker-created and never land in `broker_orders`
(store.py says a DB query for them always returns nothing), so they cannot be looked UP — but they
are placed under DETERMINISTIC coids, so they can be ADDRESSED BY NAME.
"""
from __future__ import annotations

import asyncio
import ast
import logging
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.oms import service as svc


_MANAGED_EXIT_PREFIX = "oms_v2_managed_exit:"
# Profit-taking is the redundant close that must wait while a broker OCO still reserves shares.
_EXPLICIT_NON_PROTECTIVE_REASONS = {
    f"{_MANAGED_EXIT_PREFIX}CW_TARGET",
    f"{_MANAGED_EXIT_PREFIX}SCALE_*",
}


# Routines that take `reason=` and hand it unchanged to `_emit_v2_exit_on_loop`.
_REASON_FORWARDING_ROUTINES = {"_webull_cancel_then_sell"}


def _bind_string_constants(target: ast.expr, value: ast.expr, out: dict[str, set[str]]) -> None:
    if isinstance(target, ast.Name) and isinstance(value, ast.Constant):
        if isinstance(value.value, str):
            out[target.id].add(value.value)
        return
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
        for child_target, child_value in zip(target.elts, value.elts, strict=False):
            _bind_string_constants(child_target, child_value, out)


def _joined_string_template(value: ast.JoinedStr) -> str:
    parts: list[str] = []
    for part in value.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append(part.value)
        elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name):
            parts.append(f"{{{part.value.id}}}")
        else:
            parts.append("{unresolved}")
    return "".join(parts)


def _called_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _validated_retry_reason_calls(tree: ast.Module) -> dict[int, set[str]]:
    """Follow only the guarded DB return and its exact tuple transport to emit."""
    def shape(source: str) -> str:
        return ast.dump(ast.parse(source).body[0], include_attributes=False)

    def matches(node: ast.AST, source: str) -> bool:
        return ast.dump(node, include_attributes=False) == shape(source)

    constants = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        if not isinstance(node.targets[0], ast.Name):
            continue
        value = node.value
        if (isinstance(value, ast.Call) and _called_name(value) == "frozenset"
                and len(value.args) == 1 and not value.keywords):
            value = value.args[0]
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError):
            continue
        if isinstance(literal, str):
            constants[node.targets[0].id] = {literal}
        elif isinstance(literal, set) and all(isinstance(item, str) for item in literal):
            constants[node.targets[0].id] = literal
    if not {"SOFTWARE_EXIT_REASONS", "REASON"} <= constants.keys():
        return {}
    result = {}
    for outer in tree.body:
        if not isinstance(outer, ast.AsyncFunctionDef) or outer.name != "retry_atr_proven_unsent_exit":
            continue
        readers = [node for node in outer.body if isinstance(node, ast.FunctionDef) and node.name == "read"]
        if len(readers) != 1:
            continue
        reader = readers[0]
        if any(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
               and node.id in {"SOFTWARE_EXIT_REASONS", "REASON"}
               for node in ast.walk(reader)):
            continue
        if not matches(reader.body[-1],
                       "return service._hydrate_v2_position(row), float(row.entry_price), intent.reason"):
            continue
        guards = [node for node in reader.body[:-1] if isinstance(node, ast.If)
                  and isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.Or)
                  and not node.orelse and len(node.body) == 1 and matches(node.body[0], "return None")
                  and any(matches(ast.Expr(value=part),
                      "intent.reason not in SOFTWARE_EXIT_REASONS | {REASON}")
                      for part in node.test.values)]
        # The final guard must immediately dominate the value return; no intervening mutation.
        if not guards or reader.body[-2] is not guards[-1]:
            continue
        if any(node.value is not None and not (isinstance(node.value, ast.Constant) and node.value.value is None)
               for node in ast.walk(reader) if isinstance(node, ast.Return) and node is not reader.body[-1]):
            continue
        transport = [shape("bound = await service._run_db(read, commit=False)"),
                     shape("if bound is None:\n    return None"),
                     shape("position, entry_price, reason = bound")]
        body = outer.body
        starts = [index for index in range(len(body) - 2)
                  if [ast.dump(node, include_attributes=False) for node in body[index:index + 3]] == transport]
        if len(starts) != 1:
            continue
        prefix = body[:starts[0]]
        if any(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
               and node.id in {"read", "SOFTWARE_EXIT_REASONS", "REASON"}
               for statement in prefix if statement is not reader for node in ast.walk(statement)):
            continue
        tail = body[starts[0] + 3:]
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))
               for statement in tail for node in ast.walk(statement)):
            continue
        # Any subsequent rebinding invalidates this narrow proof instead of hiding the emit.
        if any(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
               and node.id in {"reason", "bound", "intent", "SOFTWARE_EXIT_REASONS", "REASON"}
               for statement in tail for node in ast.walk(statement)):
            continue
        values = constants["SOFTWARE_EXIT_REASONS"] | constants["REASON"]
        for statement in tail:
            for call in ast.walk(statement):
                if (isinstance(call, ast.Call) and _called_name(call) == "_emit_v2_exit_on_loop"
                        and any(kw.arg == "reason" and isinstance(kw.value, ast.Name)
                                and kw.value.id == "reason" for kw in call.keywords)):
                    result[id(call)] = values
    return result


def _emitted_managed_exit_reasons(source_root: Path | None = None) -> set[str]:
    """Discover the reason contract from emit call sites, not from the classifier itself."""
    if source_root is None:
        source_root = Path(__file__).parents[2] / "src" / "project_mai_tai"
    emitted: set[str] = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        validated_retry_calls = _validated_retry_reason_calls(tree)
        bindings: dict[str, set[str]] = defaultdict(set)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    _bind_string_constants(target, node.value, bindings)
        # A routine that only FORWARDS its own `reason=` parameter to the emit is not where the
        # reason is decided - its callers are. Follow exactly one hop: the forwarded name is skipped
        # inside the routine, and every call TO the routine is held to the same contract as an emit.
        forwarded: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in _REASON_FORWARDING_ROUTINES
                and any(arg.arg == "reason" for arg in node.args.kwonlyargs + node.args.args)
            ):
                forwarded.update(id(inner) for inner in ast.walk(node) if isinstance(inner, ast.Call))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = _called_name(node)
            if called != "_emit_v2_exit_on_loop" and called not in _REASON_FORWARDING_ROUTINES:
                continue
            reason = next((kw.value for kw in node.keywords if kw.arg == "reason"), None)
            if (
                id(node) in forwarded
                and called == "_emit_v2_exit_on_loop"
                and isinstance(reason, ast.Name)
                and reason.id == "reason"
            ):
                continue
            if called in _REASON_FORWARDING_ROUTINES and reason is None:
                emitted.add(f"unresolved:{called} called without reason=")
                continue
            if id(node) in validated_retry_calls:
                emitted.update(value for value in validated_retry_calls[id(node)]
                               if value.startswith(_MANAGED_EXIT_PREFIX))
            elif isinstance(reason, ast.Constant) and isinstance(reason.value, str):
                if reason.value.startswith(_MANAGED_EXIT_PREFIX):
                    emitted.add(reason.value)
            elif isinstance(reason, ast.Name) and reason.id in bindings:
                emitted.update(value for value in bindings[reason.id]
                               if value.startswith(_MANAGED_EXIT_PREFIX))
            elif isinstance(reason, ast.JoinedStr):
                template = _joined_string_template(reason)
                if template == f"{_MANAGED_EXIT_PREFIX}{{tag}}":
                    emitted.update(f"{_MANAGED_EXIT_PREFIX}{tag}" for tag in bindings["tag"])
                elif template == f"{_MANAGED_EXIT_PREFIX}SCALE_{{level}}":
                    emitted.add(f"{_MANAGED_EXIT_PREFIX}SCALE_*")
                elif template.startswith(_MANAGED_EXIT_PREFIX):
                    emitted.add(template)
            elif reason is not None:
                emitted.add(f"unresolved:{ast.dump(reason, include_attributes=False)}")
    return emitted


@pytest.mark.parametrize("reason", ["oms_v2_managed_exit:CW_TARGET", "WEBULL_ATTACH_RISK_OFF"])
def test_literal_reason_name_has_same_prefix_semantics_as_inline_literal(tmp_path, reason):
    literal = tmp_path / "literal"
    named = tmp_path / "named"
    literal.mkdir()
    named.mkdir()
    (literal / "caller.py").write_text(
        f"service._emit_v2_exit_on_loop(reason={reason!r})\n", encoding="utf-8")
    (named / "caller.py").write_text(
        f"REASON = {reason!r}\nservice._emit_v2_exit_on_loop(reason=REASON)\n", encoding="utf-8")
    expected = {reason} if reason.startswith(_MANAGED_EXIT_PREFIX) else set()
    assert _emitted_managed_exit_reasons(literal) == expected
    assert _emitted_managed_exit_reasons(named) == expected


def test_unbound_reason_name_remains_unresolved_not_silently_ignored(tmp_path):
    (tmp_path / "caller.py").write_text(
        "service._emit_v2_exit_on_loop(reason=UNKNOWN)\n", encoding="utf-8")
    assert _emitted_managed_exit_reasons(tmp_path) == {"unresolved:Name(id='UNKNOWN', ctx=Load())"}


@pytest.mark.parametrize("mutation", ["none", "unguarded", "rebound", "wrong_return", "wrong_transport",
                                     "reader_rebound", "constant_shadow", "outer_shadow", "nested_reason"])
def test_retry_reason_requires_guarded_return_and_unchanged_transport(tmp_path, mutation):
    source = '''
REASON = "WEBULL_ATTACH_RISK_OFF"
SOFTWARE_EXIT_REASONS = frozenset({"oms_v2_managed_exit:CW_FLIP"})
async def retry_atr_proven_unsent_exit(service, account, symbol, managed_id):
    def read(session):
        intent = session.intent
        if intent is None or intent.reason not in SOFTWARE_EXIT_REASONS | {REASON}:
            return None
        return service._hydrate_v2_position(row), float(row.entry_price), intent.reason
    bound = await service._run_db(read, commit=False)
    if bound is None:
        return None
    position, entry_price, reason = bound
    return await service._emit_v2_exit_on_loop(reason=reason)
'''
    if mutation == "unguarded":
        source = source.replace("intent.reason not in SOFTWARE_EXIT_REASONS | {REASON}", "False")
    elif mutation == "rebound":
        source = source.replace("    return await", "    reason = UNKNOWN\n    return await")
    elif mutation == "wrong_return":
        source = source.replace("float(row.entry_price), intent.reason", "float(row.entry_price), intent.other")
    elif mutation == "wrong_transport":
        source = source.replace("service._run_db(read,", "service._run_db(other,")
    elif mutation == "reader_rebound":
        source = source.replace("    bound =", "    read = other\n    bound =")
    elif mutation == "constant_shadow":
        source = source.replace("        intent =", "        SOFTWARE_EXIT_REASONS = other\n        intent =")
    elif mutation == "outer_shadow":
        source = source.replace("    bound =", "    REASON = other\n    bound =")
    elif mutation == "nested_reason":
        source = source.replace("    return await service._emit_v2_exit_on_loop(reason=reason)",
                                "    async def nested(reason):\n        return await service._emit_v2_exit_on_loop(reason=reason)")
    (tmp_path / "caller.py").write_text(source, encoding="utf-8")
    expected = ({"oms_v2_managed_exit:CW_FLIP"} if mutation == "none"
                else {"unresolved:Name(id='reason', ctx=Load())"})
    assert _emitted_managed_exit_reasons(tmp_path) == expected


# --------------------------------------------------------------- the adapter can address the legs
def _adapter() -> WebullBrokerAdapter:
    return object.__new__(WebullBrokerAdapter)


def test_the_two_legs_are_addressable_by_deterministic_id() -> None:
    ids = WebullBrokerAdapter.exit_pair_leg_client_order_ids(_adapter(), "base-coid")
    assert ids == ["base-coidT", "base-coidS"]


def test_leg_ids_stay_inside_the_40_CHAR_CAP() -> None:
    """⛔ Same cap that broke the attach. A cancel aimed at an over-long id addresses an order that
    cannot exist, so the reservation is never released — which reads exactly like the bug."""
    ids = WebullBrokerAdapter.exit_pair_leg_client_order_ids(_adapter(), "z" * 60)
    assert all(len(i) <= 40 for i in ids)
    assert ids[0] != ids[1]


def test_no_base_id_means_NOTHING_is_cancelled() -> None:
    """⛔ An empty base must never fan out into blind cancels."""
    assert WebullBrokerAdapter.exit_pair_leg_client_order_ids(_adapter(), "") == []


class _Adapter:
    """Records what the OMS asked the broker to do."""

    def __init__(self, *, has_capability: bool = True, reports=None) -> None:
        self.cancelled: list[tuple[str, str]] = []
        self.has_capability = has_capability
        self.reports = reports or [
            SimpleNamespace(event_type="cancelled", reason="confirmed"),
            SimpleNamespace(event_type="cancelled", reason="confirmed"),
        ]
        if not has_capability:
            # Schwab / simulated: no addressable resting legs.
            self.cancel_exit_pair = None  # type: ignore[assignment]

    async def cancel_exit_pair(self, *, broker_account_name, symbol, base_client_order_id):
        self.cancelled.append((symbol, base_client_order_id))
        return self.reports

    async def release_exit_pair_for_close(
        self, *, broker_account_name, symbol, base_client_order_id
    ):
        if not self.has_capability:
            return ExitPairReleaseResult(outcome="unsupported")
        self.cancelled.append((symbol, base_client_order_id))
        reports = tuple(self.reports)
        outcome = (
            "released"
            if len(reports) == 2 and all(report.event_type == "cancelled" for report in reports)
            else "reserved"
        )
        return ExitPairReleaseResult(outcome=outcome, reports=reports)


def _svc(adapter, *, base: str = "protect-base") -> svc.OmsRiskService:
    s = object.__new__(svc.OmsRiskService)
    s.settings = SimpleNamespace(oms_v2_exit_release_reservation_enabled=True)
    s.logger = logging.getLogger("test-release")
    s.broker_adapter = adapter
    s._webull_protect_base = {("live:orb", "XHG"): base} if base else {}
    s._exit_reservation_released = set()
    entry = SimpleNamespace(
        id=uuid4(),
        client_order_id="entry-coid", broker_order_id="owned-parent", payload={
            "fanout_leg": "webull", "native_oco_bracket": "false",
            "webull_protect_base_client_order_id": base,
        })
    managed = SimpleNamespace(id=uuid4(), strategy_code="schwab_1m_v2",
        broker_account_name="live:orb", symbol="XHG", entry_order_id=entry.id,
        entry_client_order_id=entry.client_order_id, current_quantity=1, status="open")

    def current_row(session, *, broker_account_name, symbol):
        assert broker_account_name == managed.broker_account_name and symbol == managed.symbol
        return managed

    def bound_entry(session, account, symbol, **kwargs):
        assert current_row(session, broker_account_name=account, symbol=symbol).entry_order_id == entry.id
        return entry if base else None

    s.store = SimpleNamespace(get_open_managed_position=current_row)
    s._find_oco_entry_order = bound_entry
    return s


def _release(
    s, symbol: str = "XHG", *, terminal_after_attempt: bool = False
) -> ExitPairReleaseResult:
    return asyncio.run(s._release_exit_reservation_before_close(
        session=object(), broker_account_name="live:orb", symbol=symbol,
        terminal_after_attempt=terminal_after_attempt,
    ))


def test_it_CANCELS_the_resting_pair_before_the_close(caplog) -> None:
    """Known-good control: two confirmed legs fire the success marker and stay quiet on failure."""
    caplog.set_level(logging.INFO)
    a = _Adapter()
    assert _release(_svc(a)).outcome == "released"
    assert a.cancelled == [("XHG", "protect-base")]
    text = caplog.text
    assert "[OMS-CANCEL-PAIR-REQUEST]" in text
    assert "requested=2 confirmed=0 release_confirmed=0" in text
    assert "[OMS-EXIT-RELEASE]" in text
    assert "requested=2 confirmed=2 release_confirmed=1" in text
    assert "[OMS-CANCEL-PAIR-UNCERTAIN]" not in text


def test_forced_HTTP_417_never_emits_the_success_marker(caplog) -> None:
    """Known-bad control: the exact DAIC shape, two broker refusals returned as reports."""
    caplog.set_level(logging.INFO)
    rejected = [
        SimpleNamespace(event_type="rejected", reason="Webull rejected (http 417)"),
        SimpleNamespace(event_type="rejected", reason="Webull rejected (http 417)"),
    ]
    s = _svc(_Adapter(reports=rejected))
    assert _release(s).outcome == "reserved"
    assert ("live:orb", "XHG") not in s._exit_reservation_released
    text = caplog.text
    assert "[OMS-CANCEL-PAIR-UNCERTAIN]" in text
    assert "requested=2 reports=2 confirmed=0 refused=2 release_confirmed=0" in text
    assert "[OMS-EXIT-RELEASE]" not in text


def test_one_confirmed_and_one_refused_is_NOT_a_release(caplog) -> None:
    """A half-clear pair can still reserve the shares; 1/2 must keep success quiet."""
    caplog.set_level(logging.INFO)
    reports = [
        SimpleNamespace(event_type="cancelled", reason="confirmed"),
        SimpleNamespace(event_type="rejected", reason="http 417"),
    ]
    assert _release(_svc(_Adapter(reports=reports))).outcome == "reserved"
    assert "confirmed=1 refused=1 release_confirmed=0" in caplog.text
    assert "[OMS-EXIT-RELEASE]" not in caplog.text


def test_a_confirmed_release_is_reused_for_the_episode() -> None:
    """The ladder re-evaluates every quote tick; a confirmed release must not be repeated."""
    a = _Adapter()
    s = _svc(a)
    for _ in range(25):
        _release(s)
    assert len(a.cancelled) == 1, "a released reservation must not be re-cancelled every tick"


def test_the_latch_is_CLEARED_when_the_position_closes() -> None:
    """⛔ A latch that outlives its position means the NEXT entry's reservation is never released
    and the storm returns silently — the code would still look like it handles the case."""
    a = _Adapter()
    s = _svc(a)
    _release(s)
    assert len(a.cancelled) == 1
    svc.OmsRiskService._clear_exit_reservation_release(s, "live:orb", "XHG")
    s._webull_protect_base[("live:orb", "XHG")] = "protect-base-2"
    entry = SimpleNamespace(id=uuid4(),
        client_order_id="next-entry", broker_order_id="next-owned-parent", payload={
            "fanout_leg": "webull", "webull_protect_base_client_order_id": "protect-base-2"})
    managed = s.store.get_open_managed_position(None, broker_account_name="live:orb", symbol="XHG")
    managed.id, managed.entry_order_id = uuid4(), entry.id
    managed.entry_client_order_id = entry.client_order_id
    s._find_oco_entry_order = lambda *a_, **k: entry
    _release(s)
    assert len(a.cancelled) == 2, "the next position must release its own legs"


def test_an_adapter_with_NO_capability_changes_nothing() -> None:
    """Schwab/simulated: no addressable legs -> byte-identical to today, and NOT latched as
    released (claiming a release we never performed would be worse than doing nothing)."""
    a = _Adapter(has_capability=False)
    s = _svc(a)
    assert _release(s).outcome == "unsupported"
    assert ("live:orb", "XHG") not in s._exit_reservation_released


def test_the_router_forwards_the_post_cancel_broker_state() -> None:
    adapter = _Adapter()
    router = RoutingBrokerAdapter(
        default_provider="webull",
        provider_by_account={"live:orb": "webull"},
        factories_by_provider={"webull": lambda: adapter},
    )

    result = asyncio.run(router.release_exit_pair_for_close(
        broker_account_name="live:orb", symbol="XHG", base_client_order_id="protect-base"
    ))

    assert result.outcome == "released"
    assert adapter.cancelled == [("XHG", "protect-base")]


def test_no_known_base_and_no_entry_order_cancels_NOTHING() -> None:
    a = _Adapter()
    s = _svc(a, base="")
    assert _release(s).outcome == "unanswerable"
    assert a.cancelled == []


def test_it_falls_back_to_the_ENTRY_coid_when_the_attach_id_is_forgotten() -> None:
    """The attach base is in-memory only, so a restart loses it. The bracket ENTRY order IS a real
    `broker_orders` row, and the native combo's legs hang off it."""
    a = _Adapter()
    s = _svc(a, base="")
    managed = s.store.get_open_managed_position(None, broker_account_name="live:orb", symbol="XHG")
    s._find_oco_entry_order = lambda *a_, **k: SimpleNamespace(
        id=managed.entry_order_id, client_order_id=managed.entry_client_order_id,
        broker_order_id="owned-parent", payload={"native_oco_bracket": "true"})
    assert _release(s).outcome == "released"
    assert a.cancelled == [("XHG", "entry-coid")]


def test_a_RAISING_release_is_unanswerable_and_never_claimed_clear() -> None:
    """Unknown broker state cannot authorize a sell against potentially reserved shares."""
    class _Boom:
        async def release_exit_pair_for_close(self, **kw):
            raise RuntimeError("network")

    s = _svc(_Boom())
    assert _release(s).outcome == "unanswerable"
    assert ("live:orb", "XHG") not in s._exit_reservation_released


def test_throttle_preserves_unanswerable_instead_of_claiming_protection_survived() -> None:
    class _Boom:
        async def release_exit_pair_for_close(self, **kw):
            raise RuntimeError("network")

    s = _svc(_Boom())
    assert _release(s).outcome == "unanswerable"
    assert _release(s).outcome == "unanswerable"


def test_release_probes_end_at_the_existing_v2_retry_bound(monkeypatch) -> None:
    reports = [SimpleNamespace(event_type="rejected", reason="still working")]
    adapter = _Adapter(reports=reports)
    s = _svc(adapter)
    clock = {"now": 100.0}
    monkeypatch.setattr(svc.time, "monotonic", lambda: clock["now"])

    assert s._EXIT_RESERVATION_RETRY_SECONDS == 10.0
    assert s._EXIT_RESERVATION_MAX_ATTEMPTS == 8
    for _ in range(11):
        assert _release(s).outcome == "reserved"
        clock["now"] += 10.0

    key = ("live:orb", "XHG")
    assert len(adapter.cancelled) == 8
    assert s._exit_reservation_terminal[key] == "reserved"


def test_session_end_attempt_enters_the_operator_owned_terminal_state() -> None:
    reports = [SimpleNamespace(event_type="rejected", reason="still working")]
    adapter = _Adapter(reports=reports)
    s = _svc(adapter)

    assert _release(s, terminal_after_attempt=True).outcome == "reserved"

    key = ("live:orb", "XHG")
    assert s._exit_reservation_attempts[key] == 1
    assert s._exit_reservation_terminal[key] == "reserved"
    assert len(adapter.cancelled) == 1


@pytest.mark.parametrize(
    ("reason", "protective"),
    [
        ("oms_v2_managed_exit:CONFIRMATION_EXIT", True),
        ("oms_v2_managed_exit:CW_FLOOR", True),
        ("oms_v2_managed_exit:CW_HARD_STOP", True),
        ("oms_v2_managed_exit:CW_FLIP", True),
        ("oms_v2_managed_exit:HARD_STOP", True),
        ("oms_v2_managed_exit:FLOOR_BREACH", True),
        ("V2_EOD_CANCEL_REEXIT", True),
        ("V2_OVERNIGHT_FLATTEN", True),
        ("oms_v2_managed_exit:CW_TARGET", False),
        ("oms_v2_managed_exit:SCALE_PCT2", False),
    ],
)
def test_protective_exit_classification_is_explicit(reason: str, protective: bool) -> None:
    assert svc.OmsRiskService._is_protective_v2_exit(reason) is protective


def test_every_emitted_managed_exit_reason_is_explicitly_classified() -> None:
    emitted = _emitted_managed_exit_reasons()

    assert _EXPLICIT_NON_PROTECTIVE_REASONS <= emitted
    assert svc.OmsRiskService._is_protective_v2_exit(f"{_MANAGED_EXIT_PREFIX}CW_TARGET") is False
    assert svc.OmsRiskService._is_protective_v2_exit(f"{_MANAGED_EXIT_PREFIX}SCALE_PCT2") is False
    unclassified = {
        reason
        for reason in emitted
        if reason not in _EXPLICIT_NON_PROTECTIVE_REASONS
        and not svc.OmsRiskService._is_protective_v2_exit(reason)
    }
    assert unclassified == set(), f"managed-exit reasons need classification: {unclassified}"


# ------------------------------------------------- re-protect what the release uncovered
def _reprotect_svc(*, released: bool):
    s = object.__new__(svc.OmsRiskService)
    s.logger = logging.getLogger("test-reprotect")
    s.settings = SimpleNamespace()
    s._exit_reservation_released = {("live:orb", "XHG")} if released else set()
    s._v2_exit_close_failures = {("live:orb", "XHG"): 3}
    s._v2_exit_stood_down = set()
    s.spawned = []
    s._spawn_webull_protection = lambda **kw: s.spawned.append(kw)
    return s


def _row(entry_price=5.0, qty=1):
    return SimpleNamespace(entry_price=entry_price, current_quantity=qty,
                           strategy_code="schwab_1m_v2")


def test_a_released_position_that_will_not_CLOSE_gets_its_protection_BACK() -> None:
    """⛔⭐⭐ THE HAZARD THE RELEASE CREATES. Before it existed, a failing close was survivable —
    the OCO legs stayed put and took the position out at +2%/-5% on their own. Cancelling them
    removes that net, so a close that never goes through leaves the position NAKED. That would be
    strictly worse than the reject storm it replaced."""
    s = _reprotect_svc(released=True)
    svc.OmsRiskService._reprotect_after_failed_release(s, "live:orb", "XHG", _row())
    assert len(s.spawned) == 1
    assert s.spawned[0]["symbol"] == "XHG"
    assert s.spawned[0]["entry_price"] == 5.0
    assert s.spawned[0]["quantity"] == 1


def test_it_will_NOT_reprotect_from_an_unusable_row() -> None:
    """⛔ A pair priced off a zero entry, or for zero shares, is an unpaired sell against shares we
    may not own — the E5/NXTC oversell shape. Refuse and say so."""
    for row in (_row(entry_price=0), _row(qty=0)):
        s = _reprotect_svc(released=True)
        svc.OmsRiskService._reprotect_after_failed_release(s, "live:orb", "XHG", row)
        assert s.spawned == []


def test_reprotect_says_so_LOUDLY_when_it_cannot(caplog) -> None:
    s = _reprotect_svc(released=True)
    with caplog.at_level(logging.WARNING):
        svc.OmsRiskService._reprotect_after_failed_release(s, "live:orb", "XHG", _row(entry_price=0))
    assert "UNCOVERED" in caplog.text


def test_a_raising_reprotect_never_breaks_the_protective_sync() -> None:
    s = _reprotect_svc(released=True)

    def _boom(**kw):
        raise RuntimeError("no adapter")
    s._spawn_webull_protection = _boom
    svc.OmsRiskService._reprotect_after_failed_release(s, "live:orb", "XHG", _row())  # must not raise


def test_reprotect_fires_ONLY_when_we_actually_released() -> None:
    """A position whose legs we never cancelled is already protected — a second pair would
    double-reserve the shares and draw an oversell refusal."""
    import inspect

    src = inspect.getsource(svc.OmsRiskService._v2_close_reconcile_flat)
    assert "if key in self._exit_reservation_released:" in src
    assert "_reprotect_after_failed_release" in src
    # ...and only on a POSITIVELY-HELD read, never an inconclusive one.
    held = src.index("if state is _PositionRead.HELD:")
    assert held < src.index("_reprotect_after_failed_release")


def test_the_flag_defaults_OFF() -> None:
    """⛔ It edits the exit path and opens a brief unprotected window. Must be chosen, not
    inherited."""
    from project_mai_tai.settings import Settings

    assert Settings().oms_v2_exit_release_reservation_enabled is False


async def _assert_managed_release_order(monkeypatch, account, emit):
    from uuid import UUID
    from tests.unit.test_oco_duplicate_close_after_close import seed_episode
    from project_mai_tai.db.models import OmsManagedPosition

    service, factory, row_id, now, _ = seed_episode(account, exited=0)
    monkeypatch.setattr(svc, "utcnow", lambda: now)
    sequence = []

    async def released(**kwargs):
        assert kwargs["broker_account_name"] == account
        sequence.append("release")
        return ExitPairReleaseResult(outcome="released")

    async def submit(request):
        assert sequence == ["release"], "release must precede submit"
        assert request.broker_account_name == account and request.quantity == 150
        sequence.append("submit")
        return []

    monkeypatch.setattr(service, "_release_exit_reservation_before_close", released)
    monkeypatch.setattr(service.broker_adapter, "submit_order", submit)
    with factory() as session:
        await emit(service, session, session.get(OmsManagedPosition, UUID(row_id)),
                   intent_type="close", quantity=150, reference_price=4.19,
                   reason="oms_v2_managed_exit:HARD", bid=4.19)
    assert sequence == ["release", "submit"]


@pytest.mark.asyncio
async def test_the_release_runs_BEFORE_the_sell_is_submitted(monkeypatch) -> None:
    for account in ("live:schwab_1m_v2", "live:orb"):
        await _assert_managed_release_order(monkeypatch, account, svc.OmsRiskService._emit_v2_managed_sell)


@pytest.mark.asyncio
async def test_reversed_managed_release_order_mutant_is_detected(monkeypatch):
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(svc.OmsRiskService._emit_v2_managed_sell)))
    function = tree.body[0]
    release = next(statement for statement in function.body
                   if any(isinstance(node, ast.Attribute) and node.attr == "_release_exit_reservation_before_close"
                          for node in ast.walk(statement)))
    function.body.remove(release)
    index = next(i for i, statement in enumerate(function.body)
                 if isinstance(statement, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "reports" for target in statement.targets))
    function.body.insert(index + 1, release)
    namespace = dict(vars(svc))
    exec(compile(ast.fix_missing_locations(tree), "<release-after-submit-mutant>", "exec"), namespace)
    for account in ("live:schwab_1m_v2", "live:orb"):
        with pytest.raises(AssertionError, match="release must precede submit"):
            await _assert_managed_release_order(monkeypatch, account, namespace["_emit_v2_managed_sell"])


def test_the_hook_is_FLAG_GATED() -> None:
    import inspect

    src = inspect.getsource(svc.OmsRiskService._emit_v2_managed_sell)
    assert "oms_v2_exit_release_reservation_enabled" in src


def test_every_managed_row_close_clears_the_latch() -> None:
    """⛔ Counted, not eyeballed: every `close_managed_position` call site must clear the latch, or
    one forgotten path silently disables the fix for the next position on that symbol."""
    import inspect

    src = inspect.getsource(svc)
    closes = src.count("self.store.close_managed_position(")
    clears = src.count("_clear_exit_reservation_release(")
    # one definition + one call per close site
    assert clears >= closes + 1, f"{closes} close sites but only {clears - 1} latch clears"
