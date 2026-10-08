"""Private source overlays for runtime assertion controls; never edits this checkout."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = [
    ("skip_before_cancel_receipt", "strategy_core/schwab_1m_v2.py",
     "if budget.skip_due and budget.episodes[-1] not in budget.cancelled:", "if False:"),
    ("second_false_skip_delayed", "falseflip1_runtime.py",
     "return len(self.episodes) >= 2 and not self.skipped", "return len(self.episodes) >= 3 and not self.skipped"),
    ("skip_not_once", "falseflip1_runtime.py",
     "return len(self.episodes) >= 2 and not self.skipped", "return len(self.episodes) >= 2"),
    ("budget_cas_removed", "falseflip1_runtime.py",
     "if stored != before or after.revision != before.revision + 1:", "if False:"),
    ("owner_display_copy_removed", "falseflip1_runtime.py",
     'payload={**metadata, "entry_classification": result}', "payload=metadata"),
    ("late_conflict_invalidation_removed", "falseflip1_runtime.py",
     'if any({key: row.payload.get(key) for key in evidence} != evidence for row in rows):', "if False:"),
    ("all_sibling_close_guard_removed", "strategy_core/schwab_1m_v2.py",
     "if not false_episode_proven_closed(tuple(values), filled_accounts=accounts,",
     "if False and not false_episode_proven_closed(tuple(values), filled_accounts=accounts,"),
    ("cancel_receipt_barrier_removed", "strategy_core/schwab_1m_v2.py",
     'if opportunity not in budget.cancelled:\n            if state.symbol not in self._removed_wait_requests:',
     'if False:\n            if state.symbol not in self._removed_wait_requests:'),
    ("entry_skip_gate_removed", "strategy_core/schwab_1m_v2.py",
     'def _falseflip_entry_blocked(self, state: SymbolState) -> bool:\n        if not self._falseflip_enabled():',
     'def _falseflip_entry_blocked(self, state: SymbolState) -> bool:\n        if True:'),
    ("duplicate_above_cross_released", "strategy_core/schwab_1m_v2.py",
     "self._falseflip_cross_high.add(key)", "self._falseflip_cross_high.discard(key)"),
    ("real_close_refunded", "strategy_core/schwab_1m_v2.py",
     "return count - sum(value <= count for value in budget.refunded_counts)", "return 0"),
    ("default_off_worker_created", "oms/service.py",
     'if getattr(self.settings, "strategy_schwab_1m_v2_false_flip_enabled", False) is True\n            else None',
     'if True\n            else None'),
    ("inflight_db_writer_abandoned", "oms/service.py",
     "await asyncio.gather(work, return_exceptions=True)\n            raise", "raise"),
]


def main():
    receipts = []
    for name, file, before, after in MUTATIONS:
        original = (ROOT / "src/project_mai_tai" / file).read_text()
        if original.count(before) != 1:
            raise RuntimeError(f"non-unique boundary: {name}")
        with tempfile.TemporaryDirectory(prefix=f"falseflip-runtime-{name}-") as temporary:
            overlay = Path(temporary)/"project_mai_tai"
            shutil.copytree(ROOT/"src/project_mai_tai", overlay, ignore=shutil.ignore_patterns("__pycache__"))
            (overlay/file).write_text(original.replace(before, after))
            xml = Path(temporary)/"result.xml"
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short",
                "tests/unit/test_falseflip1_runtime.py", "--junitxml="+str(xml)], cwd=ROOT,
                env={**os.environ, "PYTHONPATH": temporary+os.pathsep+str(ROOT/"src")},
                capture_output=True, text=True, timeout=60)
            tests = list(ET.parse(xml).iter("testcase")) if xml.exists() else []
            failed = [t.attrib["name"] for t in tests if t.find("failure") is not None]
            errors = [t.attrib.get("name") for t in tests if t.find("error") is not None]
            receipts.append({"mutation": name, "rc": result.returncode, "assertion_red":
                result.returncode == 1 and bool(failed) and not errors,
                "failed_tests": failed, "errors": errors,
                "output": (result.stdout+result.stderr)[-16000:]})
    print(json.dumps(receipts, indent=2))
    if not all(r["assertion_red"] for r in receipts):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
