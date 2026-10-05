"""Run isolated ownership mutations in memory; never edit production source."""
from __future__ import annotations

import argparse
import inspect
import subprocess
import sys
import textwrap

MUTATIONS = {
    "order_id": ("lookup", "                BrokerOrder.id == entry_id,\n", ""),
    "client_id": ("lookup", "                BrokerOrder.client_order_id == entry_coid,\n", ""),
    "strategy": ("lookup", "                Strategy.code == row.strategy_code,\n", ""),
    "parent": ("detail", 'str(detail.get("entry_broker_order_id") or "") == str(entry.broker_order_id or "")', "True"),
    "pair": ("detail", 'detail.get("exit_base_client_order_id") == base', "True"),
    "quantity": ("detail", 'quantity == Decimal(row.current_quantity)', "True"),
    "virtual_strategy": ("restore", "                    Strategy.code == managed.strategy_code,\n", ""),
    "f1_unbound_flat_gate": ("flat", "        attributed = False\n", "        if entry is None:\n            return False\n        attributed = False\n"),
    "f1_mismatched_flat_gate": ("flat", "        attributed = False\n", "        if not self._owned_exit_detail_matches(row, entry, detail):\n            return False\n        attributed = False\n"),
    "f2_poll_entry_id": ("poll", "                            entry_order_id=order.id,\n", ""),
    "f2_submit_entry_id": ("submit", "                    entry_order_id=order.id,\n", ""),
    "u1_unsafe_memory_fallback": ("base", "        key = (broker_account_name, symbol.upper())\n", "        key = (broker_account_name, symbol.upper())\n        if self.__dict__.get('_webull_protect_base', {}).get(key):\n            return self._webull_protect_base[key]\n"),
    "u2_webull_standdown": ("refresh", "                if self._is_v2_webull_account(key[0]):\n                    continue  # Webull protection is addressed by its persisted pair handle.\n", ""),
    "u3_one_working_leg": ("refresh", "len(working) >= 2", "len(working) >= 1"),
    "u4_absent_row_uuid": ("resolved", "            if not bound_row_id:\n                return False\n", ""),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutant", choices=MUTATIONS)
    args = parser.parse_args()
    if not args.mutant:
        killed = 0
        for name in MUTATIONS:
            result = subprocess.run([sys.executable, __file__, "--mutant", name],
                                    capture_output=True, text=True)
            failures = [line for line in result.stdout.splitlines() if line.startswith("FAILED ")]
            ok = result.returncode == 1 and bool(failures)
            print(f"{name}: {'KILLED' if ok else 'SURVIVED_OR_ERROR'}")
            for failure in failures:
                print(failure)
            killed += ok
        print(f"ownership_mutations_killed={killed}/{len(MUTATIONS)}")
        return 0 if killed == len(MUTATIONS) else 1

    import pytest
    import project_mai_tai.oms.service as service
    import project_mai_tai.oms.store as store

    kind, before, after = MUTATIONS[args.mutant]
    cls, module, method = {
        "lookup": (service.OmsRiskService, service, "_find_oco_entry_order"),
        "detail": (service.OmsRiskService, service, "_owned_exit_detail_matches"),
        "restore": (store.OmsStore, store, "restore_virtual_positions_from_managed"),
        "flat": (service.OmsRiskService, service, "_close_confirmed_flat_episode"),
        "poll": (service.OmsRiskService, service, "sync_broker_orders"),
        "submit": (service.OmsRiskService, service, "_record_order_reports"),
        "base": (service.OmsRiskService, service, "_oco_exit_base_for_entry"),
        "refresh": (service.OmsRiskService, service, "_refresh_native_oco_armed_state"),
        "resolved": (service.OmsRiskService, service, "_close_resolved_oco_managed_row"),
    }[kind]
    source = inspect.getsource(getattr(cls, method))
    assert source.count(before) == 1
    namespace = {}
    exec(compile(textwrap.dedent(source.replace(before, after)), "<ownership-mutant>", "exec"),
         module.__dict__, namespace)
    setattr(cls, method, namespace[method])
    return pytest.main(["tests/unit/test_ownmix1_entry_binding.py",
                        "tests/unit/test_ownmix1_review_followup.py", "-q"])


if __name__ == "__main__":
    raise SystemExit(main())
