# OPEN ITEMS

> Threads that are genuinely **open**. One line each in [`session-handoff.md`](session-handoff.md).
>
> ⛔ **KEEP IT SHORT. The rule that let this rot: items were only ever ADDED.**
> - When something closes, **MOVE it to the log** — do not leave it here marked ✅.
> - A *study you are not going to run* is not an open item.
> - A *standing rule* belongs in **memory**, not here — it will never be "done".
> - A *dormant* item (its feature is switched off) is closed, not carried.

---

## 1. 🔬 Re-run the backtest-vs-live comparison on a STABLE-CODE day
Config parity is FIXED and verified (89/90, #592); what is unproven is whether the engine reproduces
a live day end-to-end. Only STKH has ever matched (+1.90% both).

⛔ **2026-07-30 is UNUSABLE** — 11 PRs deployed mid-session, plus bar holes from deliberate stops.
⛔ Respect the three structural limits: ONE round trip per symbol-day (`if exit_done: break`), quote
density ~1/4s, sparse-bar symbols uncomparable.
⛔ **NEW:** filter `WHERE source='live'` — backfilled bars now exist (#624) and must not be mixed in.
[[project_mai_tai_backtest_live_parity_audit]]

## 2. 🐌 polygon / strategy-engine freeze — **HALF CLOSED**
✅ The trade-coach half is resolved: it was burning 45% of the box while DISABLED, in a 429 retry
storm on a dead API key. Stopped 07-30; load 2.2 -> 1.37.
🔴 The freeze itself remains: 60-80s loop stalls at open/close, ~72% CPU in the JSON snapshot encode.
`#366` (snapshot-persist throttle) is **BUILT AND NEVER DEPLOYED**.

⭐ **New evidence 07-30:** `dashboard_snapshots` regrew **14 MB -> 96 MB in four minutes** after a
VACUUM FULL. That is `_replace_dashboard_snapshot` on the hot path — the same write that is 72% of
the CPU. **#366 is now the root of two open items and is the cheapest available win.**
[[project_mai_tai_polygon_freeze]]

## 3. ⛔ Schwab API-open rejects ~3/day and NOTHING evicts
`"Opening transactions for this security must be placed with a broker. Contact us"` — a DIFFERENT
symbol every day, so it is not one bad ticker: 07-30 ×1, 07-29 ×3, 07-28 ×3, 07-27 ×3, 07-23 ×7.
**~20 lost entries in a week.** No `scanner_blacklist_entries` row is created; `#326`'s eviction does
not fire on this reject reason, and the symbol stays `armed=True` as a live candidate.
⭐ Reframes "fewer entries are expected (volume floor)" — part of the shortfall is THIS.

## 4. ⛔⭐ An UNNAMED suppression stops the retry on a broker-refused symbol
STKH 07-30: 09:53 open intent -> 1 broker order -> REJECTED. 09:55 open intent -> **no order row at
all**. All three `risk_checks` read `outcome=pass, reason=ok`, and `oms.log` has **zero** STKH lines
in that window. An intent that passes risk, creates no order, and is marked `rejected` — silently.

⭐⭐ **Currently PROTECTIVE** (it stops us hammering Schwab with an order it structurally refuses)
**but nobody can name the mechanism.** Both of this month's worst defects were this exact shape:
#580 was a latch that silently stopped repricing once lost; #608 hid behind an overlapping guard
("overlapping guards hide a dead one"). A suppression nobody can name can stop working unnoticed —
and the failure mode here is a reject storm against a live broker.
⇒ Find the path that marks a risk-passed open intent `rejected` without emitting an order. If it is
the INTENDED guard, it deserves a log line and a test; if not, the real guard is missing.

## 5. ⛔ A Schwab rejection vetoes the WEBULL leg too
The fan-out itself works — proved on APLX/SNDG 07-30, both legs firing 5s apart. But when the Schwab
leg is REJECTED, Webull is never attempted, so a name Schwab refuses via API is traded on **NEITHER**
broker. [[feedback_assess_both_brokers]] — settle whether this is "Schwab can't trade this name" or
"we can't trade this name".

## 6. 📊 Order churn: 284 broker orders -> 23 round trips (12:1)
Resting-entry reprice churn. Invisible to the recorder (closed round trips only), fully visible on
the live tape. ⛔ Understand this before the trade-coach redesign — it is a large part of what the
bot actually *does* all day.

## 7. ⛔ IRE: a Schwab REPLACE spawned an order we never recorded
Our books said 2 shares, the broker said 4. Schwab's own order list:

    1007401978921  REPLACED  qty 2  filled 0   BUY  16:47:29  tag=TA_krshk30gmail...   <- ours
    1007401979166  FILLED    qty 2  filled 2   BUY  16:47:29  tag=API_TOS:TraderAPI    <- the phantom

**We never issue a replace** — the adapter is DELETE-only, no PUT anywhere. The successor filled at
the same second with a different tag. ⛔ `CANCELLED_STATUSES` includes `"REPLACED"`, so we map it to
`cancelled` and stop looking — **a replaced order has a successor and we never go find it.**

**Parked by operator decision 07-30: catch it live next time.** #626 now surfaces the resulting
position drift within ~8 minutes instead of hours.

## 8. ⛔⭐ Reconciler severity is INVERTED — an UNOWNED position pages CRITICAL
*(found 2026-07-31 from a live AZIO page; operator: "add it to the list, we can work on it later")*

The operator hand-bought **972 AZIO** on `live:orb` and got a RED page for their own trade. Ours?
**0 orders / 0 intents / 0 fills / 0 bars**, and the finding's own payload said `strategy_codes: []`.

`reconciliation/service.py`:

| line | what it does |
|---|---|
| **203** | `keys = set(aggregates) \| set(account_positions)` — the **UNION**, so every hand-placed broker position becomes something to check |
| **216** | `severity = "critical" if account_quantity == 0 or virtual_quantity == 0 else "warning"` |
| **229** | computes `strategy_codes` — and throws the answer away |

⭐ A position we never traded has `virtual_quantity == 0` **by definition**, so L216 makes it
**guaranteed CRITICAL**. **The less we know about a position, the louder it screams** — while a real
drift on a position we DO own (both quantities non-zero, disagreeing) is only a *warning*. Backwards.

⛔ **TWO SEPARATE IGNORE LISTS.** `MAI_TAI_PROTECTED_SYMBOLS` gates the **OMS**;
`reconciliation_ignored_position_mismatch_pairs` gates the **reconciler**. The alert cron separately
filters PROTECTED_SYMBOLS via `EXCLUDE_SQL` — which is why CYN wrote **923 findings on 07-31 and
pushed ZERO**, while AZIO (unprotected) paged. ⛔ **DB row count ≠ page count; read `EXCLUDE_SQL`
before calling the channel noisy.**

**Fix:** make severity **attribution-aware** — the data is already in the payload. No `strategy_codes`
AND no orders/fills/intents for that (account, symbol) ⇒ not ours ⇒ info, never pages.
Owned-and-disagreeing stays CRITICAL. This removes the need to pre-register anything, which is the
point: the operator hand-trades all day and cannot maintain a list of every symbol touched.

✅ **The OMS side is CLEAN.** `oms.log` and the v2 log had **zero** mentions of AZIO — the acting
invariant held and the manual position was never at risk. Reconciler-only change; nothing on the
trading path.

---

## ⚠️ Watch items live in [`session-handoff.md`](session-handoff.md), not here
Verification is a *state* ("is this behaving?"), not a *task* ("do this"). Keeping them here is what
made an open-items file that could never reach zero.
