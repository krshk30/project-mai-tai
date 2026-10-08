"""Run one isolated in-memory source mutation; never edit the writer's source tree."""
import inspect
import sys
import textwrap
import pytest
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

CASES = {
    "ghost_kept": (SchwabV2Strategy, "recover_soft_rest_boot", "if self._retire_flip_owner_opportunity(state, reason=\"boot_soft_rest_proven_never_dispatched\"):", "if False:"),
    "attempt_ignored": (SchwabV2BotService, "_soft_rest_boot_proofs", "len(snapshots) != 1", "len(snapshots) < 1"),
    "stale_admitted": (SchwabV2Strategy, "recover_soft_rest_boot", "not 0 <= self._now_ms() - observed <= FLIP_OWNER_EVIDENCE_MAX_AGE_MS", "False"),
    "account_coverage_removed": (SchwabV2Strategy, "recover_soft_rest_boot", "set(proof.get(\"flat_accounts\", ())) != accounts", "False"),
    "intent_protection_barrier_removed": (SchwabV2BotService, "_soft_rest_boot_proofs", "if any(session.scalar(query.limit(1)) is not None for query in barriers):", "if False:"),
    "broker_holding_ignored": (SchwabV2BotService, "_soft_rest_boot_proofs", "if any(symbol in held for _, held in books.values()):", "if False:"),
    "filled_phase_admitted": (SchwabV2Strategy, "soft_rest_boot_candidates", 'record.phase == "resting"', "True"),
    "refresh_allowed": (SchwabV2BotService, "_soft_rest_boot_positions", "if schwab._adapter_refresh_enabled:", "if False:"),
}
cls, method, before, after = CASES[sys.argv[1]]
original = getattr(cls, method)
source = textwrap.dedent(inspect.getsource(original))
assert source.count(before) == 1
namespace = dict(original.__globals__)
exec(compile(source.replace(before, after), "<mutation>", "exec"), namespace)
setattr(cls, method, namespace[method])
raise SystemExit(pytest.main(["tests/unit/test_slotclear1_soft_rest_boot.py", "-q", "--disable-warnings"]))
