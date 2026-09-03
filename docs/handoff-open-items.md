# OPEN ITEMS

> Threads that are genuinely **open**. One line each in [`session-handoff.md`](session-handoff.md).
>
> ⛔ **KEEP IT SHORT. The rule that let this rot: items were only ever ADDED.**
> - When something closes, **MOVE it to the log** — do not leave it here marked ✅.
> - A *study you are not going to run* is not an open item.
> - A *standing rule* belongs in **memory**, not here — it will never be "done".
> - A *dormant* item (its feature is switched off) is closed, not carried.
>
> ⛔ **Numbers are stable identifiers, not positions.** Items cite each other by number, so a deleted
> item leaves its number vacant (currently **5**, deleted 2026-08-14 as falsified — see item 13).
> Never renumber to close a gap.

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

> ✅ **UNBLOCKED 2026-08-14:** #704 supplies the discriminator this item was waiting for —
> `oms_managed_positions` distinguishes "ours" from "the operator's" without a maintained list.
> ⛔⭐⭐ **STILL DO NOT DOWNGRADE SEVERITY BLIND.** The CRITICAL alarm has TWO populations: the
> operator's manual holdings (round lots on `live:schwab_1m_v2`) **and our own positions whose
> `virtual_positions` row falsely reads 0** (qty 1 on `live:orb` — DSY/MB/NAMI/HUIZ, 08-07).
> Downgrading the severity would **suppress real defects**. The fix here is a **discriminator
> (ownership via `oms_managed_positions`), not a severity change.**
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

### ⛔⭐⭐ DESIGN CONSTRAINT (not a caveat) — the exit must NEVER silently fall back to the ladder

**A bracket only protects while it is live.** The OMS log shows the failure mode directly:

    [OMS-OCO-STAND-DOWN-CLEARED] live:schwab_1m_v2 KUST — OCO gone; ladder deferred ...

When the stand-down CLEARS, the timer-driven software ladder **resumes and owns the exit again** —
so a bracketed entry can churn exactly like KUST did. "OCO everything" was promoted to ELIMINATE the
churn path; if the exit silently reverts to the ladder the moment the bracket resolves or stands
down, the hole is only closed for as long as the bracket happens to be alive. **That is KUST again,
on a bracketed entry.**

⇒ **The pre-market fix is not done until the fallback is handled.** On bracket resolve/stand-down,
either:
  1. **re-arm a bracket**, or
  2. **apply the P0a marketable-hold discipline to that path too**
     (`_managed_exit_refresh_exempt` — hold a still-marketable exit instead of cancel/replacing it
     on the refresh cadence).
Never: hand the exit back to the bare timer.

Evidence that bracketed churn is real: cancelled/rejected sells within 60 min of an **OCO-bracketed**
entry — NVVE 07-23 **11**, KUST 07-22 6, FIEE 07-27 6, several at 3.
⛔ **Treat that as a FLAG, not a count.** The attribution is symbol-in-window, so some of those sells
may belong to a different position the same day. It establishes that bracketed churn EXISTS; it does
not measure how much.

⭐ **Why this is now the highest-leverage execution item:** it eliminates the failure mode rather
than detecting it, and it makes the operator's 1–2 week v2 live-validation a clean STRATEGY
measurement instead of a strategy+execution mixture. The backward execution-% study is a dead end
(see the log, 07-31), so the live run IS the measurement — it has to be clean.

---

## LIVE-MONEY board (operator letter codes — committed 2026-09-01, batch 2)

> ⛔ **Two batches lost time to definitions living outside the repo** (D21/W2B on 09-01 AM, then
> 11 of 21 rows on 09-01 PM). Rows are CREATED here from now on; a code with no row here is not a
> task. Closures move to [`handoff-log.md`](handoff-log.md) — per this file's own header rule.

### Open, defined, owned

- **OVSD1 — SCHWAB OVERSOLD REJECT STORM: the software close fires against shares a working
  broker OCO still reserves** *(owner: unassigned; **PARKED DELIBERATELY UNFIXED** by operator
  ruling 2026-09-03 — live money, high priority)*.
  **The event:** CHPT 2026-09-03, `live:schwab_1m_v2`. **205 oversold refusals in 8 minutes**
  (13:45:35 → 13:53:32 ET) plus **14 HTTP 429s in 14 SECONDS** (13:48:45 → 13:48:59). ⛔ Do not
  confuse windows: the **220 reconcile verdicts** span 13:39:04 → 13:53:31 (~14 min); the *rejects*
  span 8 min and the *429 burst* 14 s. Entry `1007821354133` filled 13:43; its OCO legs
  `1007821354135`/`...136` were working at the broker. The position was closed by **manual broker
  action** — no closing fill exists after 13:45:21.
  ⛔ **THE RULING AND WHY.** One instance, no proven root cause, and the candidate fixes (WRAP1, a
  confirmed-cancel gate, order adoption, the `>= 2` redesign) were a large speculative change to the
  **live exit path** built on a single day's evidence while blaming a rule deployed the day before.
  ⇒ **Nothing was built. Reopens on a SECOND INSTANCE WITH EVIDENCE.** `RATE1` is the alarm that
  makes the recurrence cost two minutes instead of an afternoon. [[feedback_a_wrong_reason_is_worse_than_a_missing_one]]
  ⭐⭐ **THE SHARPEST STATEMENT WE HAVE — a guess deletes a broker-confirmed fact.** `oms/service.py:3600`:
  on `result == "released"` the reconcile does `self._native_oco_armed_confirmed_at.pop((acct, symbol), None)`.
  That dict is the **stand-down**, fed by `fetch_armed_native_oco_symbols` — real broker truth. So a
  FALSE `released` does not merely permit the send: **it erases the confirmation that would have
  blocked it**, then `3669 if protection != "released": return` lets it through and `3671`'s
  stand-down is already disarmed. Once per tick.
  ⛔ **Why the release was false:** `schwab.py:255-256` `if not working: return "released"` — returned
  **before** the DELETE at 258 and the confirm re-read at 265-271. `walk()` reads `orderLegCollection[0]`,
  but a childless OCO **wrapper has no leg collection**, so `instruction == ""` and the wrapper's own
  `status` is dead code. A working wrapper holding the shares reads as no protection at all.
  ⛔ **NO CANCEL WAS EVER SENT, by either route.** `cancel_exit_pair` is implemented **only** in
  `webull.py:197`; `routing.py:59-60` hits `if fn is None: return []` on Schwab. And `requested = 2`
  at `service.py:2715` is a **hardcoded constant** — a label meaning "an OCO pair has two legs", not
  a count of requests. `reports=0 confirmed=0` is that `return []`, **not a silent broker**.
  ⛔ **A CONCURRENCY GUARD MISTAKEN FOR A REPETITION GUARD.** The reconcile re-enters on **every new
  quote tick**: `confirmation_inflight` prevents overlap, and `quote_at <= evaluated_at` is satisfied
  by any new tick. **220 `status=RELEASED` verdicts for one symbol in 14 minutes, ~1 every 3.9s** —
  and all-time the marker has **221 occurrences, all 2026-09-03, all CHPT**, so this path first ran
  that day. ⇒ Any future fix that reaches the DELETE **needs a once-per-episode bound**, or it turns
  220 false releases into 220 DELETE bursts. [[feedback_a_count_is_not_a_gate]]
  ⚠ **UNEXAMINED, and the most likely real hole:** `fetch_armed_native_oco_symbols` requires
  **`>= 2` working sell legs**. A bracket with ONE leg left reads **not armed** and the ladder
  resumes against shares a single working sell still reserves — and a partial cancel is exactly what
  produces one leg. **Not investigated. Start here on the second instance.**
  ⛔⭐⭐ **THIS IS OLD, NOT NEW — CONF1 DID NOT INTRODUCE IT.** Answered from `broker_orders`
  (table spans 2026-03-30 → 2026-09-03): **639 oversold refusals, ALL on `live:schwab_1m_v2`,
  across 20 ET days from 2026-07-01** — two months before CONF1 existed. **Four storm-scale days:**
  | ET day | rejects | symbols |
  |---|---|---|
  | 2026-07-13 | 127 | AGEN |
  | 2026-07-31 | 126 | FCUV, KUST |
  | 2026-08-04 | 115 | AAOG, AMIX |
  | **2026-09-03** | **205** | **CHPT** |
  ⇒ The operator's recollection of "something similar about a month ago" is **correct** (08-04).
  ⚠ **What this does NOT prove:** that the earlier storms share today's mechanism. CONF1 did not
  exist then, so the *trigger* differed; only the **refusal class** is shown to be old. The
  "one instance" that was parked is one instance **of the 3600 mechanism**, not of the storm class.
  ⛔ **Structural facts a future reader should not re-derive:**
  - A protective OCO leg is **never durably written at placement**. `-ocoexit-` rows: **471 in 30
    days, every one `filled`; zero working/cancelled/rejected. Zero `-protect-` rows ever.** This is
    **by design, not a write failure** — `schwab.py:154`: *"OCO child legs are created BY THE BROKER,
    so they never appear in `broker_orders`."* ⇒ "Is a sell working?" **cannot** be answered from our
    books; it must be asked of the broker. Related: item 7 (IRE).
  - **Order ADOPTION does not exist.** The only creator of a `broker_orders` row is
    `oms/store.py:367 get_or_create_order`, which **requires** `intent: TradeIntent`. The one orphan
    routine, `_terminalize_orphaned_active_intents`, does the opposite — it gives up on our intents.
    Adopting would need a synthesized intent per leg. **Attribution gap, not an exit gap:** 471/471
    `-ocoexit-` legs resolved at the broker unaided. [[project_mai_tai_per_lot_attribution_gap]]
  ✅ **Shipped and unrelated to this ruling:** `#885`'s `_V2_EXIT_MAX_REJECTS_PER_EPISODE = 20`
  absolute ceiling (`service.py:433`), merged and deployed 2026-09-03. It bounds the storm; it does
  not address the cause. **Stays.**

- **DUP2 — `fanout_slot` CLASSIFICATION MISMATCH: a Schwab `reclaim` is fanned out into the Webull
  `resting` slot** *(owner: codex-2; **CONFIRMED BREACH, mechanism now IDENTIFIED**; operator ruled
  2026-09-03 that two Webull reclaim legs in one cross is NOT INTENDED — the intended pair is one
  Schwab reclaim + one Webull reclaim)*.
  ⭐ **Mechanism, found by codex-2 in the durable fills and measured here.** `cw_entry_slot` (Schwab
  composition) and `fanout_slot` (Webull venue) are separate fields, and the venue flags
  `fanout_webull_resting_taken` / `fanout_webull_reclaim_taken` are **separate booleans**. When a
  `cw_entry_slot=reclaim` fill is stamped `fanout_slot=resting`, it consumes the **wrong** boolean,
  so a genuine reclaim leg still finds its own flag free and fires. Two Webull legs, one cross.
  **MEASURED — it is the majority case, not an edge. All three cells are bounded to the SAME
  window, `2026-08-31 → 2026-09-02`:**
  | `cw_entry_slot` | `fanout_slot` | n (08-31 → 09-02) |
  |---|---|---|
  | `first` | `resting` | 39 (correct mapping) |
  | `reclaim` | `reclaim` | 7 (correct) |
  | **`reclaim`** | **`resting`** | **11 — MISCLASSIFIED** |
  ⇒ **11 of 18 stamped reclaim fan-out legs (61%) are misclassified** in that window — current,
  and after every fix so far.
  ⛔ **An earlier draft printed `41` for the first cell and it was WRONG in two ways** (`codex-2`
  caught the window mix): it was an **unbounded** count, so it mixed windows — the reclaim cells
  ended 09-02 while it ran to 09-03 — **and it was a LIVE count taken mid-session**, so it was
  never a fixed number. Per completed session: 08-28 `1` · 08-31 `21` · 09-01 `9` · 09-02 `9`.
  09-03 is **excluded: the session was in progress at the reading and is not a bounded figure.**
  ⭐ **A census with no window is not a measurement** — and the first repair of this row proved the
  point by quoting a fresh unbounded running total, which `codex-2` also had to reject. Every
  number here now carries its window.
  ⛔ Also bounded because `cw_entry_slot` stamping only begins ~08-28: earlier fills are unstamped,
  so 11/18 is a **floor for this window, not a rate for all time** (STMP constraint).
  **The two confirmed breaches are only where a misclassified reclaim COLLIDED with a correct one
  in the same segment:** NCRA 08-31 `10:01:16` (reclaim/reclaim) + `11:24:33` (reclaim/**resting**),
  seg `1788181200000`; SSM 09-01 `14:58:19` (reclaim/**resting**) + `15:12:24` (reclaim/reclaim),
  seg `1788287460000`. The other 9 misclassifications did not collide — **so far**.
  ⚠ **Inverse harm to check, NOT asserted:** a misclassified reclaim consumes
  `fanout_webull_resting_taken`, so it could also **suppress a later legitimate resting fan-out
  leg** — a silently missing Webull mirror that would look like the leg simply not firing. Nobody
  has looked for that direction.
  ⛔ **Two earlier mechanisms are WITHDRAWN, both wrong:** mine ("the flag is per-slot") and the
  provenance-mismatch reading. The segment identity is **identical** in both pairs
  (`1788181200000`, `1788287460000`); the differing `fanout_slot_id`s are a **consequence** of the
  differing slot name feeding a deterministic derivation, not evidence of divergent segments.
  **Next action (codex-2):** find where a `cw_entry_slot=reclaim` entry is stamped
  `fanout_slot=resting`, and fix the classification. ⛔ Do **not** add a guard at the consumed
  flag — the flag is correct and segment-scoped; it is being fed the wrong slot name.
  Denominator: stamped `live:orb` reclaim fan-out legs, 08-31 → 09-02 — 18.
  Falsifier: a `cw_entry_slot=reclaim` fill stamped `fanout_slot=resting` after the fix.
- **DUP3 — 12 exit-side duplicate legs, design-or-defect, UNPROVEN** *(owner: codex-2; sits with
  DUP2)*. Not yet assessed. **Next action:** classify each of the 12 as intended fan-out behaviour
  or duplicate exit, then state which. Denominator: exit legs per closed position.
  Falsifier: two exit legs against one lot with no fan-out design that calls for it.

- **RET1 — `market_capture_quotes` prunes at 14 CALENDAR days** *(owner: **operator** — the
  retention decision is theirs; measured 2026-09-03)*. `prune-capture.service` runs
  `prune_market_ticks.py --keep-days 14`; the predicate is
  **`received_at < now() - interval '14 days'`** and the timer is `OnCalendar=*-*-* 09:30 UTC`,
  `Persistent=true`. ⛔ **It prunes by CALENDAR TIME, not by trading session** — an earlier draft of
  this row said "14 sessions", which is wrong and made its falsifier invalid.
  ⭐ **Verified by watching the window MOVE, not by reading the config:** earliest quote day was
  **2026-08-19** on 09-02 and **2026-08-20** on 09-03 — 08-19 pruned overnight.
  1. **It answers the unexplained 08-19 NBBO boundary.** Not a configuration decision, nothing to
     look for: **attrition**. A 14-day window measured from the day of that reading lands there.
  2. ⛔ **The 82-entry study window — also the RST1 hand-classified population — is expiring.**
     Because the 09:30 UTC cutoff (05:30 ET) precedes the 07:00–16:00 ET study window, a session's
     data survives the run on `S+14` and is deleted by the run on **`S+15`**:
     `08-24` deleted 09-08 · `08-25` 09-09 · `08-26` 09-10 · `08-27` 09-11 · `08-28` 09-12 ·
     `08-31` 09-15 · `09-01` 09-16. **Last day each session is fully present is `S+14`**, so the
     safe instruction is *run the control by `S+14`* — for D20's 08-31 tape that is **2026-09-14**.
  3. ⭐⭐ **STANDING CONSTRAINT: a control that depends on quote evidence must be RUN WITHIN 14
     CALENDAR DAYS of the event, or it is dead on arrival.** Put the run-by date on the row when
     the control is specced, the way D20 carries 2026-09-14.
  **Next action (operator, NOT today):** decide whether retention should be extended. Evidence for:
  D20, RST1 and the 82-entry study all hit this wall inside one week.
  Falsifier: a row with `received_at` older than 14 calendar days still present after a completed
  prune run.
- **FLR — §3 floor formula** *(owner: codex-2; ⛔ **NOT BUILT — corrected 2026-09-03**)*.
  ⛔ **An earlier draft of this row certified §3 as "HOLDS structurally" from the wrong mechanism,
  and that claim is withdrawn.** The marker it cited, `[OMS-V2-CW-FLOOR-ARMED]`, entered in
  `37fae0e` (*1-bar reclaim gap + floor-at-+2% exit*) and belongs to the **older CW floor path** —
  it predates the docs-only #866 (`1f5da81`) entirely. Stale quotes being rejected before that
  older floor logic says nothing about §3's order-staleness-vs-gap behaviour.
  **§3's own design marker `[OMS-P0A-REPRICE-BELOW-FLOOR]` has NO implementation in `src/` or
  `tests/`.** ⇒ Status is **NOT BUILT**, therefore necessarily UNEXERCISED — not "holds".
  **Next action:** build §3, or cite an implemented §3 call path. Until one exists there is nothing
  to grade. Falsifier: an implemented `[OMS-P0A-REPRICE-BELOW-FLOOR]` call path in `src/`.
- **DB2 — the Webull fill erases its own claim** *(owner: codex-2; **UNEXERCISED**, measured
  2026-09-03)*. Merged #858 (`46a0c87`). BLOCK cost marker is
  **`[V2-FANOUT-RECLAIM-BLOCKED-BY-FILLED-CLAIM]`** — `n=3` on 09-02 against **12 filled Webull
  fan-out legs**; 09-03 is `0/0` = UNEXERCISED. The 09-01 `0/16` predates the deployed v2 process
  and is **not gradable**.
  ⛔ **Falsifier corrected — the earlier wording had the polarity reversed and would have failed
  the correct behaviour on every future filled leg.** #858 treats a FILLED Webull claim as live
  venue evidence and **vetoes** the release (`[V2-FANOUT-CLAIM-ZERO-HOLD-VETOED]`,
  `released=0 held=1`); **erasure is the defect** — it is what happened to all 29 filled legs on
  08-31. Falsifier: **a filled leg whose claim is ERASED**, the veto failing.
- **STMP — first-slot fills stamp no arm id** *(owner: claude-1; **RULED 2026-09-03 — history
  UNRECOVERABLE, count from 09-02 forward, DO NOT BACKFILL**)*.
  "PASS on 3 of 15" is not a pass: **185 of 248 resting fills (74.6%) carry `cw_arm_bar_ts='0'`**
  and cannot be assigned to a segment. Of those 185, **177 carry no `fanout_slot` either** — the
  plain resting path; the other 8 are `fanout_slot=resting` yet unstamped. **2026-09-02 is the
  first session at 100% coverage.**
  ⭐⭐ **STANDING CONSTRAINT wherever SEGX or the 82-entry population is cited:** any pre-09-02
  segment-level figure is quoted as a **FLOOR with its date range attached**, never as a rate —
  *"≥1 in the 61 groupable segments, 2026-08-11 → 09-02"*, never a percentage. Rates begin 09-02.
  Falsifier: a post-09-02 first-slot fill with `cw_arm_bar_ts='0'`.
- **D20 — a filled fan-out leg does not consume its slot** *(owner: claude-1; **GRADED 2026-09-03 →
  COULD_NOT_TELL**, by the acceptance doc's own standard)*.
  **Live evidence (09-02), per SLOT — the unit of the question:** 27
  `[V2-FANOUT-MIRROR-LIVE-CROSS]` observations resolve to **10 distinct crossed slots**; counting
  observations would have inflated the denominator 2.7×. **9 filled, all 9 `webull_slot_consumed=1`;**
  the 10th (NCPL) crossed without filling and correctly consumed nothing. Falsifier hunt across
  **all 58** filled fan-out outcomes: `consumed=0` occurs **zero** times. The 4 without the field
  are explained, not excluded — one predates the field and was never crossed, three are
  `applied=0 … stale evidence cannot mutate a new segment`, deliberate refusals for retired segments.
  ⛔ **Not a PASS, and the doc says so.** `d20-fanout-duplicate-acceptance-grading.md` requires
  reproducing the **five** 2026-08-31 crossing attempts: *"Until that event exists and reproduces
  the five 2026-08-31 controls, D20 is COULD_NOT_TELL."* The marker fired **09-02 only, zero on
  08-31**, and the derivation returned **16 edges against 5**. A failing control **voids** the
  probe; it does not make it negative.
  ⭐ **Pending or dead? Pending — by REPLAY, not by waiting.** The 08-31 conditions have not
  recurred and there is no reason to expect them to, but the control needs no live repeat: the tape
  exists and the marker can be run against it. **A work item, not an event watch.**
  ⛔ **Run-by 2026-09-14** (RET1): after that the 08-31 tape is gone and the control becomes
  genuinely unreproducible — at which point D20 closes as permanently-ungradeable, because a
  control that can never be met is dead, not pending.
  **Next action:** resolve 16-vs-5 against the 08-31 tape before 09-14. The doc names the likely
  trap — a mirror-fills denominator yields 18, a one-minute-bar denominator 19 — so a **wrong unit**
  is the likeliest cause of 16.

- **RT1 — strategy -> OMS -> position roundtrip has no active integration test** *(owner:
  codex-2; boarded, not chased)*. The only cross-service test was introduced 2026-03-29, first
  stopped reaching OMS at `934d5f42` (2026-04-22) when seeded bars became closed history and one
  following tick could only start, not complete, the next bar, then was marked non-strict `xfail`
  at `3b90c089` (2026-06-17). Active tests separately cover entry -> intent, intent -> OMS/database
  positions, and OMS order event -> strategy position, but no test joins all three seams. **Next
  action:** replace the xfail with one faithful completed-bar roundtrip that must emit an intent,
  persist the OMS fill/positions, and update strategy state from the returned order event.
  Denominator: 1 active cross-service roundtrip; current result 0/1. Falsifier: the replacement
  passes after either the strategy-to-OMS or OMS-to-strategy handoff is disconnected.
- **PAPER1 — successor exit question above +5%** *(owner: codex-2; blocked on exercised paper
  harness evidence)*. Once the v1 paper harness has run forward, measure whether a position that
  reaches +5% should be sold or released as a runner. This is explicitly outside v1: do not change
  its locked first-trigger rule, add trailing behavior, or derive a choice from the historical
  backtest. Denominator and falsifier must be stated from forward harness evidence before design.
- **Q21 — EH downside protection** *(owner: codex-2, build APPROVED by operator 09-01; design =
  `webull-premarket-protection-decision.md` Parts 1/2/4 exactly as written 08-18)*. RTH-gate the
  pre-market attach · ONE counted `[WEBULL-PREMARKET-UNPROTECTED]` line per fill · OMS-restart
  fence. Log/fence-only, cannot oversell. Denominator: pre-market `live:orb` fills per session.
  Falsifier: a session with pre-market fills where the counted line ≠ fill count.

  ⛔ **CORRECTION 2026-09-03 — "ORB disabled since 07-31" is NOT the blocker.** `live:orb` still
  receives v2's **Webull fan-out leg**, and v2's entry window opens **07:00 ET** — pre-market.
  Measured: pre-market `live:orb` BUY fills on **12 of the last 15 sessions** (3 on 09-01).
  What is true is narrower: **since #869 deployed (09-01 14:37 ET) there have been ZERO pre-market
  `live:orb` fills**, so Parts 1/2 are **UNEXERCISED against a denominator of 0 — valid, not a
  finding** — and `[WEBULL-PREMARKET-UNPROTECTED]` reading 0 is correct. Accumulating watch.
  Part 4 was exercised by the deploy's own restart fence.
  Status, one label each — **updated 2026-09-03; `APPROVED-BUILD` was stale, the parts are built
  and deployed**:
  · **Parts 1/2 = DEPLOYED (#869, 09-01 14:37 ET), UNEXERCISED** — denominator 0 since deploy, so
    the zero is correct and there is nothing to investigate. Accumulating watch.
  · **Part 4 = DEPLOYED and EXERCISED** — the deploy's own restart fence ran it.
  · **§3 = NOT BUILT.** Design OPERATOR-CONFIRMED 09-01 and buildable (floor =
    max(entry×(1−hard_stop_pct), #853 ratcheted floor); one-shot; below-floor pages), but its
    marker `[OMS-P0A-REPRICE-BELOW-FLOOR]` has **no implementation in `src/` or `tests/`** — see
    the **FLR** row, which holds the evidence and the next action.
    ⛔ "STALENESS protection, not GAP protection" is a statement of the **design intent**, not a
    verified property: nothing is built to verify it against. No gain below the floor; the exposure
    there only becomes visible.
  Kin: item 11 above.
- **C42 — post-04:00 joiners arm on stale anchors** *(owner: codex-2; replaces C28+C41, one
  question asked twice)*. 09-01: 4 arms on 08-31 anchors (GYGY 04:06 · WETO 04:25 · SSM 04:35 ·
  FLYE 05:49 ET), unrolled AND uncapped — the 04:00 roll ran at `watchlist=0`, and the seed-cap is
  blind to post-boot joiners (per-symbol watch_start = boot for boot-present symbols). Spec: apply
  roll/seed-cap logic at watchlist-join time. Denominator: post-04:00 joiners per session with
  pre-04:00 anchors (09-01: 4). Falsifier: a joiner arming on a stale anchor with no cap/roll line.
  ⭐ **MEASURED 2026-09-03 — this SEPARATES C42 from C28 rather than contradicting it.** All
  `[V2-CW-SEED-CAP]` lines are **late joiners** (`watch_start > boot`); **zero** are boot-present
  (`watch_start == boot`). Of C42's own four 09-01 joiners, **three received no seed-cap line at
  all** (GYGY 0 · SSM 0 · FLYE 0; WETO 1). ⇒ The cap protects the late-joiner population and is
  **blind to the boot-present population** — exactly as this row claims. C28 is closed for the
  population the cap reaches; C42 stays open for the one it does not.
- **S7 — 'first'-slot fills stamp `cw_arm_bar_ts=0`** *(owner: codex-2; found in the S5 re-grade)*.
  Reclaim stamps the segment id; first does not — per-segment 'first'-slot composition grading is
  COULD_NOT_TELL **by construction**. Spec: stamp the resting/'first' path like reclaim.
  Denominator: 'first'-slot BUY fills per session. Falsifier: a stamped-era 'first' fill with
  `cw_arm_bar_ts=0`.
- **S8 — SSM/WETO/GYGY stale-anchor harm-linkage** *(owner: claude-1, reading)*. The C42 arms'
  disarm-vs-fill sequencing is untraced for three of four symbols (FLYE traced: its fills came from
  a fresh 09:31 ET arm). Answer: did any 09-01 fill trace to a stale-anchor segment?
- **Q4 — a guard structurally unable to fire, counted as coverage** *(owner: BLOCKED — definition
  missing; operator flagged it as still-real)*. The two candidate guards checked 09-01 came back
  healthy (liquidity floor called at 5 live sites; replace-link written at `oms/service.py:9308`) —
  Q4 is a different guard. ⛔ Cannot be specced until the row text is restated.

### Retired 2026-09-01 (operator ruling)

**Q27 · Q22 · Q19 · Q15 · B9 · Q8 · M9 · T35 · D17 · B21 · B22** — closed WITHOUT restatement:
no recorded definition anywhere, subjects superseded, and carrying them cost more than they were
worth. ⭐ The standard that closes them is the standard that would reopen them: **if any is real
it resurfaces with evidence** — and then it enters this board as a new row with an owner and a
next action, like any finding.

### Closed in batch 2 (evidence in [`handoff-log.md`](handoff-log.md), 2026-09-01 entry)

> ⛔ **REOPENED 2026-09-03 — `D20` and `DB2` below are SUPERSEDED by their rows above.** Batch 2
> closed them on a *delivered artifact* (a regrade, a merged fix); the 09-03 triage graded them on
> *evidence* and neither reached a result — D20 `COULD_NOT_TELL`, DB2 `UNEXERCISED`. A permanent ID
> cannot be both, so **the open row is the live status** and these entries are historical record.
> ⭐ The rule this exposes: **delivering the instrument is not the same as getting a reading.**

Q16 · S6 · N3 · T22 · S5 · D23(read clean ×2 sessions) · D21 · D20(regrade delivered) · W2B ·
DB2 · DB3 · G01 · C28/C41(superseded by C42).

## ⚠️ Watch items live in [`session-handoff.md`](session-handoff.md), not here
Verification is a *state* ("is this behaving?"), not a *task* ("do this"). Keeping them here is what
made an open-items file that could never reach zero.

## 12. ⛔⭐⭐ `virtual_positions` reads ZERO for a position we HOLD — and five consumers believe it
> ✅ **THE RECONCILER CONSUMER IS FIXED + DEPLOYED 2026-08-14 (#704, HEAD `69d4b5a`)** — it now reads
> `max(virtual, oms_managed_positions)`, so a false zero no longer manufactures a CRITICAL page.
> ⛔ **UNEXERCISED**: the account was flat from the deploy, so no finding has run on the new code.
> ⛔ **THE ROOT CAUSE IS UNTOUCHED.** `[VIRTUAL-CLEAR]` still zeroes a live row ~0.7s after the fill,
> inside the broker settle window (`shape=FLAT_INFERRED (n=0)`), and never restores when the broker
> becomes visible ~15s later. **The other four consumers still believe it.**

**Filed as a defect, not a preference.** The predicate `virtual_quantity != 0` was recommended and
adopted in **Ship 2 (`unowned_position_cron.sh`)** and **read C (`readc_capture.py`)** on 08-07.
It is wrong in both. ⛔ It is not a naming collision — `virtual_positions` IS the holdings ledger,
written by the OMS fill path (`apply_fill_to_positions`). It is simply **empty when it should not be**.

### THE INSTANCE — DSY, `live:orb`, still held at 16:25 ET 08-07
| source | DSY |
|---|---|
| `account_positions` (broker snapshot) | **1** |
| `oms_managed_positions` | **1 open** |
| **`virtual_positions`** | **0** ⛔ |

Two of three agree it is held and ours. ⭐ **The control group is what makes this conclusive** — DSY
is the *only* open position on the fleet, so it is the only row where zero can be judged wrong at all.

⛔ **A same-day census of 18 buy fills read as "18/18 confirm the bug" and confirmed NOTHING** — 17 of
them had already exited, where `quantity = 0` is CORRECT. The population was the artefact.
[[feedback_aggregation_masked_the_event]]

### ⛔ Proven by elimination, because the fingerprint does NOT discriminate
`_apply_position_fill`'s sell branch sets `quantity/average_price/opened_at` to `0/0/NULL` when a
position closes — **byte-identical to what `clear_virtual_positions_without_account_backing` writes.**
A zeroed row cannot be attributed to a cause by inspection.

⇒ **DSY is decisive anyway:** the broker holds 1, so no sell sequence can have run to completion
(buy 2 / sell 1 leaves 1, not 0; selling 2 leaves the broker flat). The sell path is *excluded*.
Exactly two causes remain, **both defects, and the choice between them does not change the fix**:
1. the buy fill never reached `apply_fill_to_positions`; or
2. `clear_virtual_positions_without_account_backing` erased it.

**(2) is the leading hypothesis [inferred, NOT pinned]:** DSY's virtual row was last written
**15:59:47, 1.1 s after the 15:59:46 fill**. The clear runs every `sync_broker_state`, keys off a
broker position snapshot fetched in an earlier phase, is **one-way** (nothing re-derives virtual from
account backing — grep: three `quantity = Decimal("0")`, zero repairs), and **its return count is
discarded, so it has no log line.** A ~1 s broker position-report lag is sufficient. Same family as
ERNA/#464, where a fill-settlement grace was added to the *flat-detection* path and **never to this one**.

### ⛔⭐ BLAST RADIUS — and ⛔ A BARE `WHERE` CLAUSE LIES: verify each SITE
**I first read five `WHERE quantity > 0` clauses and called all five blind. THREE WERE WRONG.** The
surrounding join decides, not the filter. Corrected, each one re-read:

| site | blind? |
|---|---|
| `schwab_1m_v2_bot.py:1048` `_held_symbols` | **YES** — `held` counts virtual ONLY; `union` adds only *in-flight* intents, so a filled-and-forgotten position is invisible. Scoped to `live:schwab_1m_v2` |
| `control_plane.py:9423` bot cards | **YES** — account rows are pre-filtered to symbols already in runtime **or** virtual, so a false zero hides the row entirely |
| `strategy_engine_app.py:9812` | under-counts open positions |
| `reconciliation/service.py:183` | **NO** — `keys = sorted(set(aggregates) | set(account_positions))`, a UNION. It fires |
| `deploy_preflight.py:97` | **NO** — that line is dead, but a separate `open_account_positions` gate still catches a held position |

### ⛔⭐⭐ THE RECONCILER IS NOT SILENT — IT IS DROWNING, AND IT CONFLATES TWO POPULATIONS
25,871 runs in 9 days (~1 per 30 s); **~2,300–3,300 `position_quantity_mismatch` CRITICAL per day**,
every one the shape `account > 0, virtual = 0`. ⭐ **Quantity splits them almost perfectly:**

| population | rows today | what it is |
|---|---|---|
| **CYN 5000 · DOCS 1000/300/500 · WWR 10000 · CLRO 1000** on `live:schwab_1m_v2` | round lots, the bulk of the volume (CYN alone ×2,488) | **the operator's manual holdings** — open item 8's population |
| **DSY 1 · MB 1 · NAMI 1 · HUIZ 1** on `live:orb` | qty 1 (DSY ×190) | **the fan-out leg = OURS.** TRUE POSITIVES of this defect |

⇒ ⛔ **Item 8 ("severity is INVERTED — an UNOWNED position pages CRITICAL") is only HALF TRUE.**
It generalised from the loud population. **Downgrading that severity without a discriminator would
suppress the real ones.** ⇒ The fix to 8 is a **discriminator (ownership via
`oms_managed_positions`), NOT a severity change** — and **item 12 settles first.**

⚠️ `FindingSpec` computes a `fingerprint`, but **`reconciliation_findings` has no fingerprint
column** — the dedup key is discarded at persistence, so every run re-emits the same finding. That
volume is why a genuine alarm reads as noise. [[project_mai_tai_reconciler_detects_nobody_listens]]
[[feedback_authoritative_for_a_is_not_for_b]] · [[feedback_aggregation_masked_the_event]]

### NEXT — in order
1. **Switch Ship 2 + read C to `oms_managed_positions`** (authorised: the "establish first" condition
   resolved — it is the same concept, broken, so nothing depends on the old meaning).
2. **Pin cause (1) vs (2)** — the clear discards its count; **log it before anything else**, then a
   held position across one sync settles it. ⛔ Do not fix blind.
3. **Then** re-open item 8.

---

## 13. ⛔⭐ THE WEBULL LEG BUYS UNDER A LOOSER PRICE CAP THAN THE STRATEGY ASKED FOR
*(opened 2026-08-14 from the CGTL −5.18% trade; operator deferred the fix, wants more examples first)*

### The mechanism — verified, not inferred
On an EH resting flip v2 emits **two** intents: Schwab (real qty) + Webull fan-out (qty 1).

| leg | `eh_resting` | `resting_band_pct` | path | cap |
|---|---|---|---|---|
| Schwab | `true` | `0.5` | `_apply_v2_eh_resting_entry` → `_band_capped_marketable_limit` | level × **1.005** |
| Webull fan-out | **ABSENT** | **ABSENT** | gate fails → generic `_apply_v2_eh_entry` | signal × **1.010** (`oms_v2_eh_entry_max_cross_pct`) |

**CGTL 2026-08-14 08:30:12** — ask 5.4800 against level 5.4338 (**+0.85%**):
`[OMS-ABANDON-INTENT] code=ASK_PAST_BAND ... cap 5.4610` (Schwab, declined) and
`[OMS-V2-EH-ENTRY] ... cap=5.4881 limit=5.48` (Webull, **filled**), 98 ms apart.

⛔ **The strategy's own reason string says `ATR Flip fan-out webull (eh_resting)` while the metadata
key is ABSENT.** The human-readable label and the machine-readable field disagree, and only the
field is read. Grep the label, believe the field.

**Denominator (21 days):** **39** Schwab legs tagged `eh_resting=true`; **0** Webull legs tagged.
**≥25** divergences where Schwab rejected and Webull filled.

### ⛔⛔ TWO DIFFERENT POPULATIONS — do not collapse them (this is where I got it wrong first)
**(a) SCHWAB GENUINELY REFUSES MOST OF THESE NAMES.** The operator said so from TOS experience and
the data agrees. Verbatim, from `broker_order_events` (durable — no log rotation):

```
Opening transactions for this security must be placed with a broker. Contact us    x12 these names / x48 in 21d
Your order is not eligible for electronic entry. Please call a Schwab rep...        x8  these names / x16 in 21d
```

**Fills settle it — 10 of 12 have NEVER filled on Schwab, while filling 2-24x on Webull:**

| sym | schwab orders | **schwab fills** | webull fills |     | sym | schwab orders | **schwab fills** | webull fills |
|---|---|---|---|---|---|---|---|---|
| BAOS | 1 | **0** | 10 | | STKH | 3 | **0** | 24 |
| CGTL | 0 | **0** | 2 |  | WXM | 1 | **0** | 20 |
| HUDI | 1 | **0** | 6 |  | WYHG | 3 | **0** | 12 |
| JWEL | 1 | **0** | 24 | | XHG | 1 | **0** | 10 |
| PLAG | 8 | **0** | 10 | | BOXL | 48 | 13 | 10 |
| WETO | 1 | **0** | 2 |  | OFAL | 102 | 10 | 8 |

⇒ For those ten, **the Webull leg is the ONLY leg**, and the fan-out is doing exactly its job.
Operator, 08-14: *"webull let us trade which is not a issue... the issue is price cap."* Correct.

**(b) AND SEPARATELY, OUR OWN BAND ABANDON.** CGTL 08-14 never reached Schwab at all — **0 Schwab
orders ever** — because `ASK_PAST_BAND` abandoned it client-side first. So for (b) we never learn
what Schwab would have said.

⛔⛔ **A CORRECTION I OWE THIS FILE.** I first wrote *"the rejects are OURS, not Schwab's"* on the
strength of an OMS-log grep for `not available|permitted|eligible|foreign|ADR|restrict`. Schwab's
actual wording contains **none of those words**, and the OMS log retains only ~6 days while these
rejects span 21. **Wrong pattern against the wrong source, stated as a finding.** The durable answer
was in `broker_order_events` the whole time.
⇒ **Search the SOURCE THAT DOES NOT ROTATE, and match the string the vendor actually emits — never a
paraphrase of it.** [[feedback_an_absence_is_evidence_only_against_a_known_denominator]] ·
[[feedback_a_wrong_reason_is_worse_than_a_missing_one]] ·
[[project_mai_tai_broker_order_events_conflates_client_aborts]]

⭐ This also re-frames **item 3** (~3 Schwab security-rejects/day, nothing evicts): those 48 rejects
in 21 days are the same population, and for the fan-out names the Webull leg is silently covering
for them. Item 3's "≈20 lost entries a week" is **not** lost on Webull — it is only lost on Schwab.

### ⛔ AND THE CAP IS NOT WHAT WE PAY — the headline correction
Webull fill vs the flip level, every divergence with a recorded level:

| WETO +0.29% · CGTL **−0.44%** · BAOS +0.69% · OFAL +0.03% · BOXL +0.32% · BAOS +0.61% · WXM +0.07% · HUDI +0.64% |
|---|

**Range −0.44% … +0.69%, median ≈ +0.32%.** The loose cap *permits* 1.0% and **nothing lands near
it.** CGTL filled **0.44% BELOW its own level** and still lost 5.18%.
⇒ **The entry PRICE is not the leak. The entry TIMING is.**
[[project_mai_tai_selection_spent_move]] · [[project_mai_tai_v2_stop_slippage_rootcause]]

### The money — per-trade %, median first (n=18 paired)
**Median +1.12%** · mean −1.04% · **10W / 8L**. Losses cluster at **−5%** (the stop); wins cap at
**+2…+3.5%** (the target) — the known "+2% caps winners" shape.
**Drop-one by name: WXM (4 trades) alone is −14.35pp of the −18.65pp total**; without WXM the mean is
**−0.31%**.
⇒ **Count says systematic, money says close to a wash** — the vol-floor shape again.
[[project_mai_tai_vol_floor_flap_measured]] · [[feedback_percentages_not_dollars]]

### The fix — scoped, NOT built (operator deferred 2026-08-14: *"no need to fix it now"*)
Propagate `eh_resting` + `resting_band_pct` onto the fan-out intent so the Webull leg prices under
**the strategy's own 0.5% band** instead of the OMS default 1.0%. A metadata change at the emit site,
not new logic.

⛔ **Frame it correctly.** This is NOT "make the two legs agree" — for the ten Schwab-refused names
there is no Schwab leg to agree with, and there never will be. It is: **the only leg we get should
still obey the band the strategy decided.** Today it obeys a looser default that nothing chose.

⛔ **Control it before believing it:** re-price the ≥25 historical fills under 0.5% and count how many
would have been declined, and assert the fan-out logs the band it was given. A fix that leaves the
Webull cap at 1.0% has changed nothing.
⚠️ **And weigh it honestly:** measured fills already land at median +0.32% vs level, well inside
0.5%, so the tighter cap would rarely bind. **Expected benefit is small; the reason to do it is that
the number in the log should be the number the strategy chose.** [[feedback_assess_both_brokers]]

⛔ **This REPLACED old item 5** ("a Schwab rejection vetoes the Webull leg too, so the name trades on
NEITHER broker"). That was falsified on 2026-08-14 — ≥25 cases show the legs proceeding
independently — and the item was DELETED rather than left standing as a wrong belief. **Numbering
below 5 is deliberately not reused; other items cite each other by number.**

---

## 14. ⛔⭐ THE FLOAT CEILING EXCLUDES EVERY LARGE-FLOAT MOVER — and nothing records the skip
*(operator 2026-08-14: "why we dont have CAPR stock in our scanner? this stock seems lot of good spikes")*

### The answer is one number
```
CAPR  shares_outstanding = 57,911,893      confirmed_max_float = 50,000,000     -> excluded, by ~16%
```
**Confirmed 0 times in 10 days. NOT blacklisted.** Its day: **+98.14%**, volume **17,099,239**,
rvol **4.4**, price $8.34. For contrast **AKAN**, confirmed all session: `shares_outstanding=540,841`
— ~100× smaller. The scanner is built for low-float squeezes and CAPR is not one.

The **alert** layer sees it perfectly — 69 lines on 08-14: SQUEEZE 5.7 / 5.8 / 7.2 / **10.0** /
**16.4** / 6.1 / 5.4 %, plus VOLUME_SPIKE **2.5×** and **5.6×**. It also appears in `five_pillars`,
which is a **display** surface — ⛔ only `confirmed` feeds a bot watchlist.

### ⛔⛔ (1) THE EXTREME-MOVER ESCAPE HATCH IS SUBORDINATE TO THE GATE IT SHOULD BYPASS
```python
if self._qualifies_path_c_extreme_mover(day_change_pct):
    if passed:            # <- _check_common_filters, INCLUDING float <= confirmed_max_float
        ... confirm ...
    logger.debug("[CONFIRMED] %s — PATH C rejected: %s", ticker, reason)
```
**All three confirm paths — A (news), B (two-squeeze), C (extreme mover) — sit behind the same
`_check_common_filters`.** Path C exists precisely for exceptional moves and **cannot fire on one**
when the float is large. A +98% day on 17M shares is still excluded on float alone.
⇒ Decide whether that is intended. If Path C is meant to be an override, it is currently not one.

### ⛔⛔ (2) THE SKIP IS UNRECORDED — "why isn't X in the scanner" is unanswerable
`_check_common_filters` returns `(False, reason)`; the caller logs it at **`logger.debug`**, which is
not enabled in production. **No artefact anywhere records that a symbol was considered and dropped,
or why.** Answering this question required reading source, not data.

⇒ **CHEAPEST FIRST STEP, independent of any threshold change: log the reject at INFO** — symbol, the
gate that bit, and the value vs the limit. It turns "why isn't X in the scanner" into a grep, and it
supplies **the denominator for every future selection study**: today we can see what passed and never
what was rejected, nor out of how many. Same shape as the 08-14 bar-watch incident.
[[feedback_an_absence_is_evidence_only_against_a_known_denominator]] ·
[[project_mai_tai_v2_snapshot_hardcoded_empty_fields]]

### ⚠️ THE THRESHOLD ITSELF IS A SELECTION QUESTION — **DISCUSS BEFORE BUILDING**
Raising `confirmed_max_float` changes **what the universe is**, and it collides with our own standing
finding that **we buy moves already SPENT** — +98% intraday is the textbook case.
⭐ The operator's counter-point is different and worth weighing separately: CAPR spiked **repeatedly**
through the day (7 squeeze alerts, 2 volume spikes), which is a **re-entry** pattern rather than one
exhausted move. Settle that before touching the number.
[[project_mai_tai_selection_spent_move]] · [[project_mai_tai_scanner_confirmed_capture]]

### Gate values, for reference (`strategy_core/config.py`)
`confirmed_min_volume=500_000` · `confirmed_max_float=50_000_000` · float-turnover tiers
**7%** (≤10M) / **10%** (≤30M) / **12%** (>30M) · `extreme_mover_min_day_change_pct` — ⚠️ appears as
both **50.0** and **30.0** in two config classes; confirm which one the live scanner reads.
CAPR passes volume (17.1M vs 500k) and would pass turnover (~30% vs 12%); **float is the only gate
that bites.**

---

## 15. 🔴🔴 HIGH — THE RELEASE → CLOSE → REPROTECT CHAIN LEAVES A REAL UNCOVERED WINDOW
*(2026-08-14. Supersedes my first draft of this item, which was WRONG — see the correction below.)*

### ⛔⛔ THE CORRECTION FIRST — the operator's broker screen beat our records
I reported *"#689 attach is 0-for-11, no Webull fill got a broker-side bracket all day."* **False.**
```
[V2-OCO-EMIT] real brackets today : 148      (13 SKIPPED, all "outside regular hours")
[WEBULL-PROTECT-ATTACHED]         :   0
```
The operator's Webull screen showed WETO holding **Target@8.17 / Stop@7.61**, matching
`[V2-OCO-EMIT] WETO entry=8.0100 -> OCO[target=8.1702 stop=7.6095]` **to the cent.**
⇒ **Brackets go on fine, 148 of them.** I read the absence of one marker as the absence of
protection, without checking the other path that provides it. The standing rule held: **the broker's
own screen is the primary source and it outranks our logs.**

### The real chain — STKH 15:02Z, verbatim
```
15:02:33 [OMS-EXIT-REPROTECT]   STKH — 3 refused closes after releasing the resting exit legs
15:02:33 [WEBULL-PROTECT-RETRY] attempt 1/3 refused: STOP_LOSS_PRICE_LT_MARKETPRICE
15:02:37 [WEBULL-PROTECT-FAILED] COULD NOT ATTACH after 3 attempts
```
1. entry fills, `[V2-OCO-EMIT]` puts a **real bracket** on ✅
2. the ladder wants out ⇒ **#691 CANCELS that bracket** (the release)
3. the close is **refused 3×**
4. **#692 tries to put the bracket back** ⇒ the re-attach **fails**
5. ⇒ **held, bracket gone, close not working. That is the uncovered window.**

⛔ **The `[WEBULL-PROTECT-*]` markers are the RE-PROTECT path, not a bare-fill rescue.** All 9
FAILED today came from step 4. Any reading of them as "naked entries" is wrong.

### ⛔⛔ AND §6 OF THE VALIDATOR PRINTS A FALSE CLEAN ON EXACTLY THIS
It reads `[OMS-EXIT-REPROTECT-FAILED]` (**=0**) and reports *"12 releases, 4 re-protected, none
failed"* — while the re-attach failed **9×** under `[WEBULL-PROTECT-FAILED]`. **Wrong marker ⇒ PASS
on a live failure.** §4 mis-attributes the same events as bare fills. **Both must read both markers.**

### ⛔⛔ WHY THE RE-ATTACH FAILS — **REWRITTEN 2026-08-17. The 08-14 answer below was wrong.**
The 08-14 entry said *"stale reference price"* and that became the accepted story. It is **half
right at best**, and three hypotheses have now been killed by evidence rather than argument.

**FACT 1 — it is not 10 failures, it is every attempt ever made.**
```
oms.log + all 6 rotated files (08-11 -> 08-17):
  [WEBULL-PROTECT-ATTACHED]   0
  [WEBULL-EXIT-PAIR-PLACED]   0     <- logged right after place_order returns
```
`place_order` has **never once returned successfully**. This is not a regression; the path has
never worked. ⇒ "#689 attach is 0-for-11" understates it — it is **0-for-ever**.

**FACT 2 — stale pricing is refuted for the bare-fill half.** Splitting the 08-14 refusals by
caller (they have opposite timing) is what the original entry never did:

| caller | fires | 08-14 | stale price? |
|---|---|---|---|
| #689 bare-fill | ~0.2s after the fill | 6 episodes, all refused | **NO — refuted** |
| #692 reprotect | 37s–10min after | 3 triggers, all refused | plausible, probably right |

CGTL's levels were **244 ms old** and exactly +2%/−5% of the fill when refused. Price cannot go
stale in 244 ms. ⇒ The stale-price story holds for **#692 only**; my blanket version was too broad.

**FACT 3 — the reject strings were read backwards.** The full text (never seen before, because the
log truncated at 200 chars — *"...should be lower than the cu"*):
```
OAUTH_OPENAPI_TRADE_STOP_LOSS_PRICE_LT_MARKETPRICE
  "The stop price of the stop-loss order should be lower than the current market price."
```
Our stop **was** lower. The error CODE names the **required relation, not the violation**, so it
reads as its own opposite. Only **1** refusal (CGTL 15:14, target 5.2173 vs *"should be higher than
5.23"*) was a genuine price violation.

**FACT 4 — the payload is NOT malformed (Probe X, 08-17, `preview_order`, nothing placed).**
```
CONTROL   Probe W shape A (LIMIT master + legs)      -> 200   instrument valid
UNDER TEST production _build_exit_only_pair_payload  -> 200   <-- while the account is FLAT
```
⇒ ⛔⭐⭐ **`preview_order` DOES NOT VALIDATE POSITION BACKING.** It passed while we hold nothing.
So **Probe W4's HTTP 200 only ever proved the shape PARSES, never that it PLACES** — the
"BROKER-PROVEN" claim in `webull.py::_is_exit_only_pair` and in
`tests/unit/test_webull_attach_protection.py` overstates what was shown. **Do not treat a preview
200 as evidence this path works.**

**ALSO KILLED:** the CORE-session / prior-close reference theory (`support_trading_session: "CORE"`
on pre-market orders). AKAN's stop 7.74 sat **below** its 08-13 close of 9.49 and was still refused.

### What survives — plausible, NOT proven
**Position backing at place time.** Attempts fired at 0s / 2s / 4s against a settle lag measured at
**12.7s** (CGTL 08-14: `FAILED` logged **8s BEFORE** `[SETTLE-LAG] VISIBLE after 12.7s`), and one
episode logged `NEVER VISIBLE after 300s`. A protective SELL for shares the broker cannot see is a
naked short to it.
⛔ **The counter-evidence is real:** in **4 of 6** bare-fill episodes attempts 2–3 fired *after*
`SETTLE-LAG: VISIBLE` and were still refused. `list_account_positions` visibility and order-side
available-to-sell are **different surfaces** and have not been shown to move together.
⇒ **If the widened horizon (#707) lands and refusals persist, the settle window is exonerated.**
⛔ **Concurrency:** STKH shows **two interleaved retry sequences on one fill**
(`1/3, 2/3, 1/3, 3/3, FAILED, 2/3, 3/3, FAILED`), so the 9 FAILED lines cover only ~5-6 distinct
positions. Any unprotected-count read off those lines is inflated.

### #691's own numbers (08-14, intraday 12:06 ET)
`reservation rejects 58 -> 24` · `-close- 5 filled / 24 rejected` · `12 releases` · `#692: 4 re-protects`
⇒ The release works. **The close still mostly fails, and the restore fails after it.** Underneath is
the retry bound that resets on any positively-HELD read — #691 was never going to fix that.

### ⛔ The largest reject class today is OURS, not Webull's
```
83x  RuntimeError('Webull combo MASTER must be LIMIT or MARKET (a buy-STOP ma...')   7 symbols
23x  NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K                     3 symbols
```
A Python exception stored with the same `Webull order rejected` prefix as a genuine refusal — the
`source`-column contamination again. ⚠️ **83 against exactly 83 mirrors placed is a suspicious 1:1;
count per `client_order_id` before calling it noise OR a real second attempt.**
[[project_mai_tai_broker_order_events_conflates_client_aborts]] · [[project_mai_tai_probe_w_webull_stoplimit_master]]

### ✅ SHIPPED 2026-08-17 — #706 + #707, both deployed (HEAD `634ff21`)
**#706** (three noise fixes, deployed 11:00 ET, 3s outage):
- **still-held guard** — bails ONLY on a positively-CONFIRMED flat; `FLAT_INFERRED`/`UNKNOWN`
  continue, because FLAT_INFERRED is the ordinary settle-window shape (CGTL read it for 12.7s).
- **placeability guard** — skips levels the broker would refuse. Biased towards SENDING: skips only
  when a level fails against EVERY proxy (bid/ask/last). **No quote ⇒ no opinion ⇒ send.**
- **coalescing** — one attach per position, killing the interleaved sequences.
- **`RoutingBrokerAdapter.fetch_quotes`** — ⛔ without it the placeability guard is a SILENT NO-OP:
  the OMS holds the ROUTER, `fetch_quotes` is Schwab-only, so every Webull lookup returned `{}` and
  the guard would read as present while never running (the `fetch_oco_exit_fill` trap). Quotes are
  symbol-level; verified 08-17 that Schwab quotes the Webull-only names (XHG 3.53/3.78).

**#707** (deployed 11:44 ET, 10s outage):
- **`[WEBULL-EXIT-PAIR-REFUSED]`** logs the exact payload sent (from the REAL builder, never a copy)
  plus the full exception and response body. ⭐ **This is the instrument that ends the guessing** —
  three hypotheses were argued and killed without it.
- reason cap **200 → 1000** (the truncation is what hid FACT 3 above).
- **retry horizon 3×2s (4.0s) → 5 attempts backing off 2/4/8/15 (~29s)**, capped. Attempt 1 stays
  immediate: settle is usually 0.3–0.7s.

### ⛔⛔ NONE OF THIS HAS PROTECTED ANYTHING — read before reporting on it
Both PRs are **UNEXERCISED**: the account was flat all session, so no Webull fill has reached the
attach path since deploy. And they are **noise + diagnosis fixes, not a cure** — the payload is
refused for a reason none of them addresses.
⇒ **A LOWER REFUSAL COUNT TOMORROW IS NOT A FIX.** The only real PASS is a
`[WEBULL-PROTECT-ATTACHED]`, which has **never once been observed.**

### Still not built
- The retry bound `_v2_exit_close_failures` still resets on every positively-HELD read (item below).
- Not releasing until the close is known placeable.
- Correcting the "BROKER-PROVEN" comments that rest on a preview (FACT 4).

---

## 16. ⛔ THE RECONCILER CANNOT SEE A SHORT POSITION AT ALL
*(found 2026-08-14 during the EOD deploy pre-flight)*

`_build_position_findings` selects `AccountPosition.quantity > 0`. **A negative quantity is excluded
from the comparison entirely**, so a short position at the broker is invisible to drift detection —
it can never produce a `position_quantity_mismatch`, in either direction.

**Live instance:** `live:schwab_1m_v2` held **XPON −1000 @ 4.4152 (mkt −$4,730)** across the 08-14
close. Confirmed **not ours** — zero XPON orders we ever placed on any account at any time, 0 fills,
0 intents, 0 managed rows, 0 `oms.log` mentions — so the OMS scoping invariant correctly never acted
on it. But the reconciler would have been equally silent had it been *ours and wrong*.

⇒ **The gap is not "we ignore the operator's shorts", it is "we cannot detect a short drift AT ALL."**
If v2 ever ends up short by defect — a double-sell, an exit that overshoots, a fan-out leg selling
stock the other leg no longer holds — **nothing in the reconciler would say so.**

⚠️ Fixing it is `quantity != 0` plus signed comparison, but **do not do it blind**: every downstream
severity and alert assumes non-negative quantities, and `abs(account_quantity - our_quantity)` on a
signed pair changes what "delta" means. Scope it with item 8 (severity inversion), which is now
workable because #704 supplied the ownership discriminator.
[[project_mai_tai_oms_scoping_invariant]] · [[feedback_the_brokers_book_is_shared]]

---

## 17. 🔴🔴 HIGH — ONE FAILED SCHWAB POSITIONS READ ERASES A HELD POSITION'S LEDGER ROW
*(2026-08-17. Proven harm, not a hypothesis. Root-cause candidate for item 12's false ZERO.)*

### The chain — every link code-confirmed
1. `broker_adapters/schwab.py::list_account_positions` **returns `[]` on ANY failure** — five
   `return []` paths including a bare `except RuntimeError`. ⛔ A bare except returning a value that
   means "nothing here" is the worst shape of this bug.
2. **Webull HAS a never-synthesize-flat guard** (cached snapshot, typed error on 429, *"never a
   synthesized flat"*). **Schwab has none.** That asymmetry is the actionable part.
3. `oms/service.py` does **not** gate the sync on an empty snapshot.
4. `store.sync_account_positions` zeroes **every symbol absent from the snapshot**.
5. `store.clear_virtual_positions_without_account_backing` then erases those virtual rows — and its
   own docstring says: *"Nothing re-derives `virtual_positions` from account backing … a row zeroed
   here stays zeroed even once the broker reports the position again."*

### ⭐⭐ THE NUMBER TO QUOTE IS THE CONVERSION, NOT THE TRIGGER RATE
```
324 Schwab positions-read failures in retained logs (08-11 -> 08-17)
109 OMS-owned position windows in the same period
  2 failures landed WHILE we held a position
  2 of 2  ->  ERASED, to the second
      08-12 19:34:18 failure -> 19:34:18.484 [VIRTUAL-CLEAR] live:schwab_1m_v2:CRWU=2
      08-14 19:31:48 failure -> 19:31:49.090 [VIRTUAL-CLEAR] live:schwab_1m_v2:VWAV=2
```
⛔ **2/324 invites someone to call this rare. The operative figure is 2 of 2 exposed holds erased,
and it scales with HOLD TIME, not with failure frequency.** Both were **isolated single failures**
(one each in a 10-minute window) ⇒ **one failed read is sufficient**; no burst, and no later good
sync repairs it. Only 2 of 324 coincided with a hold because the failures clustered while we
happened to be flat — that is luck, not a guard.

### Build THREE layers — they fail differently
1. **Port Webull's guard to the Schwab adapter** — cached snapshot, typed error, close all five
   `return []` paths.
2. **Gate the sync itself** — refuse to zero anything from an empty snapshot, ever. Independent of
   the adapter so an adapter regression cannot reopen the path.
3. **⭐ Make the erasure NOT one-way** — re-derive from broker backing once the broker reports the
   position again. L1+L2 stop new erasures; neither repairs an existing one, and it is the one-way
   property that turns a transient hiccup into permanent ledger corruption. Degrades gracefully when
   the first two are bypassed by something nobody thought of.

**Acceptance:** the mutant must prove the guard fires on a failed read, AND a separate test must
prove a failed read cannot reach `sync_account_positions` at all.
**Justification needs no further demonstration:** *an empty list from a failed call is not a flat
account.*

⛔ **ONE ROOT, TWO HARMS.** The same Schwab instability produced 7 entry aborts on 08-17 (timeouts,
`Unable to resolve host traderapi-accounts.schwab.com`, upstream resets — 5 of 7 today). A fix that
addresses only the ledger will look like it worked while entries keep dropping.
[[project_mai_tai_virtual_positions_false_zero]]

---

## 18. 🟡 ~35 OF 76 SCHWAB ENTRY REJECTS ARE OURS — the buy-stop is not above the ask
*(2026-08-17, from Schwab's OWN book: tag `TA_*`, BUY legs, `status=REJECTED`, 08-10 → 08-17.)*

| n | reason | symbols | whose |
|---|---|---|---|
| 29 (+6 in combined) | *"The stop price must be above the current ask for buy stop orders…"* | BOXL CRWU FGI GXAI RMCF SCKT XHLD | **OURS** |
| 24 | *"Opening transactions for this security must be placed with a broker"* | 21 | Schwab restriction |
| 16 | *"not eligible for electronic entry… call a representative"* | INHD PLAG | Schwab restriction |
| 1 | **"You do not have enough available cash/buying power"** | XHLD | account-level, different class |

⭐ **~41 of 76 are Schwab refusing the security outright** — this quantifies the standing "Schwab
genuinely refuses most of those names" finding. Not ours, not fixable our side.

### ⛔ TEST THE HYPOTHESIS BEFORE BUILDING — three outcomes, three different directions
v2 entries are buy-stops and Schwab requires the trigger above the ask **at arrival**. For each of
the ~35: our submitted stop, the quote at submit time, and the tape over the following minute.
- **price ran through our trigger before arrival** ⇒ latency/staleness in the entry path, and the
  loss concentrates in exactly the fastest moves we want. Real execution defect.
- **our trigger was computed below the ask when we computed it** ⇒ pricing-logic defect, and the
  **same family as the Webull `STOP_LOSS_PRICE_LT_MARKETPRICE` case** — check the shared cause.
- **neither** ⇒ something about how Schwab evaluates the trigger that we do not model. Say so
  rather than picking the closest story.

⛔⛔ **Do NOT widen the trigger to make the rejects stop.** That changes what we enter — strategy,
not execution.

⭐ **The two reject populations are ALREADY separable without item 9's `source` column:** present in
Schwab's book with `REJECTED` = broker refusal (76); absent with no `broker_order_id` = our own abort
(7, all transient infrastructure).

---

## 19. 🟡 THE BROKER-TRUTH GATE IS SHARED-BOOK SCOPED — the scoping invariant's one bypass
*(2026-08-17. LATENT — no v2 emitter reaches it today.)*

`oms/service.py:1145-1171` gates a SELL on `account_positions` — **the shared book** — then does a
live broker re-read. On any symbol the operator holds manually it **cannot distinguish our shares
from his**, so it passes on his inventory. It is the one mechanism by which the OMS scoping
invariant could be bypassed and we could sell into an operator position.

Latent because it is a *permissive* gate: it removes a rejection, it does not create a sell, and the
only consumer that could manufacture one (`strategy_engine_app`'s rehydrate) is inert for v2 —
v2 is an isolated service and is not in `state.bots` (`bot_count=1`, polygon_30s).

⛔ The `[VIRTUAL-CLEAR]` sweep has the **same** defect (`store.py`, keyed on account+symbol only), so
it is **not** the template for fixing this. Fix scoped to the OMS-owned set, same as item 20.
⭐ **The `tag` field is a second discriminator** in Schwab's book (`TA_krshk30gmailcom…` = ours,
`API_TOS:*` = the operator's) — but note the limit: **positions carry no tag, only orders.** So
OMS-owned quantity must be derived from our order history, not read off the position book. That is
the direction, not a drop-in.

---

## 20. 🟡 THE RESTART PRE-FLIGHT IS MIS-SCOPED, AND ITS SECOND CLAUSE IS THE USABLE ONE
*(2026-08-17.)*

`docs/live-market-restart-runbook.md` and the memory say **"account-flat"** against the **shared
book**. On an account where the operator holds anything manually (IVF 5000, XPON −1000) that blocks
**every deploy, forever**. It must test the **OMS-owned set** — managed rows + our non-terminal
orders — matching the scoping invariant that already governs what the OMS acts on.

⛔ **"Working orders = 0" is UNSATISFIABLE during RTH.** v2 rests entries near-continuously (336
orders against 82 arms on 08-12; median time-at-rest 61s), so as one cancels another replaces it.
Use the runbook's second clause — *"or know exactly which survive"* — which is what made today's
three deploys possible. **This is "never ask for a confirmation the system cannot produce", found in
the wild.**

## 21. 🟡 THE v2 STREAMER RECONNECT-LOOPS ALL WEEKEND WITH `symbols_desired=0`
**Volume that can bury a real signal.** `[V2-WS-DISCONNECT] failure #1: received 1000 (OK); then
sent 1000 (OK)` → `[V2-WS-LOGIN-OK]` repeats on a ~60 s cycle whenever the watchlist is empty.

⛔ **The split is by IDLE vs TRADING, not by date** — censused across every `schwab-1m-v2.log*`
(plain + gz) on 2026-08-23:

| ET day | disconnects | |
|---|---|---|
| Sat 08-16 | **1578** | idle |
| Sun 08-17 | **532** | idle |
| Mon 08-18 | 6 | trading |
| Tue 08-19 | 30 | trading |
| Wed 08-20 | 130 | trading |
| Fri 08-21 | 12 | trading |
| Sat 08-22 | **1142** | idle |
| Sun 08-23 (to ~09:45 ET) | **927** | idle |

⇒ **Every** `[V2-WS-LOGIN-OK]` on an idle day carries `symbols_desired=0`. We open a
subscription-less socket, Schwab closes it cleanly with **1000 (OK)** after ~60 s, we immediately
reopen it. Nothing is wrong at the venue — close code 1000 is the venue behaving correctly.

**Why it is on the board rather than ignored:** ~1000–1600 lines/idle day is the dominant
population in the file, and a census that greps this log without naming its population will read
it as noise or drown a real line in it. It is **not** deploy-caused — it predates both 08-23
deploys and is present in files going back to 08-16.

⛔ **The same census surfaced real transient failures that are NOT this loop** and must not be
folded into it: `userPreference HTTP 503 Unable to resolve host traderapi-user-preference.schwab.com`
×46, `HTTP 503 … host null` ×10, `HTTP 500` ×6, `HTTP 401 Client not authorized` ×1. Those are a
different question (Schwab-side DNS/auth), counted separately on purpose.

**Not yet investigated:** whether the reconnect is *supposed* to be suppressed at
`symbols_desired=0`, or whether the socket should simply be held open. Cheap to answer; nobody has.

---

## 22. 🟡 `eh_resting` NEVER TOUCHES THE SHARED FAN-OUT LATCH (§272) — REWRITTEN 2026-08-24 after cross-review

> ⛔⭐⭐ **THIS ENTRY WAS WRONG IN THREE PLACES AND IS REWRITTEN FROM `codex-2`'s CHALLENGE.**
> The original was authored by `claude-1` from code-reading alone. What survived, what did not,
> and *why the method failed*, are all recorded below — the method failure is the more useful half.

### ✅ WHAT SURVIVES

**The missing latch is real.** Of the four fan-out emit sites, three read AND write
`fanout_webull_claimed`; **`_eh_resting_cross_check` does neither.**

| site | `source=` | reads latch | writes latch |
|---|---|---|---|
| `update_position` | `rth_resting` | ✅ | ✅ |
| `_cw_v2_quote` | `reactive` | ✅ (#739) | ✅ |
| **`_eh_resting_cross_check`** | **`eh_resting`** | ⛔ **no** | ⛔ **no** |
| `_fanout_rth_resting_cross` | `rth_resting` | ✅ | ✅ |

**Signal 4 is structurally blind to it.** ⛔ **31 of 31** filled `eh_resting` Webull legs carry
`cw_arm_bar_ts = 0` (as of 2026-08-24), so signal 4 excludes them by definition: an
(`eh_resting` + `reactive`) pair inside one segment reads as **ONE leg, not a duplicate.**

### ⛔ WHAT WAS WRONG

**1. "Zero live evidence" was FALSE.** Across 9 retained EH crosses there are **3 log-derived
same-cross EH→reactive sequences**: JUNS 08-21, and PMI twice on 08-24. The original entry asserted
an absence without having looked anywhere it could be found.

**2. ⭐⭐ THE REACHABILITY TEST I SPECIFIED WAS BACKWARDS — and this is the important one.**
It said: *search inside `[V2-CW-ARM] → [V2-CW-DISARM]` windows.* **The EH cross normally fires
BEFORE its matching ARM**, so that search omits the EH event by construction. Reactive also runs in
extended hours, so **no 09:30 crossing is required** either. Run as specified, the test could only
ever return "not found" — and board 22 would have been closed as unreachable on a test that could
not detect the thing it was looking for. *A falsification test that can only fail in one direction
is not a test.*

**3. The population was the wrong definition.** Published as *22 symbol-days / 2 on 08-21*; that
counted `eh_resting` + **any other source**. The claim is about the `#739` pair, so it must be
`eh_resting` + **`reactive`**: **18 symbol-days since 08-01, 1 on 08-21 (JUNS)** — as of 2026-08-24.
Reconciled exactly: 08-21's SUGP is `eh_resting` + `rth_resting`, and `rth_resting` **does** claim
the latch, so it never belonged. ⛔ Quote the number WITH its as-of date; the original "22" did not
reproduce a day later because the population grows.

**4. The alternate guard is `resting_active`, not `resting_flip_ms`.** `resting_flip_ms` is a ~30s
settle / anti-burst guard. The real interlock is **`resting_active`, which refuses reactive while a
rest is live**; the seam opens only after fill/grace handling releases it.

### ⛔⭐⭐ AND THE OBVIOUS FIX IS A NO-OP — PROVEN BY MUTATION, NOT BY READING

Even if `eh_resting` DID claim the latch, the subsequent **BUY ARM reset clears it**:

```
after_eh        True
after_buy_arm   False
```

⇒ "make `eh_resting` claim the latch" would ship as a fix that fixes nothing. ⛔ And **no existing
test detects the latch mutation** — 73 targeted tests stayed green; a full run reached 2,307 passes
plus one unrelated scanner-history failure that passed individually on both control and mutant.
**The behaviour is unpinned in BOTH directions.**

### ⚠ JUNS IS A RECLAIM, NOT OVERLAPPING EXPOSURE

```
12:36:35Z  EH Webull fill
12:39:02Z  matching ARM (near-identical flip level)
12:46:37Z  first Webull position CLOSED
13:00:07Z  reactive Webull fill        ⇒ flat ~13m30s in between
```

The close path deliberately permits a later reclaim. Both legs carrying `cw_entry_n=1` proves an
**instrumentation** defect; it does **not** prove the second trade was behaviourally unintended.
**Harmful overlapping exposure remains UNPROVEN.**

### ⛔ WHAT IS GENUINELY `COULD_NOT_TELL`

- No recorded EH order was still working when reactive later fired: 31 filled immediately, and one
  stale intent was cancelled before broker placement.
- Venue-side Webull reconciliation is incomplete, so **the DB cannot prove an unrecorded working
  order never existed.**
- Retained logs begin **08-17** while the population starts **08-01** ⇒ log-derived segment
  matching cannot cover the whole window, and never will for the erased part.

### ⇒ DISPOSITION — do NOT close as unreachable, do NOT make the behaviour change

**Observability first:** assign a durable identity **before the ARM exists**, and record the Webull
outcome — *queued · dropped · submitted · filled · rejected · still working*. Only then can a
legitimate reclaim be told apart from overlapping duplicate exposure.
⛔ The behaviour change is the exact trade #739's author built and **discarded** — read that commit
before re-deriving it.

**Source:** `strategy_core/schwab_1m_v2.py` — reactive interlock ~L1766 · grace lifecycle ~L2202 ·
EH fan-out site ~L2413 · BUY ARM reset ~L1684.

---


## B32. ⛔⭐⭐ NO LOG MARKER MAY CONTAIN ANOTHER AS A SUBSTRING — make it a lint

**Third instance, which is where a habit becomes a tool.**

| # | collision | cost |
|---|---|---|
| 1 | the `order_created` / `refused_no_order_created` regex | a greedy match returned two metrics as one number for 4 h |
| 2 | guards matching their own log lines | a watch counted itself |
| 3 | `[V2-FANOUT-REACTIVE-LATCHED]` carrying `[V2-FANOUT-REACTIVE-SUPPRESSED]` in its text (§266) | `grep -c` of the suppression count would have returned it **inflated by exactly its own denominator** |

### ⛔⭐⭐ MEASURED BEFORE PROPOSING THE LINT — AND THE RULE AS FIRST STATED IS TOO STRONG

Scanned `src/` on 2026-08-24: **155 distinct bracket markers, 29 substring pairs, 14 of them
"bare-prefix families"** — `[V2-CW]` with `[V2-CW-ARM]`/`[V2-CW-DISARM]`/…, `[V2-FANOUT]` with its
five members, `[OMS-EXIT-REPROTECT]` with `-FAILED`/`-SKIPPED`, `[SCHWAB-TOKEN-REFRESHER]` with
three, and so on.

⇒ **A lint that simply bans "marker contains marker" fails 29 times on day one** and demands 29
renames across live log-consuming watches and cron scripts. The family-prefix convention is
deliberate and load-bearing; it is not the defect.

**The defect is on the CONSUMER side, and it is what all three instances actually share:**

> **Two markers counted as SEPARATE metrics must not be substrings of one another —
> equivalently, never count a bare-prefix marker without anchoring the token.**

`[V2-FANOUT]` sitting above `[V2-FANOUT-ON-FILL]` is harmless until somebody counts the bare
prefix. That is exactly what happened to `[V2-DB-SEED-GAP]` (three populations, opposite zero
polarity, summed by one `grep -c`) and to §266's LATCHED/SUPPRESSED pair.

**⇒ Lint the counts, not the emitters.** Rule: a count whose marker token is a bare prefix of
another marker must anchor on `]` (or name the longer sibling). **Current true violations: ONE.**

| file | line | count | why it is ambiguous |
|---|---|---|---|
| `ops/health/collect_deploy_evidence.sh` | 126 | `cnt 'OMS-V2-MIRROR.*fail'` | also matches `[OMS-V2-MIRROR-EH]` lines — two populations, one number. Low severity (both are "mirror failures") but it is the exact shape. |

⇒ **The tool is cheap: one fix, then a lint that holds at zero.** That is a far better trade than
29 renames, and it lands the rule where the harm is.

### ⭐⭐ AND THE DETECTOR REPRODUCED THE BUG IT WAS BUILT TO FIND
My first version flagged **3** consumers. **All three were false positives** — it matched a bare
prefix *inside a longer, correctly-specific sibling* (`V2-DB-SEED-GAP-CENSUS` counted as an
unanchored `V2-DB-SEED-GAP`; `SCHWAB-TOKEN-REFRESHER-DEGRADED-PERSISTENT` likewise), and one
"hit" was a **comment warning about this very trap**. The substring bug appeared *inside the tool
written to detect the substring bug*, on the first run.

⇒ Two requirements for whoever builds B32: **the token must be matched to its boundary (the next
character must not be `-`), and comment lines are not counts.** ⛔ And the lint needs its own
known-positive, or it will pass by finding nothing — the `OMS-V2-MIRROR` row above is that
fixture.

⭐ **NOTE WHAT CAUGHT INSTANCE 3: a BEHAVIOURAL test that read the emitted log.** A
source-inspection test asserts the marker is *written* and can never see that a sibling's *line*
contains it. The lint generalises that catch to every marker, including the ones no test drives.
