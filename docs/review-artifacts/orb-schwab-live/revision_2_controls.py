import ast
import importlib
import inspect
from pathlib import Path
import subprocess
import sys
import textwrap
import pytest

ROOT = Path(__file__).resolve().parents[3]
ORDER = "tests/unit/test_orb_schwab_order_route.py"
EXIT = "tests/unit/test_orb_schwab_exits.py"
ADAPTER = "tests/unit/test_orb_schwab_replace_adapter.py"
CASES = {
    "body_before_close": ("orb_schwab_exits", "exit_signal", " or now < closed_at", "", EXIT+"::test_no_body_decision_before_close_even_if_context_contains_bar"),
    "body_at_fill": ("orb_schwab_exits", "exit_signal", "return BODY_REASON, closed_at", "return BODY_REASON, fill_at", EXIT+"::test_body_under_45_matches_paper_formula_and_has_priority_over_atr"),
    "body_boundary": ("orb_schwab_exits", "exit_signal", "percentage < PAPER_MIN_BREAK_BODY_PCT", "percentage <= PAPER_MIN_BREAK_BODY_PCT", EXIT+"::test_exact_45_body_boundary"),
    "atr_on_break_bar": ("orb_schwab_exits", "exit_signal", "bar.timestamp > minute", "bar.timestamp >= minute", EXIT+"::test_atr_sell_on_break_bar_itself_is_not_a_later_bar_exit"),
    "advisory_flag": ("oms.service", "OmsRiskService.process_trade_intent", 'bool(getattr(self.settings, "orb_live_schwab_orders_enabled", False))\n                and (', 'True\n                and (', ORDER+"::test_v2_own_add_not_blocked_by_orb_broker_read_and_flag_off_has_no_lock[False]"),
    "watchdog_flag": ("oms.service", "OmsRiskService._orb_schwab_watchdog", "if not self.settings.orb_live_schwab_orders_enabled:", "if False:", ORDER+"::test_flag_off_watchdog_does_no_query_and_emits_no_intent"),
    "entry_window": ("orb_schwab_order_route", "orb_schwab_intent_refusal", "time(9, 28, 15)", "time(9, 29)", ORDER+"::test_new_open_deadline_is_exactly_092815_not_0929"),
    "source": ("orb_schwab_order_route", "orb_schwab_intent_refusal", "event.source_service != SOURCE_SERVICE", "False", ORDER+"::test_wrong_source_is_refused_before_any_io"),
    "filled_qty": ("oms.service", "OmsRiskService._orb_schwab_cancel_refusal", "or broker_report.filled_quantity != 0", "", ORDER+"::test_cancel_with_accepted_status_but_nonzero_filled_quantity_is_refused"),
    "lower_reprice": ("oms.service", "OmsRiskService._process_orb_schwab_reprice", "old_level <= 0 or new_level <= old_level", "old_level <= 0", ORDER+"::test_reprice_to_lower_level_is_refused"),
    "price_matches": ("broker_adapters.schwab", "SchwabBrokerAdapter.replace_bracket_order", "if not price_matches:", "if False:", ADAPTER+"::test_accepted_put_retains_new_id_on_every_confirmation_problem[wrong_price]"),
    "drop_new_id": ("oms.service", "OmsRiskService._process_orb_schwab_reprice", "target.broker_order_id = report.broker_order_id", "pass", ORDER+"::test_put_accepted_confirmation_unreadable_persists_new_id_and_reconciles_under_hold"),
    "drop_hold": ("oms.service", "OmsRiskService._orb_schwab_cancel_refusal", 'if (target.payload or {}).get("orb_replace_hold"):', "if False:", ORDER+"::test_put_accepted_confirmation_unreadable_persists_new_id_and_reconciles_under_hold"),
}


def install(module, attr, source):
    function = attr.split(".")[-1]
    exec(compile(textwrap.dedent(source), "<verification-control>", "exec"), module.__dict__)
    if "." in attr:
        setattr(getattr(module, attr.split(".")[0]), function, module.__dict__[function])


def old_method(module_path, cls, method, ref="84c3f234"):
    path = "src/project_mai_tai/" + module_path.replace(".", "/") + ".py"
    old = subprocess.check_output(["git", "show", ref + ":" + path], text=True)
    tree = ast.parse(old)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls)
    function = next(node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method)
    install(importlib.import_module("project_mai_tai." + module_path), cls+"."+method,
            textwrap.dedent("\n".join(old.splitlines()[function.lineno-1:function.end_lineno])))


if len(sys.argv) == 1:
    failed = []
    for case in [*CASES, "RED_R1", "RED_A1", "RED_A2", "RED_A3", "BASE_M1"]:
        result = subprocess.run([sys.executable, __file__, case], cwd=ROOT, text=True, capture_output=True)
        expected = 0 if case == "BASE_M1" else 1
        print(case, ("BASELINE PASS" if expected == 0 else "KILLED/RED") if result.returncode == expected else "UNEXPECTED", flush=True)
        print(result.stdout[-1400:], result.stderr[-500:], flush=True)
        if result.returncode != expected:
            failed.append(case)
    sys.exit(bool(failed))

case = sys.argv[1]
if case == "RED_R1":
    from types import ModuleType
    from datetime import UTC, datetime, timedelta
    name = "project_mai_tai.old_body_control"
    module = sys.modules[name] = ModuleType(name)
    old = subprocess.check_output(["git", "show", "84c3f234:src/project_mai_tai/orb_schwab_exits.py"], text=True)
    exec(compile(old, "<old-body-control>", "exec"), module.__dict__)
    start = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
    tape = module.OrbExitTape(start - timedelta(minutes=5))
    for sec, price in [(0, 10), (2, 11), (5, 9), (10, 10.1)]:
        tape.trade(start + timedelta(seconds=sec), price, 100)
    context = tape.evidence("fill", start + timedelta(seconds=10), start + timedelta(seconds=11),
                            atr_bars=[], atr_status="unknown")
    assert context["reason"] is None, "old head sells at fill, before break bar closes"
    sys.exit(0)
elif case == "BASE_M1":
    old_method("oms.service", "OmsRiskService", "process_trade_intent", "fc68b238")
    test = ORDER+"::test_v2_own_add_not_blocked_by_orb_broker_read_and_flag_off_has_no_lock[False]"
elif case == "RED_A1":
    for path in ["strategy_core/orb_intrabar", "orb_schwab_macd"]:
        module = importlib.import_module("project_mai_tai." + path.replace("/", "."))
        source = subprocess.check_output(["git", "show", "84c3f234:src/project_mai_tai/"+path+".py"], text=True)
        exec(compile(source, "<old-head-control>", "exec"), module.__dict__)
    test = "tests/unit/test_orb_schwab_macd.py::test_imcc_shaped_full_history_negative_refuses_even_when_last_26_looks_positive"
elif case == "RED_A2":
    old_method("broker_adapters.schwab", "SchwabBrokerAdapter", "replace_bracket_order")
    test = ADAPTER+"::test_unknown_replace_never_claims_success"
elif case == "RED_A3":
    old_method("oms.service", "OmsRiskService", "process_trade_intent")
    test = ORDER+"::test_v2_own_add_not_blocked_by_orb_broker_read_and_flag_off_has_no_lock[True]"
else:
    name, attr, before, after, test = CASES[case]
    module = importlib.import_module("project_mai_tai." + name)
    obj = module
    for item in attr.split("."):
        obj = getattr(obj, item)
    source = inspect.getsource(obj)
    assert before in source, (case, "mutation target missing")
    install(module, attr, source.replace(before, after, 1))
sys.exit(pytest.main(["-p", "no:cacheprovider", "-q", test]))
