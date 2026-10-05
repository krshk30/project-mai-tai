"""Offline assessment of recorded October 5 evidence; no broker/DB/Redis writes."""
import argparse
from datetime import datetime, UTC
from decimal import Decimal
import json
import re
from types import SimpleNamespace

from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.oms.atr_reprice_runtime import AtrRepriceRuntimeMixin
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence")
    parser.add_argument("followup")
    args = parser.parse_args()
    with open(args.evidence) as handle:
        evidence = json.load(handle)
    with open(args.followup) as handle:
        followup = json.load(handle)
    jobs = {row["id"]: row["payload"] for row in evidence["queries"]["rpg_jobs"]}
    authorizations = {row["id"]: row["authorization"] for row in followup["rpg_authorizations"]}
    for symbol in ("APUS", "VEEA"):
        own_jobs = {key: job for key, job in jobs.items() if job["old"]["symbol"] == symbol}
        state = SimpleNamespace(symbol=symbol, fanout_segment_id=next(iter(own_jobs.values()))["segment_id"])
        owner = SimpleNamespace(_rpg_handoffs=own_jobs)
        owned = SchwabV2Strategy._rpg_entry_owned(owner, state)
        print(f"{symbol} recorded_phases={[job['phase'] for job in own_jobs.values()]} entry_owned={owned} requested_jobs=0")
        assert owned is True
    for row in evidence["queries"]["reprice_intents"]:
        md = row["payload"].get("metadata", {})
        token = md.get("rpg_handoff_token")
        if not token or row["account"] != "live:schwab_1m_v2":
            continue
        auth = authorizations[token]
        event = TradeIntentEvent.model_validate(auth["event"])
        event.payload.metadata = md
        event.payload.quantity = Decimal(row["quantity"])
        # Restore the recorded dispatch phase for its pre-wire comparison only.
        job = {**jobs[token], "phase": "submitting", "authorization": auth}
        service = SimpleNamespace(
            settings=Settings(), _rpg_dispatch_token=token,
            _rpg_now=lambda: datetime.fromtimestamp(auth["at"], UTC),
            _rpg_journal=lambda: SimpleNamespace(read=lambda *a, **kw: job),
        )
        reason = AtrRepriceRuntimeMixin._rpg_open_refusal(service, event)
        keys = [key for key in ("stop_price", "limit_price", "cw_flip_level", "cw_entry_slot", "fanout_segment_id", "fanout_slot_id")
                if auth["event"]["payload"]["metadata"].get(key) != md.get(key)]
        print(f"{event.payload.symbol} wire_guard={reason} mismatched_keys={keys}")
        assert reason == row["payload"]["refusal_code"] == "rpg_current_price_size_or_identity_changed"
    opens = {}
    comparisons = []
    for row in evidence["history"]:
        line = row["line"]
        match = re.search(r"\[OMS-V2-MANAGED-OPEN\] sym=(\S+) acct=(\S+) qty=([0-9.]+)", line)
        if match:
            symbol, account, quantity = match.groups()
            opens[account, symbol] = Decimal(quantity)
        match = re.search(r"\[OMS-OCO-EXIT-POLL\] (\S+) (\S+).*?qty=([0-9.]+)", line)
        if match:
            account, symbol, quantity = match.groups()
            prior = opens.get((account, symbol))
            comparisons.append((account, symbol, prior, Decimal(quantity)))
    missing = sum(prior is None for _, _, prior, _ in comparisons)
    mismatches = [row for row in comparisons if row[2] is not None and row[2] != row[3]]
    print(f"exit_poll_lines={len(comparisons)} prior_open_missing={missing} qty_matches={len(comparisons)-missing-len(mismatches)} mismatches={mismatches}")
    assert len(comparisons) == 42 and missing == 0 and len(mismatches) == 1
    print("Assessment replay COMPLETE; this reproduces defects, not a fix or a readiness PASS.")


if __name__ == "__main__":
    main()
