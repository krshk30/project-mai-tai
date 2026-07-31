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

## 9. ⛔⭐ Redis evicts the HEARTBEAT stream -> a false "oms-risk fleet down" RED page
*(found 2026-07-31 from a live page; operator: "add it to the list")*

The page said `NO heartbeat in mai_tai:heartbeats (zombie signature / fleet down)`. **The OMS was
completely healthy** — `active (running)`, 12h uptime, **NRestarts=0**, heartbeat 0s old, and
**zero gaps >60s in `oms.log`**. Re-running the watchdog by hand returned `VERDICT: OK`.

**Root cause: Redis eviction, not the OMS.**

    maxmemory        512 MB
    maxmemory_policy allkeys-lru      <- evicts ANY key
    evicted_keys     185

`mai_tai:heartbeats` is **47 KB** — a trivial LRU victim. When Redis hits the ceiling it drops the
whole key; the watchdog then finds no `oms-risk` entry and emits the scariest verdict it owns.
Caught mid-recovery: `XLEN` climbed **30 -> 52 -> 59** over three samples a minute apart — the stream
repopulating after eviction, not a service coming back.

**What fills Redis:**

| stream | entries | memory |
|---|---|---|
| **`mai_tai:snapshot-batches`** | **26** | **180 MB** |
| `mai_tai:market-data` | 41,069 | 15 MB |
| `mai_tai:strategy-state` | 57 | 3.8 MB |
| `mai_tai:heartbeats` | 59 | 0.05 MB |

~7 MB per snapshot batch (~13k symbols + reference data) — the SAME oversized payload behind the
polygon freeze and #366. `redis_snapshot_batch_stream_maxlen=180` therefore authorises **~1.26 GB
against a 512 MB budget**.

### ⛔⭐ DO NOT "fix" THIS BY CUTTING THE MAXLEN — it is load-bearing
`publisher.py` defaults this to **4**, which makes 180 look like drift. It is not.
`_prefill_alert_history_from_snapshot_batches` requests `count = squeeze_10min_needs`
= `_snaps_per_10min` = **120 batches** at the 5s snapshot interval. 180 = 120 required + headroom.

Cutting to 4 leaves the momentum scanner with no squeeze history after ANY strategy-engine restart:
~5 min blind on the 5-min squeeze, **~10 min blind on the 10-min squeeze**. Squeeze -> CONFIRM ->
watchlist -> v2 entries, so it costs REAL ENTRIES on every restart (there were 11 restarts on 07-30).
⭐ Researched before landing on the list precisely because the obvious fix was the wrong one.

**Viable directions instead:**
1. **Shrink the payload** — same root as #366; reference data re-sent in full every 5s cycle is the bulk.
2. **Raise `maxmemory`** (512 MB on a 4 GB box) — buys headroom, does not stop growth.
3. **Reconsider `allkeys-lru`** — live operational state should not be silently evictable. ⛔ This is
   the bigger hazard: today it took the heartbeat stream and produced a false page; it could equally
   take something load-bearing and produce silent misbehaviour.

---

## 10. ⛔⭐⭐ SELECTION: we buy stocks whose move is already SPENT — scanner AND bot
*(operator, 2026-07-31, from the AXTU chart: "we may have to drop the whole stock... this is some of
the stock we don't wanna play there. That's something we need to do from the scanner, from our bot,
everywhere. Make a note. We will discuss.")*

⭐ **This is the SELECTION lever, and it is the one we have never pulled.**
[[project_mai_tai_30s_exploration]] closed 279 trades net-negative under EVERY exit and concluded
"SELECTION is the only lever left"; [[project_mai_tai_v2_stop_slippage_rootcause]] proved four ways
that the ENTRY, not the exit geometry, is the problem. The operator has now arrived at the same place
from the chart. Do not re-open exit tuning to solve this.

### The worked example — AXTU, 2026-07-31 (+54.5% on the day BEFORE we touched it)

| hour ET | range | median bar volume |
|---|---|---|
| 10:00 | 8.4% | 4,388 |
| 11:00 | 7.6% | 1,300 |
| 12:00 | **5.1%** | **1,200** |

Range compressing, volume dying, the 50% move already made. **We bought it THREE times during that
decay** (11:15 @3.80 -> stopped ~3.61 ≈ −5%; 12:03 @3.745; 12:18 @3.86) plus a Webull fan-out leg.

⭐ **Operator's criterion, in their words:** what we want is a stock that "goes down, then comes up
20%, then goes down" — one that still OSCILLATES. A name that has made its move and gone quiet has no
swing left to capture, and its volume is too thin to trade even if it did. We are systematically
buying exhaustion.

### Why the vol floor cannot fix this on its own
The floor is judged on **ONE completed bar**. AXTU 11:15 armed off the 11:14 bar (**10,467**, clearing
the 10,000 floor by 4.7%) and filled 32s later into a bar that closed at **2,999**. AXTU's median
minute today was **2,217**, and only **21 of 125 bars (17%)** cleared 10,000 — the gate only has to
sample one spike. ⛔ #625's re-check could not fire: the fill beat the next bar close by 25s.
⇒ A rolling-window liquidity test (median of last N bars) is the minimum fix, but it is a PATCH.
The real question is whether the name should have been a candidate at all.

### Scope when we build it — the operator was explicit: **everywhere**
1. **Scanner** — stop CONFIRMING names whose move is spent (a confirm today fires on a squeeze that
   has already happened).
2. **Bot** — a per-symbol tradeability gate at arm time (oscillation + live liquidity), not one bar.
3. **Backtest** — whatever rule lands must be replayable, or we cannot measure it.

⛔ **DISCUSS BEFORE BUILDING** (operator: "We will discuss"). Open design questions: what measures
"still oscillating" (Kaufman ER is already computed in
[[project_mai_tai_backtest_engine]]'s param sweep and classified CLRO correctly); how much of the
day's move is "spent"; and the sample-size trap — this must not become another >100-configs-on-27-
trades overfit.

---

## 11. ⛔⭐⭐ PRE-MARKET OCO HOLE — the structural fix for the KUST class (PROMOTED, top item)
*(operator 2026-07-31: promote it — "every entry under a native OCO means the broker owns the exit,
so there's no software-ladder churn path to ride to the stop")*

A pre-market / EH entry gets **no native OCO** (`[V2-OCO-EMIT] SKIPPED (outside regular hours)`), so
the **software ladder** owns its exit — and the ladder is what cancel/replaced KUST's fillable sell
NINE times while the bid sat at or above the limit, riding a right signal to −5.17%.

**The fix:** get every position under a broker OCO — either emit one the instant RTH opens for any
position still held from pre-market, or harden the EH software exit specifically.

⛔⭐ **"OCO ⇒ churn-immune" is NOT unconditional — design for this or the fix is partial.**
Cancelled/rejected sells within 60 min of an **OCO-bracketed** entry: NVVE 07-23 **11**, KUST 07-22
6, FIEE 07-27 6, several at 3. The mechanism is visible in the OMS log:

    [OMS-OCO-STAND-DOWN-CLEARED] live:schwab_1m_v2 KUST — OCO gone; ladder deferred ...

When the stand-down CLEARS, the software ladder resumes and can churn **even on a bracketed entry**.
So emitting a bracket is necessary but not sufficient; the stand-down-clear path needs its own
answer. *(Caveat: that count is symbol-level in a time window — some sells may belong to another
position the same day.)*

⭐ **Why this is now the highest-leverage execution item:** it eliminates the failure mode rather
than detecting it, and it makes the operator's 1–2 week v2 live-validation a clean STRATEGY
measurement instead of a strategy+execution mixture. The backward execution-% study is a dead end
(see the log, 07-31), so the live run IS the measurement — it has to be clean.

---

## ⚠️ Watch items live in [`session-handoff.md`](session-handoff.md), not here
Verification is a *state* ("is this behaving?"), not a *task* ("do this"). Keeping them here is what
made an open-items file that could never reach zero.
