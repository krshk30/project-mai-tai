"""Run reader-hook and evidence mutations in disposable copies, never the writer tree."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
SERVICE = "src/project_mai_tai/oms/service.py"
SHADOW = "src/project_mai_tai/oms/wbquiet_shadow.py"
ORB = "src/project_mai_tai/oms/orb_schwab_eod.py"


def remove_call(source, function, reader=None):
    lines = source.splitlines(keepends=True)
    spans = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not isinstance(call.func, ast.Attribute) or call.func.attr != function:
            continue
        if reader is not None and (len(call.args) < 2 or not isinstance(call.args[1], ast.Constant)
                                   or call.args[1].value != reader):
            continue
        spans.append((node.lineno - 1, node.end_lineno))
    assert spans, (function, reader)
    for start, end in sorted(spans, reverse=True):
        lines[start:end] = [" " * (len(lines[start]) - len(lines[start].lstrip())) + "pass\n"]
    return "".join(lines)


def variants():
    for name, path in [("reconcile_tri_state", SERVICE), ("exit_snapshot_refresh", SERVICE),
                       ("orb_admission_positions", SERVICE), ("orb_close_positions", ORB),
                       ("reserve1_bracket_note_renewal", SERVICE)]:
        yield name, path, lambda source, name=name: remove_call(source, "note_consumed", name)
    yield "committed_marker", SERVICE, lambda source: remove_call(source, "note_committed")
    yield "precommit_marker", SERVICE, lambda source: source.replace(
        "        synced_positions = await self._run_db(_persist)\n"
        "        wbquiet_shadow.note_committed([name for aid, name in accounts if aid in account_ids])",
        "        wbquiet_shadow.note_committed([name for aid, name in accounts if aid in account_ids])\n"
        "        synced_positions = await self._run_db(_persist)", 1)
    for name, old, new in [
        ("window_expiry", "READER_WINDOW_SECONDS = 60", "READER_WINDOW_SECONDS = 600"),
        ("generation_bound", "deque(maxlen=MAX_WINDOWS)", "deque()"),
        ("account_bound", "len(self.committed_generations) >= MAX_ACCOUNTS", "False"),
        ("account_attribution", "and p.broker_account_name in names", "and True"),
        ("provider_exclusion", 'and provider != "schwab"', "and True"),
        ("adapter_pull_count", 'int(source == "adapter_positions")', "0"),
        ("publication_loss", '"shadow_receipt_emitted": published', '"shadow_receipt_emitted": True'),
        ("overflow_loss", "not anchors or self.reader_dropped", "not anchors"),
        ("failure_isolation", "if isinstance(observer, ShadowObserver):\n            observer.reader_dropped += 1",
         "raise"),
        ("empty_generation_guess", "0 < len(positions) <= 256", "0 <= len(positions) <= 256"),
        ("cache_identity_guess", "returned is stored and returned.broker_account_name == name", "True"),
        ("stale_generation_reuse", 'self.acquisition_generations[name] = "UNMEASURED"', "pass"),
    ]:
        yield name, SHADOW, lambda source, old=old, new=new: source.replace(old, new, 1)


def main():
    destination = Path(sys.argv[1])
    destination.mkdir(parents=True, exist_ok=True)
    receipts = []
    for name, relative, mutate in variants():
        with tempfile.TemporaryDirectory(prefix="wbquiet-reader-mutant-") as directory:
            root = Path(directory)
            for entry in ("src", "tests"):
                shutil.copytree(ROOT / entry, root / entry, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(ROOT / "pyproject.toml", root)
            path = root / relative
            original = path.read_text()
            changed = mutate(original)
            assert changed != original, name
            path.write_text(changed)
            env = {**os.environ, "PYTHONPATH": str(root / "src")}
            xml = destination / f"{name}.xml"
            run = subprocess.run([sys.executable, "-m", "pytest",
                                  "tests/unit/test_wbquiet_shadow_readers.py", "-q",
                                  f"--junitxml={xml}"], cwd=root, env=env,
                                 capture_output=True, text=True, timeout=120)
            log = destination / f"{name}.log"
            log.write_text(run.stdout + run.stderr)
            cases = list(ET.parse(xml).getroot().iter("testcase"))
            failures = [case.attrib["name"] for case in cases if case.find("failure") is not None]
            errors = [case.attrib["name"] for case in cases if case.find("error") is not None]
            receipt = {"mutation": name, "source": relative, "exit_code": run.returncode,
                       "assertion_failures": failures, "errors": errors,
                       "red": run.returncode == 1 and bool(failures) and not errors,
                       "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                       "source_sha256": hashlib.sha256(changed.encode()).hexdigest()}
            receipts.append(receipt)
            print(name, "RED" if receipt["red"] else "INVALID/GREEN", flush=True)
    (destination / "summary.json").write_text(json.dumps(receipts, indent=2) + "\n")
    return int(not all(receipt["red"] for receipt in receipts))


if __name__ == "__main__":
    raise SystemExit(main())
