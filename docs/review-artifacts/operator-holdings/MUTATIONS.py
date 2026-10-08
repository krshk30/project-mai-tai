"""Assertion mutations in memory; never edit a checkout or live state."""
from pathlib import Path
import subprocess
import sys


MUTANTS = [
    ("order_count", "session_orders == 0 and session_fills == 0", "session_fills == 0"),
    ("fill_count", "session_orders == 0 and session_fills == 0", "session_orders == 0"),
    ("pending", "or pending_intents != 0", "or False"),
    ("sell", "or unowned_sells != 0", "or False"),
    ("fresh", "source_fresh is not True", "False"),
    ("complete", "or activity_complete is not True", "or False"),
    ("managed", "or managed_quantity != 0", "or False"),
    ("virtual", "or virtual_quantity != 0", "or False"),
    ("ledger", "or net_fill_balance != 0", "or False"),
    ("date", '.date() == date(2026, 10, 6)', '.date() != date(1999, 1, 1)'),
    ("account", 'and account_name == "live:schwab_1m_v2"', "and True"),
    ("symbol", 'and symbol == "IPDN"', "and True"),
    ("quantity", 'and broker_quantity == Decimal("1000")', "and True"),
]


def main():
    source = Path("src/project_mai_tai/operator_holdings.py").read_text()
    failed = []
    for name, old, new in MUTANTS:
        if source.count(old) != 1:
            raise RuntimeError("mutation fragment not unique: " + name)
        mutated = source.replace(old, new)
        script = (
            "import project_mai_tai.operator_holdings as module\n"
            f"exec(compile({mutated!r}, module.__file__, 'exec'), module.__dict__)\n"
            "import pytest\n"
            "raise SystemExit(pytest.main(['tests/unit/test_operator_holdings.py', '-q', '--tb=short']))\n"
        )
        run = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        assertion_failed = "AssertionError" in run.stdout and run.returncode == 1
        print(name, "RED" if assertion_failed else "SURVIVED/ERROR", flush=True)
        if not assertion_failed:
            failed.append(name)
            print(run.stdout[-3000:], run.stderr[-1000:])
    print(f"{len(MUTANTS) - len(failed)}/{len(MUTANTS)} assertion RED")
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
