"""Isolated in-memory mutations; never change source bytes or production state."""
import inspect
import json
import subprocess
import sys
import textwrap

CASES = {
    "N1-no-release": {"_rpg_release_unwired": [("if (job[\"phase\"] not in", "if (True or job[\"phase\"] not in")]},
    "N2-wire-generation-veto": {"_rpg_persisted_local_open": [("if candidates or not", "if False or not")]},
    "N3-positive-origin": {"_rpg_persisted_local_open": [(" or payload.get(\"refusal_origin\") != \"skipped_before_submit\"", "")]},
    "N4-slot-identity": {"_rpg_matches_local_open": [("and previous.get(\"fanout_slot_id\") == slot", "and True")]},
    "N5-quantity": {
        "_rpg_persisted_local_open": [("or opening.payload.quantity != event.payload.quantity", "or False")],
        "_rpg_release_unwired": [(" or opening.payload.quantity != old.quantity", "")],
    },
    "N6-revision-CAS": {"_rpg_release_unwired": [(" or row.payload[\"revision\"] != job[\"revision\"]", "")]},
    "N7-fill-evidence": {
        "_rpg_persisted_local_open": [("if session.scalar(select(Fill.id)", "if False and session.scalar(select(Fill.id)")],
        "_rpg_release_unwired": [("if session.scalar(select(Fill.id)", "if False and session.scalar(select(Fill.id)")],
    },
    "N8-wire-hints": {"_rpg_persisted_local_open": [("if (payload.get(\"broker_order_id\")", "if (False and payload.get(\"broker_order_id\")"),
        ("or previous.get(\"broker_order_id\")", "or False"),
        ("or previous.get(\"webull_wire_submitted_at_utc\")", "or False"),
        ("or (\"webull_local_no_wire\" in previous and previous[\"webull_local_no_wire\"] != \"true\")", "or False")]},
    "N9-retained-wires": {"_rpg_release_unwired": [("or data.get(\"wire_submissions\", 0) != 0", "or False")]},
    "N10-retained-unknown": {"_rpg_release_unwired": [("or data[\"phase\"] not in {\"held\", \"queued\", \"prepared\", \"retired\"}", "or False")]},
    "N11-stale-queue-token": {"_rpg_release_unwired": [("\"phase\": \"prepared\", \"token\": \"\"", "\"phase\": \"prepared\", \"token\": data[\"token\"]")]},
    "N12-feedback": {"_rpg_release_unwired": [("\"phase\": \"refused\", \"reason\": \"replacement_refused\"", "\"phase\": \"clear\", \"reason\": \"replacement_refused\"")]},
    "N13-no-rebuy": {"_rpg_release_unwired": [("or job.get(\"no_rebuy\") or job.get(\"replacement_filled\")", "or False")]},
    "N14-wired-loop-wake": {"_rpg_advance": [(
        'if starting_phase == "held_unknown" and job["phase"] in ACTIVE_PHASES - {"held_unknown"}:\n'
        '        self._rpg_retry_dirty = True\n        self._rpg_retry_signal().set()', 'if False:\n        pass')]},
    "N15-account-proof": {
        "_rpg_persisted_local_open": [("BrokerAccount.name == event.payload.broker_account_name,", "True,")],
        "_rpg_release_unwired": [("BrokerAccount.name == old.broker_account_name, Strategy.code", "True, Strategy.code")],
    },
    "N16-generation-proof": {
        "_rpg_persisted_local_open": [('TradeIntent.payload["metadata"]["rpg_resting_generation"].as_string() ==\n'
            '            event.payload.metadata["rpg_resting_generation"]', 'True')],
        "_rpg_matches_local_open": [('and all(not md.get(key) or previous.get(key) == md[key] for key in\n'
            '                    ("rpg_resting_generation", "webull_mirror_generation_id"))', 'and True')],
    },
    "N17-positive-filled-metadata": {"_rpg_persisted_local_open": [("or filled != 0", "or False")]},
    "N18-four-phase-admission": {"_rpg_release_unwired": [(
        'job["phase"] not in {"refused", "expired", "clear", "held_unknown"}', 'False')]},
    "N19-idempotent-terminal-feedback": {"_rpg_release_unwired": [(
        'or job.get("release_reason") == "old_local_no_wire_return_to_strategy"', 'or False')]},
    "N20-locked-terminal-phase": {"_rpg_release_unwired": [(
        'or row.payload["phase"] != job["phase"]', 'or False')]},
}


def mutate(changes):
    from project_mai_tai.oms import atr_reprice_runtime as module
    cls = module.AtrRepriceRuntimeMixin
    for method, replacements in changes.items():
        original = getattr(cls, method)
        source = textwrap.dedent(inspect.getsource(original))
        for old, new in replacements:
            assert source.count(old) == 1, (method, old, source.count(old))
            source = source.replace(old, new)
        scope = dict(module.__dict__)
        exec(compile(source, module.__file__, "exec"), scope)
        setattr(cls, method, staticmethod(scope[method]) if method == "_rpg_matches_local_open" else scope[method])


if __name__ == "__main__":
    if len(sys.argv) == 2:
        mutate(CASES[sys.argv[1]])
        import pytest
        target = ("tests/unit/test_rpgstuck1_loop_liveness.py::test_l7_wired_terminal_zero_controlled_recovery_real_loop_keeps_advancing"
            if sys.argv[1] == "N14-wired-loop-wake" else "tests/unit/test_rpgnowire1_proof_release.py")
        raise SystemExit(pytest.main(["-q", "-p", "no:cacheprovider", target]))
    results = []
    for name in CASES:
        result = subprocess.run([sys.executable, __file__, name], capture_output=True, text=True, timeout=90)
        failures = [line for line in result.stdout.splitlines() if line.startswith("FAILED ")]
        results.append({"mutation": name, "rc": result.returncode,
            "verdict": "RED" if result.returncode == 1 and failures else "SURVIVED" if result.returncode == 0 else "ERROR",
            "failed_tests": failures, "tail": result.stdout.splitlines()[-1:] + result.stderr.splitlines()[-2:]})
        print(json.dumps(results[-1]), flush=True)
    raise SystemExit(0 if all(row["verdict"] == "RED" for row in results) else 1)
