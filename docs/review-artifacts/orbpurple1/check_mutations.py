"""Execute each mutant in a fresh process without changing repository files."""

import argparse
import importlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

TEST = "tests/unit/test_orbpurple1_atr_entry.py::"
MUTANTS = {
    "remove_producer_gate": ("project_mai_tai.services.orb_schwab_app",
                             "and self.settings.orb_schwab_atr_entry_gate_enabled",
                             "and False",
                             "test_apus_purple_is_withheld_and_rollback_restores_one_buy[True]"),
    "allow_purple": ("project_mai_tai.orb_schwab_atr_entry",
                     'allowed = result["state"] == "long" and close >= trail',
                     "allowed = True",
                     "test_all_recorded_placement_candidates_replay_through_live_gate[APUS-2026-10-05-5.07-5.421986558524449-False]"),
    "unknown_fail_open": ("project_mai_tai.orb_schwab_atr_entry",
                          'return AtrEntryGate("unknown", status)',
                          'return AtrEntryGate("allowed", status)',
                          "test_empty_or_unavailable_completed_history_never_passes[bars1-schwab_bar_read_unavailable]"),
    "remove_oms_admission_gate": ("project_mai_tai.oms.service",
                                 "elif self.settings.orb_schwab_atr_entry_gate_enabled:",
                                 "elif False:", "test_oms_cannot_bypass_producer_atr_gate[below_line]"),
    "remove_post_preview_gate": ("project_mai_tai.oms.service",
                                "if post_preview_refusal is None and self.settings.orb_schwab_atr_entry_gate_enabled:",
                                "if False:", "test_oms_rechecks_atr_after_preview_and_records_local_refusal"),
    "default_off": ("project_mai_tai.settings", "orb_schwab_atr_entry_gate_enabled: bool = True",
                    "orb_schwab_atr_entry_gate_enabled: bool = False",
                    "test_default_on_independent_from_paper_and_paper_service_is_unchanged"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutation", choices=MUTANTS)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/orbpurple1-mutations"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.mutation:
        module_name, old, new, test = MUTANTS[args.mutation]
        module = importlib.import_module(module_name)
        source = Path(module.__file__).read_text()
        assert source.count(old) == 1, (args.mutation, source.count(old))
        exec(compile(source.replace(old, new), module.__file__, "exec"), module.__dict__)
        return int(pytest.main(["-p", "no:cacheprovider", "-q", TEST + test,
                                f"--junitxml={args.output_dir / (args.mutation + '.xml')}"]))
    results = []
    for name in MUTANTS:
        result = subprocess.run([sys.executable, __file__, "--mutation", name,
                                 "--output-dir", str(args.output_dir)], capture_output=True, text=True)
        (args.output_dir / (name + ".log")).write_text(result.stdout + result.stderr)
        xml = args.output_dir / (name + ".xml")
        root = ET.parse(xml).getroot() if xml.exists() else None
        failed = [item.attrib["name"] for item in root.iter("testcase") if item.find("failure") is not None] if root is not None else []
        errors = list(root.iter("error")) if root is not None else ["no_xml"]
        row = {"mutation": name, "red": result.returncode == 1 and bool(failed) and not errors,
               "returncode": result.returncode, "failed_names": failed}
        results.append(row)
        print(json.dumps(row), flush=True)
    (args.output_dir / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    return 0 if all(row["red"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
