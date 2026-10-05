"""Mutate only an in-memory policy function; never edit a gate or a live file."""
import importlib.util
import inspect
from pathlib import Path

import pytest

PATH = Path(__file__).with_name("test_standing_allowance.py").resolve()
spec = importlib.util.spec_from_file_location("standing_tests", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
mutations = {
    "fingerprint": ('or payload.get("fingerprint") != "position-quantity:" + ACCOUNTS[0] + ":" + symbol', "or False"),
    "balance": ('or quantity(payload.get("net_fill_balance")) != ALLOWANCES[symbol]', "or False"),
    "zero-account": ('"account_quantity", "virtual_quantity", "managed_quantity"))', '"virtual_quantity", "managed_quantity"))'),
    "current-session": ('for row in result["net_bot_fills"]:', "for row in []:"),
    "degraded-error": ('or set(details) != {"total_findings", "critical_findings", "run_status", "cutover_confidence"}', "or False"),
    "deepcopy": ("adjusted = copy.deepcopy(overview)", "adjusted = overview"),
    "broker-fresh": ('fresh(result["direct_read_started_at"][account], now, account + " direct read")', "pass"),
    "cached-identities": ('if sorted(map(identity, cached_findings)) != sorted(map(identity, findings)):', 'if False:'),
    "cached-title": ('row["title"]', '"ignored"'),
}
extra_mutations = {
    "webull-alias-exposure": ("webull_positions", 'if "holdings" in body and "positions" in body and body["holdings"] != body["positions"]:', 'if False:'),
    "webull-completion": ("webull_positions", 'if not markers or any(type(marker) is not bool for marker in markers) or len(set(markers)) != 1:', 'if markers and (any(type(marker) is not bool for marker in markers) or len(set(markers)) != 1):'),
    "schwab-identity": ("schwab_holdings", 'or body.get("accountNumber") != expected_number', 'or False'),
}
sources = {name: inspect.getsource(getattr(module.gate, name)) for name in
           ("standing_allowance", "webull_positions", "schwab_holdings")}


class MutationPlugin:
    def __init__(self, changed, function):
        self.changed = changed
        self.function = function

    def pytest_collection_modifyitems(self, items):
        for item in items:
            namespace = dict(vars(item.module.gate))
            for name, original in sources.items():
                exec(original, namespace)
                setattr(item.module.gate, name, namespace[name])
            exec(self.changed, namespace)
            setattr(item.module.gate, self.function, namespace[self.function])


for name, (function, old, new) in {
        **{name: ("standing_allowance", old, new) for name, (old, new) in mutations.items()},
        **extra_mutations}.items():
    source = sources[function]
    if name == "cached-title":
        new = '"ignored"'
        source = source.replace('row["payload"]["title"]', '"ignored"')
    assert source.count(old) == 1, (name, source.count(old))
    changed = source.replace(old, new)
    if name == "webull-completion":
        changed = changed.replace('more = markers[0]', 'more = markers[0] if markers else False')
    code = pytest.main(["-q", str(PATH)], plugins=[MutationPlugin(changed, function)])
    print("MUTATION", name, "RED" if code == 1 else "NOT_ASSERTION_RED", code, flush=True)
    assert code == 1, (name, code)
