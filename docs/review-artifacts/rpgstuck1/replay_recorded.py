"""Read-only offline before/after replay of E1 tickets and E3 authorizations."""
from copy import deepcopy
from datetime import UTC, datetime
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

data = json.loads(Path("tests/fixtures/rpgstuck1_recorded.json").read_text())
result = {"source": inspect.getfile(SchwabV2Strategy), "symbols": {}, "canonical": {}}
for symbol in ("APUS", "VEEA"):
    jobs = {row["id"]: row["payload"] for row in data["tickets"] if row["payload"]["old"]["symbol"] == symbol}
    state = SimpleNamespace(symbol=symbol, fanout_segment_id=next(iter(jobs.values()))["segment_id"])
    strategy = SimpleNamespace(_rpg_handoffs=jobs)
    owned = SchwabV2Strategy._rpg_entry_owned(strategy, state)
    accounts = {}
    for account in ("live:schwab_1m_v2", "live:orb"):
        accounts[account] = (SchwabV2Strategy._rpg_entry_owned(strategy, state, account=account)
            if "account" in inspect.signature(SchwabV2Strategy._rpg_entry_owned).parameters else owned)
    result["symbols"][symbol] = {"phases": [job["phase"] for job in jobs.values()], "global_owned": owned,
                                 "account_owned": accounts, "requested_durable": 0}
for row in data["tickets"]:
    if row["payload"]["old"]["broker_account_name"] != "live:schwab_1m_v2":
        continue
    token, job = row["id"], deepcopy(row["payload"])
    intent = next(item for item in data["intents"] if item["payload"].get("metadata", {}).get("rpg_handoff_token") == token)
    event = TradeIntentEvent.model_validate(job["authorization"]["event"])
    event.event_id = UUID(intent["payload"]["event_id"])
    event.payload.metadata = intent["payload"]["metadata"]
    job["phase"] = "submitting"
    service = object.__new__(OmsRiskService)
    service.settings = Settings(oms_v2_emit_native_oco_bracket_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2")
    service._rpg_dispatch_token = token
    service._rpg_now = lambda: datetime.fromtimestamp(job["authorization"]["at"], UTC)
    service._rpg_journal = lambda: SimpleNamespace(read=lambda *a, **kw: job)
    result["canonical"][event.payload.symbol] = {"refusal": service._rpg_open_refusal(event),
        "authorization_prices": [job["authorization"]["event"]["payload"]["metadata"][key] for key in ("stop_price", "limit_price")],
        "wire_prices": [event.payload.metadata[key] for key in ("stop_price", "limit_price")],
        "quantity": str(event.payload.quantity)}
print(json.dumps(result, indent=2))
