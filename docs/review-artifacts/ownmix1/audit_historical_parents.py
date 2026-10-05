"""Inventory retained poll/open comparisons without inferring parent ownership from size."""
from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal
from pathlib import Path


def audit(evidence):
    opens = {}
    comparisons = []
    for record in evidence["history"]:
        line = record["line"]
        match = re.search(r"\[OMS-V2-MANAGED-OPEN\] sym=(\S+) acct=(\S+) qty=([0-9.]+)", line)
        if match:
            symbol, account, quantity = match.groups()
            opens[account, symbol] = (quantity, record["at"])
        match = re.search(r"\[OMS-OCO-EXIT-POLL\] (\S+) (\S+).*?qty=([0-9.]+)", line)
        if match:
            account, symbol, quantity = match.groups()
            prior = opens.get((account, symbol))
            comparisons.append({
                "account": account, "symbol": symbol, "poll_at": record["at"],
                "poll_quantity": quantity, "prior_open_at": prior[1] if prior else None,
                "prior_open_quantity": prior[0] if prior else None,
                "quantity_matches": bool(prior and Decimal(prior[0]) == Decimal(quantity)),
                "parent_ownership": "UNMEASURED",
                "reason": "retained poll/open lines have no durable managed-entry binding or parent",
            })
    return {
        "source": "ownmix-rpgstuck-own-read.json",
        "retained_poll_count": len(comparisons),
        "quantity_matches": sum(row["quantity_matches"] for row in comparisons),
        "verified_correct_historical_parents": 0,
        "historical_equal_quantity_ownership_unmeasured": sum(row["quantity_matches"] for row in comparisons),
        "comparisons": comparisons,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence")
    args = parser.parse_args()
    result = audit(json.loads(Path(args.evidence).read_text()))
    assert result["retained_poll_count"] == 42 and result["quantity_matches"] == 41
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
