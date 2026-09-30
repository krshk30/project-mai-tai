"""Summarize read-only capture windows without inventing event-arrival causality."""

import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


rows = list(csv.DictReader(Path(sys.argv[1]).open(newline="", encoding="utf-8")))
counts = Counter()
examples = defaultdict(list)
for row in rows:
    minute_mod = int(row["reject_at"][14:16]) % 5
    if not row["intent_created_at"]:
        counts["unmatched_intent"] += 1
        continue
    prints = int(row["exchange_prints_2s"])
    quotes = int(row["gateway_quotes_2s"])
    label = "no_print_no_quote" if prints == 0 and quotes == 0 else (
        "print_no_quote" if prints and not quotes else
        "quote_no_print" if quotes and not prints else "print_and_quote"
    )
    counts[f"mod{minute_mod}:{label}"] += 1
    counts[label] += 1
    counts[f"mod{minute_mod}"] += 1
    examples[label].append({
        "case_id": int(row["case_id"]), "symbol": row["symbol"],
        "reject_at": row["reject_at"], "intent_created_at": row["intent_created_at"],
        "prints_2s": prints, "quotes_2s": quotes,
        "nbbo_states_10s": int(row["nbbo_states_10s"]),
        "last_gateway_quote": row["last_gateway_quote"],
    })

print(json.dumps({"rows": len(rows), "counts": counts, "cases": examples}, indent=2))
