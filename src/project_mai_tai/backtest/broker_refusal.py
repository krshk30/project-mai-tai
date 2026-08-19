"""R4 — the broker REFUSES. Model it, with the observed classes kept separate.

⛔⭐⭐ THE DEFECT THIS FIXES. The replay engine's implicit broker model is **"orders fill."** The
real one refuses constantly — 82 rejected entry orders on `live:schwab_1m_v2` over six sessions
(2026-08-10 → 08-17). An engine that fills a name Schwab will not accept books P&L from a trade that
**could never have happened**, and that is why the engine has never found a defect the tape found.

## The observed taxonomy — from Schwab's OWN book, cross-checked against our stored reasons

| class | Schwab book | our DB | modelled as |
|---|---|---|---|
| `NOT_ELECTRONICALLY_TRADEABLE` | ~41 (21 syms) | 40 | **the order never exists — exclude the name** |
| `TRIGGER_NOT_ABOVE_ASK` | ~35 (7 syms) | 34 | reject at submit; depends on the tape |
| `INSUFFICIENT_BUYING_POWER` | 1 | 1 | reject at submit; account-level |
| `CLIENT_ABORT` | 7 | 7 | the order never reached the book at all |

⛔ **The four classes behave differently and MUST NOT be collapsed into one refusal rate.**
"Not electronically tradeable" removes the trade entirely and is a property of the SYMBOL;
"trigger not above ask" is a submit-time reject that depends on where the tape was at that instant.
A single blended rate would model neither.

## ⛔⭐ THE LIST IS DERIVED, NEVER HAND-EDITED
`derive_refused_symbols` reads the reject reasons we already store
(`broker_orders.payload->>'reject_reason'`, 82/82 populated). It takes no curated input and has no
override hook, deliberately.

**If anyone ever needs to add a symbol by hand, that is the signal this has stopped being execution
modelling and become strategy** — at which point it belongs in a different conversation, not in a
tuning constant here.

⭐ Offline-derivable on purpose: sourcing from our own DB rather than a live Schwab call keeps a
replay reproducible and lets it run against a fixture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class RefusalClass(str, Enum):
    """Why the broker refused. Kept separate because they are modelled differently."""

    NOT_ELECTRONICALLY_TRADEABLE = "not_electronically_tradeable"
    TRIGGER_NOT_ABOVE_ASK = "trigger_not_above_ask"
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"
    CLIENT_ABORT = "client_abort"


# Verbatim fragments from the broker's own `statusDescription` / our stored `reject_reason`.
# ⛔ Matched on the MESSAGE TEXT, not on an error code: the codes name the REQUIRED relation rather
# than the violation (`STOP_LOSS_PRICE_LT_MARKETPRICE` reads as its own opposite), which is how a
# whole week of diagnosis went the wrong way on 2026-08-17.
_PATTERNS: tuple[tuple[RefusalClass, re.Pattern[str]], ...] = (
    (
        RefusalClass.NOT_ELECTRONICALLY_TRADEABLE,
        re.compile(r"opening transactions for this security must be placed with a broker", re.I),
    ),
    (
        RefusalClass.NOT_ELECTRONICALLY_TRADEABLE,
        re.compile(r"not eligible for electronic entry", re.I),
    ),
    (
        RefusalClass.TRIGGER_NOT_ABOVE_ASK,
        re.compile(r"stop price must be above the current ask", re.I),
    ),
    (
        RefusalClass.INSUFFICIENT_BUYING_POWER,
        re.compile(r"not have enough available cash/buying power", re.I),
    ),
    # Ours, not the broker's — these never reached the book.
    (RefusalClass.CLIENT_ABORT, re.compile(r"read operation timed out", re.I)),
    (RefusalClass.CLIENT_ABORT, re.compile(r"unable to resolve host", re.I)),
    (RefusalClass.CLIENT_ABORT, re.compile(r"upstream connect error|reset before headers", re.I)),
    (RefusalClass.CLIENT_ABORT, re.compile(r"application encountered unexpected error", re.I)),
    # ⛔⭐⭐ OUR OWN RuntimeError, STORED AS "Webull order rejected" (P3, measured 2026-08-19).
    # 544 rows on `live:orb` in 30 days carry `RuntimeError('Webull combo MASTER must be LIMIT or
    # MARKET ...; got STOP_LIMIT')` — the #16 dead-mirror population (542 attempts over 08-14..18),
    # aborted CLIENT-SIDE before the order ever reached Webull. The abort patterns above were all
    # SCHWAB-shaped network failures, so every one of these classified as a BROKER refusal.
    # ⛔ That is the abort/refusal conflation with a name: 544 of that account's 12,138 rejects are
    # not the broker refusing us, they are us never asking. Any "Webull reject rate" computed before
    # this was measuring our own bug.
    (RefusalClass.CLIENT_ABORT, re.compile(r"combo MASTER must be", re.I)),
    (RefusalClass.CLIENT_ABORT, re.compile(r"RuntimeError", re.I)),
)


def classify_refusal(reason: str | None) -> RefusalClass | None:
    """The refusal class for a stored reject reason, or None if unrecognised.

    ⛔ Returns None rather than guessing. An unrecognised reason must surface as UNKNOWN in the
    caller's denominator, never be folded into the nearest class — that is how a taxonomy silently
    stops matching the broker.

    ⛔ A reason can carry MORE THAN ONE message (Schwab pipe-joins them). First match wins, and the
    order above puts the symbol-level classes first: a name that is not electronically tradeable is
    untradeable regardless of what else was wrong with the order.
    """
    if not reason:
        return None
    text = str(reason)
    for klass, pattern in _PATTERNS:
        if pattern.search(text):
            return klass
    return None


@dataclass(frozen=True)
class RefusalModel:
    """The refusal facts a replay needs, all DERIVED.

    `refused_symbols` — names the broker will not accept electronically at all. The engine must not
    trade them; their absence is the single largest correction to a replayed P&L.
    `counts` — per-class totals, so every run can state its own denominator (R7).
    `unclassified` — reasons the taxonomy did not recognise. ⭐ A non-zero value here is a signal the
    broker changed its wording, not something to round away.
    """

    refused_symbols: frozenset[str]
    counts: dict[RefusalClass, int]
    unclassified: tuple[str, ...]
    # ⛔⭐⭐ THE DENOMINATOR IS LOAD-BEARING, AND `unclassified` IS NOT IT.
    # `unclassified` is DEDUPED (distinct wordings, for reading), so it answers "how many kinds of
    # reason did we fail to recognise" — never "how much of the population". Measured 2026-08-19 on
    # 30 days of live:schwab_1m_v2: 85 unclassified ROWS carrying just 2 distinct reasons, so the
    # header read `UNCLASSIFIED=2` while a THIRD of the population (85/258, 32.9%) was unclassified.
    # A taxonomy that has stopped matching the broker would look trivial exactly when it matters.
    unclassified_rows: int = 0

    def is_refused(self, symbol: str) -> bool:
        return str(symbol or "").upper() in self.refused_symbols


def build_refusal_model(rows: list[tuple[str, str | None]]) -> RefusalModel:
    """Build the model from `(symbol, reject_reason)` rows. Pure — no DB, no network.

    The caller supplies the rows so this stays testable against a fixture and so the DB query lives
    with the other data access rather than in the model.
    """
    refused: set[str] = set()
    counts: dict[RefusalClass, int] = {k: 0 for k in RefusalClass}
    unknown: list[str] = []
    unknown_rows = 0
    for symbol, reason in rows:
        klass = classify_refusal(reason)
        if klass is None:
            # ⛔ Count the ROW even when the reason is empty. A reject we stored with no reason is
            # still a reject we could not classify; dropping it shrinks the denominator silently.
            unknown_rows += 1
            if reason:
                unknown.append(str(reason)[:120])
            continue
        counts[klass] += 1
        if klass is RefusalClass.NOT_ELECTRONICALLY_TRADEABLE:
            refused.add(str(symbol or "").upper())
    return RefusalModel(
        refused_symbols=frozenset(refused),
        counts=counts,
        unclassified=tuple(dict.fromkeys(unknown)),
        unclassified_rows=unknown_rows,
    )


REFUSAL_ROWS_SQL = """
    SELECT bo.symbol, bo.payload->>'reject_reason'
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    WHERE ba.name = :account
      AND bo.side = 'buy'
      AND bo.client_order_id LIKE '%%-open-%%'
      AND bo.status = 'rejected'
      AND bo.submitted_at >= :start
      AND bo.submitted_at < :end
"""


def header(model: RefusalModel, *, account: str, start: datetime, end: datetime) -> str:
    """R7/R9 — the population, window and account, stated BEFORE any number."""
    parts = [f"{k.value}={v}" for k, v in model.counts.items() if v]
    total = sum(model.counts.values()) + model.unclassified_rows
    line = (
        f"REFUSAL MODEL | account={account} | window={start:%Y-%m-%d}..{end:%Y-%m-%d} | "
        f"rows={total} | {' '.join(parts) or 'no refusals'} | "
        f"refused_symbols={len(model.refused_symbols)}"
    )
    if model.unclassified_rows:
        # ⛔ ROWS first, then distinct wordings. The row count is the share of the population the
        # taxonomy did not recognise; the distinct count only says how many kinds there were.
        pct = 100.0 * model.unclassified_rows / total if total else 0.0
        line += (
            f" | ⛔ UNCLASSIFIED={model.unclassified_rows} rows ({pct:.1f}% of population), "
            f"{len(model.unclassified)} distinct reason(s)"
        )
    return line
