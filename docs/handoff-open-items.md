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

> ⛔⭐⭐ **DO NOT WORK THIS BEFORE ITEM 12.** The CRITICAL alarm has TWO populations: the
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

## ⚠️ Watch items live in [`session-handoff.md`](session-handoff.md), not here
Verification is a *state* ("is this behaving?"), not a *task* ("do this"). Keeping them here is what
made an open-items file that could never reach zero.

## 12. ⛔⭐⭐ `virtual_positions` reads ZERO for a position we HOLD — and five consumers believe it

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

### Why the re-attach fails — stale reference price
Attach fires **37s to ~10 min** after the entry, but the levels are computed off the **ENTRY** price.
By then price has traded through the stop, and Webull refuses exactly what it says:
```
22x STOP_LOSS_PRICE_LT_MARKETPRICE      stop no longer below market
 3x STOP_PROFIT_PRICE_GT_OPENPRICE      target no longer above open
 2x SYMBOL_CAN_NOT_SELL_SHORT           the position was ALREADY GONE — chasing nothing
```
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

### Direction (not built)
Re-price the re-attach off a **fresh quote at attach time**; **refuse to attach if we no longer
hold**; **serialise** the retry loop; and consider not releasing until the close is known placeable.
⛔ Control it: **a re-attach that has only ever failed proves nothing until one succeeds.**
