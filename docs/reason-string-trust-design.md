# P9 — What a "reason" is worth: one finding, two contracts

**Status:** finding + proposal. Written 2026-08-19. No code change in this document.

> ## ⛔⭐⭐ THE ONE-LINE RULE
> **A reject reason is a CLAIM BY THE BROKER, NOT A FACT.** Where a reject can be checked against
> what we actually sent, **check it**. The 84 rejects of 2026-07-22/23 are the proof that the text
> and the truth can diverge *completely*.

---

## The convergence — three things that turned out to be one

1. **Broker reason text can be flatly wrong.** B14: 84 orders rejected with
   `"Limit price cannot be zero for limit orders."` — and **not one of them had a zero limit
   price.** The values sent were `12.6962`, `9.6170`, `4.3025`, `2.1228`, `0.7737`. The message was
   the *entire* stored reason; there was no second pipe-joined clause hiding the real cause.
2. **The §27/§121 taxonomy classifies by that text.** `backtest/broker_refusal.py::classify_refusal`
   matches regexes against the broker's prose to decide whether a name is untradeable.
3. **`trade_reasons.py` exists to ban substring-matching reason strings — and is imported by
   nothing.** So the ban is enforced nowhere. (Found by the §137 inert-module lint, one day after
   the same lint's motivating case.)

Put together: **we classify execution behaviour off strings we have proven can be false, using a
discipline module that is not connected to anything.**

---

## ⛔ But the two "reason" fields have DIFFERENT contracts. Do not merge them.

This is the part that must not be flattened into a single fix. `trade_reasons.py`'s own docstring
already draws the line, and it is right:

| | **OUR reasons** — `trade_intents.reason` | **BROKER prose** — `broker_orders.payload->>'reject_reason'` |
|---|---|---|
| shape | structured: `emitter:RULE`, or a bare rule | free-form vendor text, no structure |
| authored by | us | the broker |
| failure mode | **substring-matching conflates distinct populations** | **the text can be factually FALSE** |
| correct handling | parse, then compare the RULE for **equality** against an explicit set | do not trust alone — **check against what we sent** |
| worked example | `%HARD_STOP%` swept 3 distinct populations (872 / 542 / 471) | `"Limit price cannot be zero"` on a limit price of `12.6962` |

⛔ **The fixes are opposite in kind.** For our own strings the bug is *imprecision* and the fix is
*parse-and-equality*. For broker prose the bug is *untruth* and no amount of parsing helps — the
only defence is corroboration against the order we actually transmitted. Applying the
`trade_reasons` discipline to broker text would be a category error, and the module says so.

---

## ⛔ #729 IS UNAFFECTED — do not re-open it

#729 added two `CLIENT_ABORT` patterns for `combo MASTER must be` / `RuntimeError`. That change
**splits by SOURCE, not by classifying broker prose**: a `RuntimeError` is *our own exception text*,
stored in a field the OMS filled in on our side of the wire. The order never reached Webull, so
there is no broker claim to doubt. It answers "who produced this string", which is a fact about our
code, not "is the broker's account of events accurate".

The classes that DO rest on broker prose are `NOT_ELECTRONICALLY_TRADEABLE`,
`TRIGGER_NOT_ABOVE_ASK` and `INSUFFICIENT_BUYING_POWER`. Those are what §141 asks us to re-examine.

---

## Re-examining what the taxonomy actually rests on

`NOT_ELECTRONICALLY_TRADEABLE` is the load-bearing one: it is the only class that **removes a name
from the replay universe entirely** (30 days). Its whole evidentiary basis is the sentence
*"Opening transactions for this security must be placed with a broker"*.

⛔ **Two counts appear below and they are NOT a discrepancy — state the predicate with the number.**
`REFUSAL_ROWS_SQL` filters `side='buy' AND client_order_id LIKE '%-open-%'` and yields **58**
symbols / 92 rows. The corroboration query below drops the side/coid filters (any rejected order
carrying that sentence) and yields **56**. The model's own predicate is the 58.

**Is that sentence checkable against what we sent?** Partly — and this is the useful test:

- A refusal that is a property of the **SYMBOL** should reproduce across *different* orders, prices,
  sizes and days. If a name is refused on every attempt regardless of what we sent, the claim is
  corroborated by our own data without trusting the prose.
- A refusal that reproduces only for a **particular order shape** (a price precision, an order type,
  a session) is not a symbol property, whatever the text says. That is exactly the B14 shape — the
  text named a zero limit price; the truth was an order-shape problem on 48 of 84, and something
  still unidentified on the other 36.

### ⛔ The obvious corroboration rule DOES NOT WORK, and the reason matters

The natural proposal is: *a name enters `refused_symbols` only if it was refused on ≥2 attempts
that differ in order shape.* **Measured, it collapses — and not because the evidence is weak.**

| over 30 days, `live:schwab_1m_v2` | count |
|---|---|
| symbols refused "must be placed with a broker" | **56** |
| …with ≥2 attempts | 12 |
| …with ≥2 distinct session dates | 11 |
| **…with exactly ONE attempt** | **44 (79%)** |

The first read of that is "79% of our universe exclusions rest on a single uncorroborated broker
sentence". **That read is wrong**, and checking it was the whole point:

> **All 56 are in `schwab_ineligible_today`** — 71 cache rows covering 56 symbols. After the first
> refusal the symbol is cached ineligible for that session and **we deliberately do not retry.**

So the single attempt is **our own caching working as designed**, not thin evidence. A rule
demanding a second attempt asks the system to spend a real rejected order to re-prove something it
already recorded — and the cache exists precisely to stop that. ⛔ **The rule is unsatisfiable by
construction. Withdrawn.**

⇒ **The honest position:** corroboration from our own order book is *largely unavailable*, and that
is a structural consequence of a correct optimisation, not carelessness. The symbol-level class
therefore does rest on one broker sentence per name — and there is no cheap way to make it rest on
more **from order flow**.

⇒ **If corroboration is wanted, it must come from a READ, not an order.** Schwab exposes instrument
metadata; if it carries a non-electronic / broker-only flag, that is an independent second source
costing no order and no risk. **Unverified — nobody has checked whether that field exists.** That is
the next cheap step, and it is a question about the API, not about our data.

---

## ⛔ Why `trade_reasons.py` cannot simply be "wired in"

Searched the whole repository. **There is no committed call site that substring-matches a structured
reason.** Both incidents it was written for — 08-04 (`ILIKE '%HARD_STOP%'`, headline inflated 75%)
and 08-05 (`%hard_stop%`, reported 385/394 when the truth was 1/394) — were **ad-hoc SQL typed by a
human**, not code.

So the module's intended consumer is *a person writing a one-off query*, and **a Python module
cannot reach that consumer.** Importing it somewhere to satisfy the lint would be worse than leaving
it inert: it would look enforced while enforcing nothing — the same defect one layer up.

**The fix that would actually work is database-side**, because that is where the bug happens:

```sql
CREATE OR REPLACE VIEW v_trade_intent_reasons AS
SELECT ti.*,
       split_part(ti.reason, ':', 1) AS emitter,
       CASE WHEN ti.reason LIKE '%:%' THEN split_part(ti.reason, ':', 2)
            ELSE ti.reason END       AS rule
FROM trade_intents ti;
-- then:  WHERE rule = ANY(ARRAY['HARD_STOP','HARD_STOP_NATIVE_BACKUP'])   -- explicit, never LIKE
```

An ad-hoc query reaches for a view; it does not import a Python module. **Not created here** — it is
a production database object and belongs to an operator decision, not a side effect of a write-up.

Until then `trade_reasons.py` stays in `KNOWN_INERT` with its reason recorded, which is honest:
the ban it encodes is documentation, and documentation is not enforcement.

---

## What this changes

- ⭐ **Any study that classified execution behaviour from broker prose inherits the prose's
  reliability**, which B14 showed can be zero. Say so where such a number is quoted.
- The **refused-symbol list** (58 names) is the highest-stakes consumer, because it deletes names
  from a replay universe. Corroborate before trusting.
- `trade_reasons.py` is **not enforcement**. Anyone reading its docstring and assuming the rule is
  applied somewhere is wrong.

## Pointers

`backtest/broker_refusal.py` · `trade_reasons.py` · `tests/unit/test_no_inert_modules.py` ·
[[feedback_a_wrong_reason_is_worse_than_a_missing_one]] ·
[[project_mai_tai_broker_order_events_conflates_client_aborts]] ·
[[feedback_authoritative_for_a_is_not_for_b]]
