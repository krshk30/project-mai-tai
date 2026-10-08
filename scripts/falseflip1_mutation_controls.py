"""Isolated semantic mutations of the pure FALSEFLIP1 groundwork, never live code."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/project_mai_tai/falseflip1.py"
MUTATIONS = {
    "entry_order_match_removed": (
        "        or identity.entry_order_id != identity.fill_order_id\n", ""),
    "closed_bar_guard_removed": (
        'bar.observation_phase != "live" or bar.observed_at_ms < bar.bar_ms + 60000',
        'bar.observation_phase != "live"'),
    "replay_guard_removed": (
        'bar.observation_phase != "live" or bar.observed_at_ms < bar.bar_ms + 60000',
        'bar.observed_at_ms < bar.bar_ms + 60000'),
    "short_price_comparison_removed": (
        'if bar.state == "short" and close < trail:', 'if bar.state == "short":'),
    "real_classification_is_false": (
        'return Classification("REAL_FLIP", "entry_bar_closed_long", identity, bar)',
        'return Classification("FALSE_FLIP", "entry_bar_closed_long", identity, bar)'),
    "missing_filled_sibling_guard_removed": (
        '        or {value.account for value in identities} != filled_accounts\n', ""),
    "held_sibling_close_guard_removed": (
        'return all((value.account, value.managed_row_id) in closed_rows\n               for value in identities)',
        'return True'),
    "false_exit_label_removed": (
        'return f"false_flip_{normalized or \'unknown\'}"', 'return mechanism'),
}


def main() -> None:
    original = SOURCE.read_text()
    receipts = []
    for name, (before, after) in MUTATIONS.items():
        if original.count(before) != 1:
            raise RuntimeError(f"mutation boundary is not unique: {name}")
        with tempfile.TemporaryDirectory(prefix=f"falseflip1-{name}-") as temporary:
            overlay = Path(temporary) / "project_mai_tai"
            overlay.mkdir()
            (overlay / "__init__.py").write_text(
                f"__path__ = [{str(overlay)!r}, {str(ROOT / 'src/project_mai_tai')!r}]\n")
            (overlay / "falseflip1.py").write_text(original.replace(before, after))
            environment = {**os.environ, "PYTHONPATH": temporary + os.pathsep + str(ROOT / "src")}
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "tests/unit/test_falseflip1_step0.py"],
                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60,
            )
            output = result.stdout + result.stderr
            assertion_red = result.returncode == 1 and "AssertionError" in output and "ERROR collecting" not in output
            receipts.append(dict(mutation=name, rc=result.returncode, assertion_red=assertion_red,
                                 output=output))
    print(json.dumps(receipts, indent=2))
    if not all(row["assertion_red"] for row in receipts):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
