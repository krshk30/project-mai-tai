"""Isolated in-memory controls; never mutate source or call a broker."""

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
BASE = "90106fb4ebbb1feda7247d420352114d14dcd084"
CASES = {
    "reset_deadline_each_minute": ("orb_schwab_exits", "completed_bar_evidence",
        "datetime.fromisoformat(at) + timedelta(minutes=1)", "now.replace(second=0, microsecond=0)",
        EXIT + "::test_atr_pending_deadline_survives_minute_roll_and_context_reload"),
    "forget_missing_across_runs": ("orb_schwab_exits", "completed_bar_evidence",
        'set(prior.get("atr_missing_minutes", []))', "set()",
        ORDER + "::test_missing_later_atr_bar_pages_once_at_91_seconds_after_reload"),
    "pending_body_sells": ("orb_schwab_exits", "exit_signal",
        ' or context.get("body_status") == "pending_break_bar"', "",
        EXIT + "::test_pending_body_cannot_sell_even_if_an_inconsistent_context_contains_ohlc"),
    "page_pending_atr": ("orb_schwab_exits", "save_context",
        '{"complete", "pending_last_closed_schwab_bar"}', '{"complete"}',
        ORDER + "::test_missing_later_atr_bar_pages_once_at_91_seconds_after_reload"),
    "hide_read_failure": ("orb_schwab_exits", "save_context",
        "if body_missing or atr_missing:", 'if context.get("body_status") != "pending_break_bar" and (body_missing or atr_missing):',
        ORDER + "::test_unreadable_atr_is_not_hidden_by_pending_body"),
    "drop_incident_dedup": ("orb_schwab_exits", "save_context",
        "if exists is None:", "if True:",
        ORDER + "::test_missing_later_atr_bar_pages_once_at_91_seconds_after_reload"),
    "drop_v2_db_flag": ("oms.service", "OmsRiskService.process_trade_intent",
        'bool(getattr(self.settings, "orb_live_schwab_orders_enabled", False))\n                and event.payload.intent_type == "open"',
        'True\n                and event.payload.intent_type == "open"',
        ORDER + "::test_flag_off_v2_skips_orb_db_collision_even_with_orb_owned_position"),
}


def install(module, attr, source):
    function = attr.split(".")[-1]
    exec(compile(textwrap.dedent(source), "<grace-control>", "exec"), module.__dict__)
    if "." in attr:
        setattr(getattr(module, attr.split(".")[0]), function, module.__dict__[function])


def run(case):
    if case == "RED_BASE":
        module = importlib.import_module("project_mai_tai.orb_schwab_exits")
        old = subprocess.check_output(["git", "show", BASE + ":src/project_mai_tai/orb_schwab_exits.py"], text=True)
        exec(compile(old, "<base-exits>", "exec"), module.__dict__)
        tests = [EXIT, ORDER]
    elif case == "BASE_FLAG_OFF":
        module = importlib.import_module("project_mai_tai.oms.service")
        old = subprocess.check_output(["git", "show", "fc68b238:src/project_mai_tai/oms/service.py"], text=True)
        cls = next(node for node in ast.parse(old).body if isinstance(node, ast.ClassDef) and node.name == "OmsRiskService")
        fn = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_trade_intent")
        install(module, "OmsRiskService.process_trade_intent", "\n".join(old.splitlines()[fn.lineno - 1:fn.end_lineno]))
        tests = [ORDER + "::test_flag_off_v2_skips_orb_db_collision_even_with_orb_owned_position"]
    elif case in {"three_second_grace", "never_expire"}:
        module = importlib.import_module("project_mai_tai.orb_schwab_exits")
        module.SCHWAB_BAR_EVIDENCE_GRACE_SECONDS = 3 if case == "three_second_grace" else 100000
        tests = [EXIT + "::test_break_bar_waits_90_seconds_from_its_close",
                 ORDER + "::test_missing_later_atr_bar_pages_once_at_91_seconds_after_reload"]
    else:
        name, attr, before, after, test = CASES[case]
        module = importlib.import_module("project_mai_tai." + name)
        obj = module
        for part in attr.split("."):
            obj = getattr(obj, part)
        source = inspect.getsource(obj)
        assert source.count(before) == 1, (case, "mutation must have exactly one target")
        install(module, attr, source.replace(before, after, 1))
        tests = [test]
    return pytest.main(["-p", "no:cacheprovider", "-q", *tests])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(run(sys.argv[1]))
    unexpected = []
    for case in ["RED_BASE", "BASE_FLAG_OFF", "three_second_grace", "never_expire", *CASES]:
        result = subprocess.run([sys.executable, __file__, case], cwd=ROOT, text=True, capture_output=True)
        expected = 0 if case == "BASE_FLAG_OFF" else 1
        verdict = ("BASELINE PASS" if expected == 0 else "KILLED/RED") if result.returncode == expected else "UNEXPECTED"
        print(case, verdict, flush=True)
        print(result.stdout[-1600:], result.stderr[-500:], flush=True)
        if result.returncode != expected:
            unexpected.append(case)
    sys.exit(bool(unexpected))
