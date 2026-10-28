"""Explicit CONTROLLED evidence for old mechanics fixtures, not production defaults.

The three flag examples follow the retained October-5 snapshot (e42e5075...)
and C1 2026-10-23 10:19; the separate checked-in file is NOT that native snapshot.
The production classifier still determines eligibility, including size >=100.
"""

from dataclasses import replace
import hashlib
import json
from pathlib import Path

from project_mai_tai.atr_simulation.execution_eligibility import classify_execution, policy_from_payload
from project_mai_tai.atr_simulation.types import EvidenceKind, SourceRef, TradePrint


POLICY_PATH = Path(__file__).resolve().parents[1] / "fixtures/atr_simulation_controlled_condition_rules.json"
POLICY_BYTES = POLICY_PATH.read_bytes()
POLICY_SOURCE = SourceRef(str(POLICY_PATH), hashlib.sha256(POLICY_BYTES).hexdigest(),
                          "$", EvidenceKind.CONTROLLED)
POLICY = policy_from_payload(json.loads(POLICY_BYTES), POLICY_SOURCE)


def controlled_trade(key, symbol, price, size, *, conditions="14", present=True):
    """Keep every supplied raw operand; attach explicit controlled condition 14."""
    raw = TradePrint(key, symbol, price, size)
    return replace(raw, execution=classify_execution(raw, conditions, POLICY,
                                                    conditions_present=present))
