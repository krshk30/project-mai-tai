"""Guard-removal probes compiled in memory; no source, broker or ledger writes."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "strict_flat_readonly.py"
MUTATIONS = {
    "dated_rule": ('if now.astimezone(ZoneInfo("America/New_York")).date() != IPDN_DATE:', 'if False:'),
    "direct1000": ('or quantity(holdings[0][2]) != IPDN_QUANTITY', 'or False'),
    "account1000": ('or rows[0]["symbol"] != "IPDN" or quantity(rows[0]["quantity"]) != IPDN_QUANTITY',
                    'or rows[0]["symbol"] != "IPDN" or False'),
    "source_freshness": ('for key in ("updated_at", "source_updated_at"):', 'for key in ("updated_at",):'),
    "bot_net_zero": ('if net != 0:\n                raise ValueError("IPDN current bot fill balance nonzero")',
                     'if False:\n                raise ValueError("IPDN current bot fill balance nonzero")'),
    "bot_books_zero": ('for key in ("managed_rows", "virtual_rows", "working_orders", "inflight_intents"):',
                       'for key in ("working_orders", "inflight_intents"):'),
    "pending_blocks": ('for key in ("managed_rows", "virtual_rows", "working_orders", "inflight_intents"):',
                       'for key in ("managed_rows", "virtual_rows"):'),
    "matching_finding_net": ('"virtual_quantity", "managed_quantity", "our_quantity", "net_fill_balance"))):',
                            '"virtual_quantity", "managed_quantity", "our_quantity"))):'),
}


def child(name):
    import pytest

    source = SOURCE.read_text()
    old, new = MUTATIONS[name]
    assert source.count(old) == 1, name
    changed = source.replace(old, new)

    class Patch:
        def pytest_collection_modifyitems(self, items):
            seen = set()
            for item in items:
                gate = item.obj.__globals__.get("gate")
                if gate is not None and id(gate) not in seen:
                    seen.add(id(gate))
                    exec(compile(changed, str(SOURCE), "exec"), gate.__dict__)

    return pytest.main([str(ROOT / "test_ipdn_residual.py"), "-q", "-p", "no:cacheprovider"], plugins=[Patch()])


if __name__ == "__main__":
    if len(sys.argv) == 2:
        raise SystemExit(child(sys.argv[1]))
    receipts = []
    for name in MUTATIONS:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), name],
                                capture_output=True, text=True, timeout=30)
        assertion_red = (result.returncode == 1
                         and ("AssertionError" in result.stdout or "DID NOT RAISE" in result.stdout)
                         and "ERROR collecting" not in result.stdout)
        receipts.append({"mutation": name, "rc": result.returncode, "assertion_RED": assertion_red})
    print(json.dumps(receipts, indent=2))
    raise SystemExit(0 if all(row["assertion_RED"] for row in receipts) else 1)
