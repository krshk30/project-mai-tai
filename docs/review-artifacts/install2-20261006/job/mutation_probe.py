"""Disposable runner-only guard mutations. Never mutates the repository or production."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

JOB = Path(__file__).resolve().parent
ROOT = JOB.parents[3]
TEST = JOB / "test_install2.py"
MUTANTS = (
    ("start-order", "release_policy.py",
     '("start", "oms"), ("start", "schwab-1m-v2"), ("start", "strategy")',
     '("start", "oms"), ("start", "strategy"), ("start", "schwab-1m-v2")',
     "test_literal_install2_daily_cumulative"),
    ("after16", "linesrc_disposition.py", "hour >= 16", "hour >= 0", "test_linesrc_near_miss_blocks"),
    ("duplicate-catalog", "catalog_policy.py", "len(result) == len(set(result))", "True",
     "test_catalog_duplicate_identity_blocks"),
    ("clean-exit", "attended.py", 'after["ExecMainStatus"] == 0', "True",
     "test_zero_result_nonzero_exit_is_not_clean_stop"),
)


def run(path, selected):
    env = dict(os.environ, PYTHONPATH=str(path) + os.pathsep + str(ROOT / "src") + os.pathsep + str(ROOT))
    return subprocess.run([sys.executable, "-m", "pytest", "--import-mode=importlib",
                           str(TEST), "-q", "-k", selected, "--tb=short", "--show-capture=no"],
                          cwd=ROOT, env=env, capture_output=True, timeout=120)


def main():
    receipts = []
    for name, file, old, new, selected in MUTANTS:
        control = run(JOB, selected)
        if control.returncode:
            print(control.stdout.decode() + control.stderr.decode())
            raise SystemExit("control failed: " + name)
        with tempfile.TemporaryDirectory(prefix="install2-mutation-") as temporary:
            destination = Path(temporary)
            for source in JOB.glob("*.py"):
                shutil.copyfile(source, destination / source.name)
            target = destination / file
            raw = target.read_text()
            if raw.count(old) != 1:
                raise SystemExit("mutation anchor ambiguous: " + name)
            target.write_text(raw.replace(old, new))
            result = run(destination, selected)
            killed = result.returncode == 1 and b"failed" in result.stdout and b"ERROR collecting" not in result.stdout
            receipts.append(dict(name=name, control_rc=control.returncode, mutant_rc=result.returncode,
                                 killed=killed, stdout=result.stdout.decode(), stderr=result.stderr.decode()))
            if not killed:
                print(json.dumps(receipts, indent=2))
                raise SystemExit("mutant survived or infrastructure failed: " + name)
    print(json.dumps(dict(scope="runner-only, not application combination acceptance",
                          killed=len(receipts), total=len(MUTANTS), receipts=receipts), indent=2))


if __name__ == "__main__":
    main()
