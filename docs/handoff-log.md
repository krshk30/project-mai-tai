# Handoff LOG — append-only narrative

> **This file is APPEND-ONLY.** New dated entries go at the TOP. Nothing here is ever rewritten —
> it is the record of what happened and why, including the wrong turns.
>
> ⛔ **Do NOT put current state here.** "What is true right now" lives in
> [`session-handoff.md`](session-handoff.md), which is OVERWRITTEN each session. Mixing the two is
> exactly what let the state rot for twelve days while this log stayed current (see 2026-07-29).
>
> **Maintenance:** monthly, roll entries older than ~2 weeks into
> [`handoff-archive/<YYYY-MM>.md`](handoff-archive/). No size cap applies to this file.

> Entries through **2026-07-15** were rolled to
> [`handoff-archive/2026-07.md`](handoff-archive/2026-07.md) on 2026-07-29 (verbatim, nothing edited).

---

## ⛔⭐ 2026-07-28 (NIGHT) — BACKTEST-vs-LIVE PARITY AUDIT (#592): the replay was studying a config we were not trading

Operator asked to confirm the backtest engine is "on the same level" as live before trusting it.
**It was not.**

### Finding 1 — the replay OVERRODE live config (FIXED, deployed)
`build_replay_settings` overlaid `LIVE_LOCKED` **after** the env-merged base, so a hardcoded list
beat production. Across all 90 live-relevant settings:

| setting | live | replay |
|---|---|---|
| `cw_v2_reclaim_enabled` | True | **False** |
| `cw_v2_eh_resting_entry_enabled` | True | **False** |
| `oms_v2_eh_entry_enabled` | True | **False** |

Reclaim went ON 07-27, EH flags ON 07-24; the list was never re-synced. **Reclaim off alone drops
`max_entries_per_flip` from 2 to 1** — the replay could not model a segment's second entry.
Fixed: LIVE_LOCKED is now a FALLBACK (`base.model_fields_set` ⇒ env wins), so it self-syncs.
`REPLAY_FORCED` carries the one real modelling choice (boot-hold released).
✅ Re-verified on the box after deploy: **89/90 identical**, 1 deliberate.
Re-runnable check: `/home/trader/_parity_diff.py`.

### Finding 2 — the engine itself is faithful
STKH 07-28: live `3.6899 → 3.7600 = +1.90%`, replay `3.6900 → 3.7600 = +1.90%`. Entry within
$0.0001, identical exit and reason. A real end-to-end match.

### ⛔ Finding 3 — three structural limits that bound EVERY comparison
1. **ONE round trip per symbol-day** — `if exit_done: break`. Live took **6** INLF round trips; the
   replay takes the first and stops. "1 vs 6" is structural, not a fidelity failure — but only the
   FIRST live trade of a symbol-day is ever comparable.
2. **Quote density** ~1 per 3.6-4.6s in the replay window vs a continuous live stream. The resting
   fill model needs "the first quote whose ask lands in [stop, limit]", so a 4s gap can miss a fill
   live caught, or fill at a different ask.
3. **Sparse-bar symbols are uncomparable.** CNET: 71 bars, 1 quote per 118s.
   ⛔ I nearly credited the new vol floor for CNET's "no entry" — **forcing the floor to 0 still
   produced no entry**, so it was DATA, not the gate. Disprove the flattering explanation.

### ⛔⭐ Finding 4 — do NOT judge parity on a day you deployed into
07-28 had **6 deploys mid-session** (orphan fix 15:15 ET, vol floor ~18:00, cooldown ~19:00). Live
ran >=4 code versions; the replay runs the final one. EGG proves it: the replay entered at 4.03 off
flip_level 4.0271 (the correct current trail) while LIVE was still sitting on the **orphaned** order
at 4.5257 from 13:30 — the #580 bug. **The replay was more correct than live was.**

**Next: re-run this comparison after a full session on stable code.** That is the clean test.

## ⭐ 2026-07-28 (NIGHT) — COOLDOWN REMOVED (#590) · no behaviour change

Operator, on reviewing the cooldown logic: *"per segment we are allowing our strategies to trade
once... two trades per ATR segment, one from resting, another from reclaim. Do we really need this
cooldown?"* — correct on every point.

### Why it existed, and why it doesn't need to
A 5-bar cooldown was armed whenever a position closed. It dates from when reclaim was **uncapped**
and could chase the same trade repeatedly. The per-segment cap replaced that need:
`_cw_v2_max_entries_per_flip` = **2** with reclaim on = one resting + one reclaim per ATR segment.

### It was already inert
Every gate that read the counter sits on a path `_cw_v2_enabled` short-circuits — `on_quote` returns
into `_cw_v2_quote` before the legacy touch/hold gate, and `_cw_entry` returns `None` on its first
line. **None of the three live paths (reactive, resting, fan-out) ever consulted it.** Same shape as
the liquidity floor the same evening: a gate guarding only replaced code.

### ⭐ Why REMOVE rather than leave it dormant
**It contradicted the design.** Reclaim gap = **1 bar**; cooldown = **5**. Wiring the counter back up
would block the exact second entry a segment is meant to allow (resting fills bar 1, spike bar 4,
reclaim). A switched-off safety gate invites a future session to "fix" it and silently break reclaim.

### ⛔ The hazard, now guarded by a test
The two lines that actually ENABLE reclaim lived in the same block as the cooldown:
```python
state.cw_v2_emit_claimed = False     # lets the segment's SECOND entry fire
state.cw_v2_bars_since_exit = 0      # starts the 1-bar reclaim gap counting
```
Removing "the cooldown" without keeping them stops every second entry, silently.
`test_a_close_still_releases_the_reclaim_claim` fails if either is dropped (mutation-verified).

### ⚠️ A break I introduced and caught
Removing the log ARGUMENTS left `cooldown=%d` in two format strings (`V2-CW-STATE-PROBE`,
`V2-MACD-PROBE`). Python's logging swallows a bad format into `--- Logging error ---` rather than
raising, so **both probe lines would simply have gone missing in production**. Fixed; all 28 logging
calls in the module are AST-verified for specifier/argument parity, and both lines were then proven
to RENDER on the deployed box, not just parse.

### ⛔ A correction to what I told the operator earlier that day
I had listed the spurious cooldown as *"silently blocking re-entries"*. **Wrong** — nothing live read
the counter, so it blocked nothing. I inferred impact from the arming without checking the readers.
The `SPURIOUS` label from #585 is now pointless for cooldown purposes, but stays on the close log
because a spurious transition still **releases the reclaim claim** (a real effect, worth watching).

### ⭐ Third default-vs-production divergence in one evening
`_cw_v2_reclaim_gap_bars` defaults to **0** in code; the box runs `..._CW_V2_RECLAIM_GAP_BARS=1`.
Same trap as the vol floor (5000 in code, 10000 live). Now documented in a test rather than hidden.
**Standing lesson: check the ENV before quoting any default as the live value.**

## ⛔⭐ 2026-07-28 (LATE) — the liquidity floor guarded ONLY DEAD CODE (#587, #588) — FIXED LIVE

Operator: *"we are buying at ATR flip without checking volume"*. Correct, and the cause was worse
than a missing check.

### The gate existed, was configured, and protected nothing
`strategy_schwab_1m_v2_atr_flip_vol_floor` is described in settings as **"the ONLY filter"**. It was
applied in exactly two functions — `_maybe_atr_emit` and `_cw_entry` — the A/B and break paths that
the resting flip-entry **replaced**. Every path that actually trades had no check at all:

| path | floor | status |
|---|---|---|
| `_maybe_atr_emit` (legacy) | ✅ | dead |
| `_cw_entry` (break) | ✅ | dead |
| `_cw_v2_quote` (reactive) | ❌ | **LIVE** |
| `_cw_v2_resting_track` (resting) | ❌ | **LIVE** |
| `_fanout_rth_resting_cross` (fan-out) | ❌ | **LIVE** |

> ⭐⭐ **configured ≠ enforced.** Sibling of "written ≠ used" (fossil DB columns) and "empty ≠ true"
> (the hardcoded snapshot). When a filter is *described* as protecting you, **grep every CALLER**
> before believing it.

### The live case the operator cited
```
CNET 2026-07-28
19:52:02  [V2-RESTING-PLACE] stop=1.4034   <- driving bar volume 4011
19:57:06  [V2-FANOUT-RTH-RESTING] px=1.4300 -> parallel Webull leg
          bought 1.43, stopped out 1.36 = -4.9%
```

### Design points that must not be undone
- ⛔ **ARM-ONLY on the resting path** — gates the initial arm, never a reprice or cancel. An order
  already working must keep being managed even if the tape thins, or we recreate the #580 orphan.
- ⛔ **The fan-out leg is gated separately** — it fires from its OWN software price-cross detector,
  so gating the Schwab primary does not cover it.
- ⛔ **ORB needed an ABSOLUTE floor.** It had only `vol_mult * avg_volume`; 1.5x a tiny opening-range
  average is still tiny. Added ON TOP of the relative gate, not replacing it.
- Judged on the **last COMPLETED bar** — the forming bar's volume grows through the minute.

### ⛔ settings.py had been lying about the value
The default said **5000** while the box ran `..._ATR_FLIP_VOL_FLOOR=10000` **all along**. I nearly
reported 5000 as the live value, and the operator revised a threshold decision believing it was 5000
("keep 5K, my mistake about 10K" — when production was already 10K). Aligned to 10000 in #588;
**live behaviour never changed**. ⭐ **Check the ENV before quoting a default as the live value.**

Raising the default turned **43 tests red** — all a *fixture collision*: they used volume exactly
`10_000` and the gate is strictly `>`. Bumped to 25_000. ⛔ Only volume literals; `± 10_000` in the
same files are millisecond timestamps.

Memory: `project_mai_tai_liquidity_floor_guarded_dead_code`.

## ⭐⭐ 2026-07-28 (EVENING) — after-close batch RUN: 3 PRs merged, 2 flags flipped, 2 studies closed

Ran the whole 07-28 after-close batch. **Three of my own prior assumptions were wrong and are
corrected below** — that is the main value of this entry.

### Deployed / flipped
| item | outcome |
|---|---|
| **P0-a** event-driven exit capture | **LIVE** — `MAI_TAI_OMS_NATIVE_OCO_EXIT_POLL_ENABLED=true` + OMS restart. Proven within a minute: `[OMS-V2-OCO-RESOLVED-FLAT] INLF ... closing phantom managed row (no ladder rejects)`. **No 429 flood**: 1-2/min after vs 12+/min in the pre-restart EOD burst. |
| **P0-c** Webull realign | **OFF** (`..._REALIGN_ON_FILL_ENABLED=false`), verified loaded — the attended check had failed with `OAUTH_OPENAPI_ORDER_CANT_NOT_BE_REPLACE`. |
| **#582** scanner CONFIRM timestamp | merged + deployed (strategy engine restarted) |
| **#583** `cw_entry_n` off-by-one | merged + deployed (v2 restarted) |
| **P1-1** #574 | already live from the 15:15 ET restart |

### ⭐⭐ P2-5 NO_FRESH_QUOTE — CLOSED, NO CHANGE NEEDED (the study measured the wrong feed)
`quote_staleness_at_signal.py` reports **23.5%** of RTH entry signals sitting on a quote >2s old,
and it is chronic rather than one episode (drop-one moves it only 23.5 -> 20.8%; 16 symbol-days).

**But that is POLYGON `market_capture_quotes`, and the real gate is OMS-side reading the OMS's own
broker ask.** Actual `NO_FRESH_QUOTE` fires in the entire OMS log: **3** (1 on 07-20, 2 on 07-28 =
the INLF case that prompted the question).

> ⭐ **Third instance of the bar-source defect**: a study built on Polygon used to judge a
> broker-fed decision. **Check which feed the DECISION reads before measuring it.**
> Also `NO_FRESH_QUOTE` lives in `oms/service.py`, not the strategy — grepping the v2 log gives 0.

### ⚠️ P2-7 missed flips — 36% measured, DO NOT act on it yet
22 watched BUY flips today, **8 never armed** (ENTX 4/4, CNET 2/3, BIYA 1/1, INLF 1/6).

- ⛔ **Not the cooldown.** CNET at its missed flip logged `pos_qty=0 cooldown=0`.
- ENTX had **no probe lines at all** at those instants -> v2 was not processing the symbol. It was
  absent from the live watchlist (2-5 symbols at a time; the cap of 25 is **not** binding — the
  confirmed set itself churns).
- ⛔ **The windows come from `scanner_confirmed_events`, whose bug #582 fixes FORWARD ONLY.**
  Today's rows predate the fix and one (POLA) was demonstrably future-dated. **Re-run on a clean
  day before believing 36%.**

### P1-3 backfill — 4 of 8 exits recovered, and the other 4 never can be
The exit row id is `f"{entry.client_order_id}-ocoexit"`, so N exits mapping to ONE entry collapse
into one row and `record_fill_if_needed` rejects the rest at `incremental_quantity <= 0`. BIYA had
4 real exits on 07-27 but one entry order -> only 1 recoverable.
**This affects the LIVE capture too**, whenever a symbol is entered twice in a segment (reclaim).
Fix shape: put the CHILD id into the exit `client_order_id`.

### Follow-ups CLOSED the same evening (#585) — operator-decided
| decision | outcome |
|---|---|
| Webull 429 loses a trade's P&L | **RETRY, bounded.** `_fetch_oco_exit_detail` now returns `_EXIT_FETCH_FAILED` (distinct from "no exit") and the managed row is held up to `_MAX_EXIT_FETCH_DEFERRALS=3` (~45s). ⛔ Bounded because an open row blocks fan-out re-entry — protection still outranks bookkeeping. |
| Only ONE exit per entry order | **FIXED.** The child id joins the exit key. BIYA had 4 real exits on 07-27 and 1 was recordable; this bit RECLAIM hardest, i.e. the population being judged right now. Fills still dedupe on `broker_fill_id`, so no double-count. |
| Spurious 5-bar cooldown | **MEASURE FIRST.** Log now labels `real-position-closed` vs `SPURIOUS-no-shares-ever-held`. Behaviour UNCHANGED and pinned by a test — loosening a cooldown means more live entries. Count for a week, then decide. |
| Hand-cancel doesn't stop the fan-out leg | **NO CODE — operating procedure.** Hand-cancel **and** set `global_manual_stop_symbols` (#556, built for exactly this). ⛔ And it is NOT "cancel asymmetry": a direct broker DELETE bypasses the bot's cancel path, and the Webull leg fires from a *software* price-cross detector, not from the Schwab order. |

⭐ **An existing invariant was amended explicitly**, not silently:
`test_a_broker_failure_never_breaks_the_close_path` pinned "the row must still close" via the helper
returning `None`. That contract changed, so the test now asserts the sentinel **and** drives the
retry loop to prove it terminates — "the row always closes in the end" is still pinned, just bounded
instead of immediate. Mutation-checked in four directions; suite green at 1622.

### Corrections to the batch's own premises
- Scanner timestamps: **1** corrupt row, not "~3". And **"no fractional seconds" is NOT a
  corruption tell** — 78 CONFIRM rows in 3 days have none, because every correctly-parsed time-only
  string lacks them. The only sound detector is future-dating.
- **No historical repair is possible**: a row future-dated yesterday is indistinguishable from a
  legitimate past time today.

### Open, with evidence attached
- A Webull **429 permanently loses an exit fill** (`closing without a recorded exit`) — transient
  error, permanent give-up, the exact blackout the capture exists to close.
- **Spurious 5-bar cooldown** whenever a resting intent goes terminal (same union as the orphan).
- **Schwab/Webull cancel asymmetry**: cancelled the Schwab leg 15:18 ET, the Webull leg still
  filled 15:19 (+2.09%). Worked out, but a one-sided cancel needs understanding.

## ⭐⭐⭐ 2026-07-28 (INTRADAY) — RESTING-ORDER ORPHAN root-caused + FIXED LIVE (#580) · #578 REVERTED

**Operator report:** "Resting order again is way off… we have to adjust every minute." EGG's resting
buy-stop sat at **3.93 while price fell to 3.55** and the bot never adjusted it. The operator
hand-cancelled EGG **three times** in one afternoon. Same shape as POLA on 07-27.

### Root cause — pinned, not inferred
`_fetch_open_positions` returns `virtual_positions ∪ in-flight OPEN intents`. **A resting order's
intent stays `submitted` for its ENTIRE life** — it only resolves when price triggers it. So the
union reported `qty=2` for an order that had never filled, tripping the first gate of
`_cw_v2_resting_track`, which cleared `resting_active` **without cancelling the broker order**.
From that instant neither the 0.5% STABLE-REST reprice nor the flip-no-fill cancel could fire.

*Broker cross-check:* the order was still `WORKING` (unfilled) at the same moment the bot logged
`pos_qty=2`. A working buy-stop and a position cannot both be true.

### ⭐⭐ It is a LATCH RACE — and the latch is permanent
Same day, same code path:

| symbol | trail behaviour | `pos_qty` while resting | outcome |
|---|---|---|---|
| INLF | moved ≥0.5% every 2–3 min | `0` throughout | **24 reprices**, healthy |
| EGG | sat still | latched to `2` | **0 reprices**, orphaned |

INLF repriced *before* the position poll saw its own intent. EGG's trail sat still, the poll won the
race once — and the gate then blocked every **future** reprice too. **Losing the race a single time
orphans the symbol for good.** That is why "it adjusts sometimes" and "it never adjusts" were both
true reports, and why this looked intermittent for two days.

### ⛔ The wrong fix I nearly shipped — #578, reverted by #579
First attempt was a "cancel a resting order that drifted >4% from the market" bound. **Checking it
against the LIVE order before deploying killed it:** EGG's *legitimate* order sat at 3.93 — exactly
ON the ATR trail — and was **5.93% above mid**. The guard would have cancelled a healthy setup.
⭐ **Distance from MARKET cannot separate stale from valid** (on a volatile name the trail is
legitimately far above price — that IS the premise of a resting buy-stop). Never pick a threshold
without testing it against a live *valid* case. #578 was merged but **never deployed**; reverted.

### The fix (#580, `347f146`) — deliberately surgical
Only the resting-order **ownership** gate reads a fills-only count `position_qty_held`. New
`_fetch_position_maps()` returns `(union, held)`; `_fetch_open_positions()` keeps its exact
signature/return; `update_position(..., held_qty=None)` defaults to `qty` so every existing caller is
byte-identical. ⛔ **Every other gate keeps the conservative union on purpose** — reactive entry,
cooldown, re-entry, fan-out, protected-symbols. Dropping resting intents there would let a market buy
fire while a stop-limit rests = **double position**.

5 new tests reproducing EGG (incl. the order actually *following the trail down*).
**Mutation-checked both ways.** Full unit suite green (1598).

### Deploy — 15:15 ET, attended, fleet FLAT (0 shares in `virtual_positions`)
Merged → pulled → `schwab-1m-v2` restarted clean. The still-orphaned EGG order (stop=3.81, price
3.675) was cancelled at the broker first, because the restarted bot has no memory of it and would
otherwise have placed a **second** live buy order alongside it.

### ⚠️ Follow-up NOT done
The same union arms a **spurious 5-bar cooldown** whenever a resting intent goes terminal
(`"cooldown armed for EGG — position qty 2 -> 0"` at 18:21:40 UTC with **no real exit**). Same root,
but it changes entry *timing*, so it needs its own measurement.
⛔ **Corollary: a `"position qty N -> 0"` log line is NOT proof of a real exit** — check `fills` /
`virtual_positions` before reading one as a round trip. I misread two of them as round trips today.

Memory: `project_mai_tai_resting_order_orphan_latch`. P0-b in the 07-28 after-close batch is CLOSED.

---

## ⭐⭐⭐ 2026-07-27 (pt 4, EVENING) — P&L blackout ROOT-CAUSED + FIXED · 3 flags ON · reclaim back ON

Post-close session. **13 PRs merged today.** Everything below is deployed and verified.

### The headline: the bot page's P&L was not "never wired" — it BROKE on 07-22
Operator: *"PNL from bot's page is blank... used to work till Friday."* They were right and I was
wrong to say the field was never populated. The page's P&L comes from
`collect_completed_trade_cycles` over DB **`fills`**, NOT the snapshot's hardcoded `daily_pnl`.

    Schwab sell FILLS   07-20: 3 · 07-21: 5 · 07-22: 1 · 07-23: 0 · 07-27: 0
    Schwab sell ORDERS  07-23: 11 REJECTED · 07-27: 6 REJECTED

Exit fills stopped **the day the native OCO went live**. The exit executes on a broker-created
child leg the OMS never placed, so nothing books a fill; the OMS then fires its own close, which
the broker rejects (already flat). **Not a Webull problem** — the fan-out only made it total.

**FIXED in two steps, both deployed:** #565 `fetch_oco_exit_fill` on BOTH adapters (Schwab walks
`childOrderStrategies`; Webull uses the `T`/`S` suffixed coids), #566 wires both close paths to
record the exit as a real order + fill. ⛔ Two traps, both found live: a **CANCELED sibling carries
an execution priced 0.0** (booking it = a −100% trade), and **Webull 429s** if you query both legs
(only one can fill → return on the first hit).

### Also fixed tonight
| PR | what |
|---|---|
| #562 | **fill-anchored OCO bracket** — legs were priced off the pre-trade REFERENCE, so the "−5%" stop actually ran **−3.85%..−5.83%** (12 combos: ALIGNED 8 / DRIFTED 4) |
| #563 | attended-check runbook for #562 |
| #567/#568 | **the OCO watch pager** (`*/15 14-21 UTC`, ET guard 10:00–16:30) |
| #569 | the overnight flatten **paged 58× in 4 min over a phantom row** and cleared nothing — no-bid ≠ naked, so it now ASKS THE BROKER |
| #570 | **entry segment identity** (`cw_entry_n` + `cw_arm_bar_ts`) so reclaim can be judged on live fills |

### ⚠️ FOUR flags switched ON tonight — all were OFF by default
    webull_bracket_realign_on_fill_enabled      = True   (#562)
    oms_record_native_oco_exit_fills_enabled    = True   (#565/#566)
    strategy_schwab_1m_v2_cw_v2_reclaim_enabled = True   (reversal of #456)
    (oms_native_oco_resolve_flat_reconcile_enabled was already True)

**Reclaim is the one to watch.** It reverses a decision made on LIVE money (firsts n=17 win 58%
median +1.93% · **reclaims n=13 win 38% median −4.98%**). Operator's call, and the reasoning is
sound: *"testing is not going to give us the real actual issues — only the live."* Two things
differ from July: the **1-bar reclaim gap is now ON** (targets the ~8s re-entry that caused it) and
exits are native OCO. ⭐ It moves the **REACTIVE** path (7d: 19 orders / 12 filled); the **resting
path is NOT capped** by `max_entries_per_flip` and never was.

### ⛔ Judging reclaim: do NOT group by `cw_flip_level`
It repeats across segments when the ATR trail has not moved — FIEE booked two SEPARATE round trips
2 min apart at an identical level, and BIYA/ENTX looked like 2-per-flip on a day reclaim was OFF.
Use `cw_arm_bar_ts` (segment) + `cw_entry_n` (1=first, 2=reclaim), shipped in #570.
[[project-mai-tai-v2-entry-segment-identity]]

### Which strategy trades where (asked and answered)
Only **schwab_1m_v2** touches Webull, via the fan-out. `polygon_30s` is **paper only**; ORB is
inactive. Fill asymmetry is by design: Schwab rests a stop-limit (~13% trigger), Webull buys MARKET
at the cross (~100%).

### ⛔ Process notes (the two that cost the most)
- **`awk '$0 >= "<date>"'` compares LEXICALLY** — untimestamped traceback/JSON lines pass ANY date
  filter. Produced FOUR false alarms today (1630, 414, plus two smaller). **Anchor with `^2026-`.**
- **A cron script committed from Windows lands 100644** and silently never runs. #568.
- Mutation testing caught **6 tests passing for the wrong reason** across the day — an unconfigured
  account short-circuiting to None, and filters masking each other. Isolate each guard.

### Open
1. **Watch the 4 flags tomorrow** — the pager covers two of them; reclaim needs the `cw_entry_n` split.
2. Fossil-warmup guard on newest-bar age (⛔ design doc first — bar-build).
3. `test_scanner_cycle_history_retention_and_dedup` is **FLAKY** (leaks a pending
   `hydrate-generic-ELAB` task): failed 2×, passed 4× incl. alone on a clean tree.
4. Missed-flip sweep across ~2 weeks (off-hours), tracker now honest.
5. `docs/session-handoff.md` is ~1800 lines vs its own ~400 rule — roll into `handoff-archive/`.

---

## ⭐⭐ 2026-07-27 (pt 3, FINAL) — **LIVE OPS DAY**: 7 PRs · Webull fan-out made VISIBLE · BIYA SOLVED · bracket-anchoring defect found

Market-hours session, all deployed and verified live. Fan-out stayed **ON** (operator's call after
being shown the risk).

### Shipped + deployed
| PR | what | deployed |
|---|---|---|
| #556 `bb6ac10` | OMS honours `global_manual_stop_symbols` at `_evaluate_risk` — live per-symbol veto, no restart, fail-closed | 11:17 ET |
| #557 `1d8eca0` | **Webull combo status poll uses the MASTER coid** (`...M`) — the day's key fix | 12:33 ET |
| #558 `8d0be03` | manual stop is **exposure-directional** — blocks entries, NEVER blocks exits | 13:06 ET |
| #559 `031e6a3` | v2 snapshot reports **real positions across BOTH brokers**, labelled `primary`/`fanout` | 13:07 ET |
| #553 `3b99482` | gateway reference-cache periodic refresh (merged earlier, deployed today) | 13:36 ET |
| #561 `b159cd1` | the cooldown log no longer claims a cause it cannot observe | 16:48 ET |
| #562 `53689a9` | **fill-anchored OCO bracket** — flag `..._REALIGN_ON_FILL_ENABLED` **staged=false, inert** | 16:48 ET |
| #563 `b16d09e` | attended-check runbook for #562 | docs |

Also: `DFNS` removed from `MAI_TAI_PROTECTED_SYMBOLS` (now `CYN,CELZ`) and moved onto the
manual-stop lever — verified `DFNS open -> BLOCKED (manual_stop)`, `DFNS close -> allowed`.

### ⭐ #557 — the one that mattered
`_place_combo_bracket` places legs under SUFFIXED coids (`_combo_leg_coid(base,"M"/"T"/"S")`); the
status poll asked for the BARE base → `417 ORDER_NOT_FOUND` **forever** (542 fetch failures/hour).
Four Webull fan-out legs filled AND closed at the broker while v2 reported `positions: []` /
`daily_pnl 0.0`. **542/hr → 0**, and orders now carry REAL Webull order ids.
⛔ **invisible ≠ unmanaged** — the native OCO worked on every trade; I claimed they were naked and
the broker tape disproved it. [[project-mai-tai-webull-combo-status-poll]]

### ⭐⭐ BIYA "08:19 flip never armed" — SOLVED
Schwab REST warmed newly-confirmed symbols with a series whose **newest bar was weeks old**
(LGHL ~60d, BIYA ~46d, ENTX ~35d — 3 of 3, at their exact CONFIRM timestamps). Indicators were built
on June prices. Schwab later served BIYA fine (398 fresh bars incl. the real `08:19 BUY 2.8300`).
⛔ **`[V2-CW-ARM]` also fires during WARMUP REPLAY** — one log instant emits dozens of arms with bars
spanning weeks. That is why #552's `arm_bar_ts>24h` guard blocked *every* arm, AND why my "81% of
arms are stale, so it's normal" base rate was measuring the wrong quantity.
**Guard the NEWEST bar's age, never `arm_bar_ts`.** Fix NOT built — bar-build is design-first.
[[project-mai-tai-v2-fossil-warmup-series]]

### ⛔ BLOCKER found — no realized P&L exists for natively-bracketed trades
Today's `fills`: **7 buys, 0 sells.** Exits execute on the broker-side OCO child legs (`...T`/`...S`)
which the OMS never polls, so no exit fill is ever recorded. `daily_pnl`/`closed_today` therefore
**cannot** be computed and were deliberately left hardcoded rather than fabricated.
Exit data IS retrievable — probed live: BIYA `STOP_PROFIT status=FILLED filled_price=3.9300`
(entry 3.859 = **+1.84%**). **Next fix: poll the `T`/`S` legs to capture the exit fill.** It reuses
#557's proven suffix mechanism and would additionally fix phantom rows (positions would close on the
real exit instead of after 3 rejected closes).

### ⭐ BRACKET-ANCHORING DEFECT — found by auditing every Webull trade against the broker tape
The combo is placed as ONE atomic order, so BOTH exit legs are priced off the pre-trade REFERENCE
**before the master has filled**. The Webull leg is MARKET-at-the-ATR-cross = exactly where slippage
lives, so the realised bracket drifts off spec. Every `[V2-OCO-EMIT]` is arithmetically perfect
(+2%/−5% of its reference) — the bug is purely *what it anchors to*.

**12 fan-out combos today: ALIGNED 8 / DRIFTED 4** (aligned = +2.00%/−5.00% **of the fill**, ±0.30%):

    LGHL 12:32  fill 1.200  target +1.67%  stop -5.83%   <- worst
    BIYA 14:00  fill 3.936  target +1.63%  stop -5.49%
    BIYA 12:51  fill 4.120  target +1.70%  stop -5.34%   -> realised -5.83%, BEYOND the design limit
    FIEE 13:00  fill 5.980  target +3.18%  stop -3.85%   <- drifted the OTHER way: stop too TIGHT

So the "−5% stop" actually ran **−3.85%..−5.83%** and the "+2% target" **+1.63%..+3.18%**.
BIYA 12:51's overshoot was ~2/3 anchoring + ~1/3 stop-market slippage — **#562 removes the former
only**; don't read a residual overshoot as the fix failing.
⛔ Only the Webull leg is exposed: the Schwab leg is a resting buy-stop-LIMIT, so its fill lands at
the trigger and the bracket stays aligned.

### Fan-out results today (per-trade %, median-first)
Bot-only, the 5 that ran to their OWN exit: **+1.84% · +1.90% · +3.18% · −4.90% · −5.83%**
→ **median +1.84%**. Three more were closed by hand by the operator (QBTX −3.80%, LGHL −2.50%,
QBTX −0.45%) and are NOT strategy outcomes. n=12 at qty 1 — **not a verdict**.
⚠️ A **FIEE 313-share** round trip (6.06 → 5.43, −10.40% in 33s) was **NOT placed by mai-tai** —
qty 313 vs our qty 1, bare Webull uuid coid, no bracket, extended-hours session. Largest dollar
event of the day and not attributable to the strategy.

### ⭐ First HONEST missed-flip base rate (off-hours sweep)
`scratchpad/missed_flips.py` rewritten to scope each symbol to its real CONFIRM→DROP windows (v1
judged every flip since 04:00, including ones before the symbol was watched):

    17 WATCHED BUY flips · 6 NOT ARMED = 35% miss rate
    77 excluded as out-of-window   <-- v1 would have called these misses
     0 excluded as fossil

Of the 6: BIYA 08:19 + LGHL 08:50 sit inside the 07:56–09:04 window when the bad #552 guard was live
(**self-inflicted**), and DFNS 15:21 is expected (blacklisted/manual-stopped from ~10:42).
⇒ **~3 genuinely unexplained**: BIYA 09:34, ENTX 09:43, DFNS 10:30. ONE DAY, n=17 — a direction,
**not a verdict**. Re-run across ~2 weeks before concluding anything.

### Live ops state at END OF DAY (17:00 ET)
All 6 services active, `NRestarts=0`, heartbeats fresh, **0 errors since the 16:48 restart**.
`PROTECTED_SYMBOLS=CYN,CELZ` · manual-stop row `["DFNS"]` · fan-out **ON** (Schwab qty2 + Webull
qty1 → `live:orb`) · realign flag **staged false** (verified to parse as `False`) ·
`virtual_positions` empty = **flat at both brokers**. The junk dirs whose names were literal
Windows paths under `/home/trader/` are **removed** (9 empty dirs, via `rmdir`, zero files
inside). Env backups:
`.bak.pre-fanout.20260727T135456Z` · `.bak.pre-protect-dfns.20260727T144811Z` ·
`.bak.pre-unprotect-dfns.20260727T173448Z` · `.bak.pre-realign-stage.20260727T205312Z`.

### ⛔ Process notes (five — all self-inflicted, all worth not repeating)
1. **#552** shipped a fossil-arm guard with no base-rate check → zero arms possible 07:56–09:04 ET.
   Rolled back + reverted (#554). 23 failing tests were the signal; I explained them away.
2. **Twice** I raised false alarms from my own `awk`/regex filters: `awk '$0 >= "<date>"'` compares
   **lexically**, so untimestamped traceback/JSON lines (starting `}`) pass ANY date filter and drag
   in history. Reported "1630 errors" and "414 errors"; anchored counts were **0** and **6**.
   ⭐ **Always anchor log filters with `^2026-...`.**
3. **#556 → #558 same day**: blocking every intent type would have stranded an open position.
4. Twice I asserted a conclusion the broker tape then disproved — "the fan-out legs are naked"
   (the native OCO had worked on every one) and "my new tests contaminate the suite" (the identical
   command passed on re-run). ⭐ **Check the broker / re-run before calling something broken.**
5. `test_scanner_cycle_history_retention_and_dedup` is **FLAKY** — failed once, passed on re-run of
   the same command and in two full suites. Unrelated to today's changes; worth its own look.

### Open items (end of day)
1. **⏭️ ENABLE the realign flag under an ATTENDED check** (operator's call, next session). Runbook:
   `docs/webull-bracket-realign-attended-check.md`; verifier `/home/trader/verify_realign.py` reads
   the answer off the BROKER. The ONLY unproven part is whether v3 `replace_order` accepts a PARTIAL
   combo (2 exit legs, master omitted because filled). A failed realign is **not** an emergency —
   the original bracket stays and the position stays protected.
2. **Poll OCO `T`/`S` legs for exit fills** — unblocks `daily_pnl`/`closed_today` (today's `fills`
   were **7 buys, 0 sells**) and fixes phantom rows. Highest value after #562.
3. Fossil-warmup guard on **newest-bar age** (⛔ design doc first — bar-build).
4. Missed-flip sweep across **~2 weeks** (off-hours) now that the tracker is honest.
5. Cooldown-strands-a-live-order (EDBL 2.77% drift) — needs a base rate first.
6. `docs/session-handoff.md` is **~1700 lines** against its own "keep under ~400" rule — roll
   entries older than ~2 weeks into `handoff-archive/`.

---

## ⭐ 2026-07-27 (pt 2) — **R2 REPLACED**: 3-MIN TIME STOP + FLOORED TRAIL 3% (robust +0.62%) · NO live change

**Operator's call: R2 is no longer the breakeven cut.** *"The breakeven never worked anyway — the
3-min stop plus floor trail 3% is our R2."* Full detail: [[project-mai-tai-v2-three-exit-rules]].

    not +2% by minute 3  -> EXIT AT MARKET (~-0.8% median, range -2.61%..+0.68%)
    +2% by minute 3      -> PROVEN: floor = max(+2% level, peak x (1-3%)), ratcheting,
                            breach judged at the BAR CLOSE.   (-5% stop + flip stay as backstops)

**robust +0.62%** vs baseline -0.75% · median **+0.05%** · mean +1.94% · **win 51.9%** · worst -5.19%.
12 of 27 prove within 3 min.

**⭐ WHY A CLOCK, NOT A PRICE — the insight that unlocked it.** A breakeven at the FILL fires in ~0.5s
on **27 of 27**: we buy at the ASK and the next print is at the BID, so the **spread itself** trips it
before the stock does anything. A clock cannot be tripped that way. Same root cause as the +2%-floor
failure — we kept placing exits closer to price than the market's own noise.

**⭐ WHY 3 MINUTES — validated twice, independently.** Winners reach +2% in a median **1.6 min**;
losers that ever get there take **75.7 min** (47x). Losers reach -3% in **1.4 min** vs winners' 8.4m.
And the time-stop sweep peaks at 3 min on BOTH curves (target 1m -0.25 / 2m -0.09 / **3m -0.04** /
4m -0.35 / 7m -0.56; trail 1m +0.12 / 2m +0.35 / **3m +0.40** / 4m -0.17 / 7m -0.56).

**⭐ WHY THE FLOOR (operator's addition).** Without it the trail books UNDER +2% on trades that had
already earned it — CPHI 07-21 **-5.35%**, ADVB **-5.32%**, CPHI 07-15 -3.24%, LGPS -2.92%, CJMB -2.41%
— all become +1.60/+1.68/+1.85/+1.67/+1.61% with it, while the real runners still run (ZYBT +36.44%,
ZCMD +5.92%, UBXG +5.51%, ERNA +4.44%). ⭐ Take the floor even though plain-trail-5% scores marginally
higher (+0.80%): the floor gradient has a proper **interior peak** (0.5%=+0.37 1%=+0.37 2%=+0.36
**3%=+0.62** 5%=+0.43) while plain-trail is **still climbing at the tested edge** — the pattern that
produced two false winners this weekend. Floor also gives 52% wins vs 33%.

**⚠️ COST (operator accepts):** it caps slow-starting monsters — AGEN **+27.58% -> +1.75%** (dipped
under +2% right after proving, then ran +51.8%), NXTC +8.53->+1.86, VMAR +6.81->+2.09; ATPC (peak
+37.9%) and VEEE (+2% at 4.8m, peak +56.1%) time out. Operator hopes reactive catches them —
⚠️ but reactive is capped at +2% today too, so it catches the trade, not the move.
**⚠️ Honest label:** with the floor on, trail width barely matters below 3% — the floor does the work.
This is really *"take +2% on the first weak BAR CLOSE unless it is still running hard."*

**SHORTLIST (all vs baseline robust -0.75%, win 63.0%):** ⭐ **R2-v2 +0.62% / win 52%** ·
old-R2+R3 +0.75% / win 26% / worst -2.94% · 3-min+plain-trail-5% +0.80% (⛔ untrusted gradient) ·
3-min+target -0.04% / win 52% (safest step up) · R1 speed-gate+trail-2% -0.24% / win 63%.

---

## ⛔ 2026-07-27 — R2 "breakeven race" variant TESTED AND REJECTED (don't re-litigate) · NO live change

**Operator's objection to R2 was good and is CONFIRMED:** judging "weak" on the ENTRY BAR alone is
hasty — of the 20 trades R2 marks weak, **15 DID reach +2% later** (AGEN peak +51.8%, VEEE +56.1%,
EHGO +24.6%, …); only 5 never did (INM, LABT, SMCX, KUST, SKYQ).

**⛔ But the proposed fix — keep a breakeven armed and let the trade RACE to +2% — is WORSE.** 18
variants (arm at fill / entry-bar close / 2 bars × buffer 0/0.25/0.5% × proven-gets target/trail3):
best **robust +0.11%** vs the existing **R2-v1+R3 = +0.75%**. ⭐ **Armed at the FILL it cuts 27 of 27
— a 0.0% win rate: NOT ONE trade reached +2% before dipping back to the buy price.** Same tick-grid
cause as the +2% floor — the resting order fills on a **WICK** at the top of a spike, so price sags
back through the fill within seconds. Arm at the entry-bar close → 21/27 cut; arm 2 bars in → 16/27.

**⭐ And the objection is ALREADY ANSWERED by the combined rule.** A weak trade is not condemned: its
exit is whichever comes FIRST among {breakeven, +2% target, −5%, flip}, so a weak trade reaching +2%
before returning to the fill **takes the +2%** (`CPHI 07-15, 1st bar +1.85%, [weak] → +1.85%
[target]`). ⇒ **Correct framing of R3: the first-bar high is NOT a verdict on the trade — it only
decides WHO GETS THE TRAIL INSTEAD OF THE +2% TARGET**, and that is earned (STRONG peak median
+17.8% vs +7.8%). Only open sub-question: should a weak-but-PROVEN trade get the trail rather than
the target? (+0.11% vs +0.75% here — no on this sample; revisit with more data.)

**📋 THE 27-TRADE REFERENCE SET printed** (corrected baseline = today's live behaviour): 17W/10L,
win 63.0%, median +1.61%, mean −0.56%, **sum −15.13pp**. ⭐ **Median hold ≈ 4 MINUTES, 8 trades done
in under 60 seconds** — the structural reason bar-based signals cannot time these exits. Worst
give-backs: **ZYBT 07-20 in 12:27:09 out 12:27:11 (2 SECONDS) +1.78% while the stock went +173%**;
**CPHI 07-21 (7s) +1.60% while it went +105%.** Reproduce: scratchpad `print27.py`.
[[project-mai-tai-v2-three-exit-rules]]

---

## 🔬⭐ 2026-07-26 (Sun) — R&D DAY: v2 RESTING exit research (NO live change) + replay flip-leg bug FIXED (#549)

**Market closed; nothing deployed; live is UNCHANGED and stays on the baseline** (+2% target / −5%
stop / flip). This was a full research day on the **RESTING entry only**, 10 days (07-13..07-24,
27 trades — 07-10 excluded, its trade tape is already pruned). All tooling in the session scratchpad,
run on the VPS. Memory: [[project-mai-tai-v2-three-exit-rules]], [[project-mai-tai-v2-exit-upside-research]].

**⛔ THE BUG THE OPERATOR CAUGHT (fixed, PR #549).** `backtest/replay.py::_open_static_oco` modelled
only target/stop/close-at-bell and set `exit_done=True` immediately, so it **omitted the live
bar-close flip exit** — `schwab_1m_v2._maybe_cw_flip_close` fires whenever CW is on + holding + a bar
CLOSES below the ATR trail, and it has **NO RTH gate**. Spotted off a TOS chart: **SMCX 07-22** held
to the bell at −2.81% when live would have flip-closed **14:33**. Fix mirrors the existing EH branch
(real strategy emits the draft; fill = first print at/after the bar close). Impact: 2 of 27 trades
change (SMCX −2.81%→−1.40%, KUST −5.48%→−4.78%); baseline robust mean −0.83%→−0.75%. Test pins 4
cases and **was verified to FAIL without the fix**. Golden gate 16 green, 1534 unit pass, ruff clean.
⭐ It also corrected my own inference — *"the flip never fires because the target pre-empts it"* was
wrong; there was no flip leg to fire.

**⭐ THE FINDING THAT STARTED IT — the +2% target really does cap winners.** MFE on the 17 resting
winners (entry→16:00, off the raw tape): **median +14.5%** (+9.85% on the conservative max-1-min-close
measure) against ~+1.75% booked; **14 of 17 left ≥5pp on the table**. Peaks verified as real prints
(ZYBT +173% had 448 prints within 0.5% of the peak). ⭐ This **corrects the 07-15 floor-ratchet study**
("winners peak +2.01..+2.43%") — that was measured on live positions **already closed at +2%**, so it
structurally could not see higher. The ceiling was the instrument.

**⛔ WHAT WAS TESTED AND FAILED** (all vs the corrected baseline, robust mean −0.75%): 14 exit signals
+ 5 combos (MACD cross, histogram-shrink N=1-4, StochK <80 / falling, volume-fade, ATR flip) — **every
one lost**, best `stoch_fall3` −0.80%. ⭐ **Mechanism: no signal BRACKETS the peak** — median lag vs the
price peak runs −21 bars (stoch_dn80) to +16 bars (atr_flip); all booked ~5% of the available move.
Also closed: a FIXED floor **is** the target (arithmetic — it fires 0.0–0.3s after arming because the
resting fill sits ~1 TICK above it), and all 9 dynamic ladders collapsed to identical numbers for the
same reason. ⛔ >100 configurations were tested on 27 trades — **stop-optimizing marker**.

**✅ THE THREE RULES THAT SURVIVED (resting only, NOT deployed):**
| rule | robust mean | note |
|---|---|---|
| R1 trail the movers (**judge breach at the BAR CLOSE, never intrabar**) | −0.24% | keeps win 63% + worst −5.53% |
| R2 **breakeven-cut** when the entry bar's HIGH < +2% | +0.04% | the robust core; worst −5.53%→**−2.94%** |
| R3 **first-bar high ≥+2% = the runner filter** | (a gate) | STRONG peak median +17.8% vs +7.8%; holds 3 of 4 monsters |
| **R2+R3 COMBINED** (disjoint subsets → additive) | **+0.75%** | **+1.50pp/trade**; trail 3% optimal in ALL 6 sweeps; both legs pay evenly w/o ZYBT |

⚠️ Combined costs win rate **63%→26%** and median +1.61%→−0.23% (many ~0% scratches, few big wins) —
better RISK, very different feel. ⚠️ The operator's **re-entry safety net does NOT hold**: 8 of 19 cut
trades had a later reactive entry, 5 won / 3 lost, **net −1.7pp, and none caught a tail** (reactive is
capped at +2% too).

**🔜 NEXT SESSION — REACTIVE.** It is still plain +2%/−5% — i.e. exactly where RESTING started today,
with a −5% loser against a +2%-capped winner. Operator: *"change that reactive a little bit like that,
but not right now… run it next week and see."* Apply R1–R3 there. **Until validated, LIVE STAYS ON THE
BASELINE.** Operator wants to validate every live trade by hand next week; Thu/Fri produced ~zero
trades, which is exactly why the **dual-broker fan-out** matters for getting live samples.
⚠️ Everything above is n=27 backtest — the data is pruned at 07-13, so more evidence must come from
**FORWARD-testing, not more backtesting.** Nearly-free item found on the way: **anchor the OCO legs to
the FILL, not `entry_ref`** (a "+2%" target is really +1.78%, a "−5%" stop really −5.12%; ~+0.17pp/trade).

---

> **Sessions 2026-07-16 .. 07-25 moved to** [`handoff-archive/2026-07.md`](handoff-archive/2026-07.md) on 07-28 (verbatim, nothing edited).

## 🚦 STATUS — v2 IS LIVE · NOW ON THE CONFIRMED-WINDOW RULESET (2026-07-10, canary qty 2)

> **⭐ SUPERSEDES the ATR touch/flip framing below (kept for history).** On **2026-07-10 ~00:07 ET** (attended, market
> closed, fleet flat) v2's **entry+exit logic was REPLACED wholesale** with the **confirmed-window (CW) ruleset**
> (operator: *"don't wait 30 days; change the rules, keep the plumbing; real money, NOT shadow"*). **There is no more
> Path-A / Path-B — v2 ALWAYS waits 3 bars and enters on a confirmed break; the whole bar-close-fallback structure is
> gone.** Running config (deploy HEAD `b94ba7d`): `CONFIRMED_WINDOW_ENABLED=true`, `HOLD_CONFIRM_ENABLED=false`,
> `ATR_ONLY_MODE=true`, `OMS_V2_EXIT_MANAGEMENT_ENABLED=true`, **`ATR_FLIP_QUANTITY=2` (canary — step to 10 once the
> confirmed-only edge shows live)**, account `live:schwab_1m_v2`, `go_live=true`.
> - **Rules:** ENTRY — on an ATR **BUY flip**, wait 3 bars, enter on the first later bar whose HIGH breaks the max-high of
>   those 3 bars (a SELL flip before the break cancels). EXIT — full close at **+2% target** OR **−5% hard stop** OR a
>   **bar-close-confirmed ATR flip** (bar closes below the trail).
> - **⭐ AMENDED 2026-07-14 ~21:30 ET (#456, live):** **RECLAIM is OFF** (`cw_v2_reclaim_enabled=false` ⇒ **1 entry per
>   BUY-flip segment**, not 2; code retained + inert) and the **ENTRY WINDOW is 7:00 AM–4:30 PM ET** (was 7–18). The
>   **OMS exit gate stays 7–20 on purpose** so exits outlive entries (a 16:29 entry must still be exitable). Backtest
>   07-09..07-14: the reclaim cut is worth **~+$20/4d** (90→50 trades, win 65%→74%, hardstops 26→8); the 16:30 window looked like a
>   **no-op in the backtest but is NOT** — live really entered 17:02 + 17:45 on 07-14 (harness under-models
>   after-hours), so it is a justified guardrail. **Live winners are FINE (median +2.27%); the −5% STOP is the leak.** See 2026-07-14 Recent Activity. [[project_mai_tai_v2_reclaim_off_and_window_1630]]
> - **Single kill switch** = `strategy_schwab_1m_v2_confirmed_window_enabled` (read by BOTH strategy entry + OMS exit so
>   they can't diverge). **Rollback = flag `false` + restart (byte-identical off).** Tunables `oms_v2_cw_target_pct=2.0`,
>   `oms_v2_cw_hard_stop_pct=5.0`. Env backup `/etc/project-mai-tai/project-mai-tai.env.bak.precw-*`.
> - **PRs (merged to main):** #408 entry · #409 exit price legs · #411 bar-close flip (Route C: strategy emits
>   `v2_cw_flip` → OMS in-memory `_cw_flip_pending` → managed close) · #413 makes CW exclusive with the old on_quote
>   hold-confirm path (dual-entry bug caught in pre-flight).
> - **Validation gate = the LIVE forward test** (`docs/atr-confirmed-window-forward-test.md`, pre-committed stopping rule:
>   30 name-days; kill if median negative OR flip-exit avg worse than −5% OR win-rate below payoff-implied breakeven).
>   The backtest can't reach the confirmed-only universe historically (scanner-confirmed set captured only since ~07-09).
>   **Honesty caveats:** v2 fills are IDEALIZED (`reference_price`, no entry slippage) → live CW looks BETTER than the
>   honest backtest — watch flip-exit fills for real slippage; the broad 10-day research was −1.28%/trade (diluted by
>   non-confirmed names), confirmed-only 07-09 was +1.68%.
>
> **This retires the old "Path-B leak / ATR-edge profitability" open item — there is no Path-B to decide anymore.**

---

## 🚦 STATUS (HISTORY) — v2 IS LIVE (2026-06-17, ATR-only, real Schwab account)

v2 went **live-credentialed** on **2026-06-17** as a **reasoned, operator-accepted risk** (profitability-after-spread
was/is still accumulating — see open items). Running config, ground-truthed from `/proc/<pid>/environ` + DB on deploy:

- **`broker_provider=schwab`, `account_name=live:schwab_1m_v2`**, real shared hash bound (the only `live:` Schwab key);
  `go_live_enabled=true`, `atr_only_mode=true` (P1/P2 disabled at two layers), qty 10, ATR fresh-flip qualifier on (age<5).
- **CYN is PROTECTED** — `MAI_TAI_PROTECTED_SYMBOLS=CYN` → `protected_symbol_set={CYN}` in the running config; the real
  account **holds 8000 sh CYN @ $2.57** (operator's manual position). 3-layer block + watchlist exclusion + #326. v2
  has never emitted/ordered/filled CYN (verified). `oms_managed_positions` CYN rows = 0 (bot does not manage it).
- **Rollback (tested):** `systemctl stop project-mai-tai-schwab-1m-v2.service` halts new entries instantly (OMS +
  market-data keep managing exits). Re-isolate to paper = `GO_LIVE_ENABLED=false` + `BROKER_PROVIDER=simulated` + restart.
  Env backup: `/etc/project-mai-tai/project-mai-tai.env.bak.pre-golive.20260617T003247Z`.

**What "live" has and hasn't proven yet:** the execution path is proven **to Schwab acceptance** (06-17: LNAI order
accepted by Schwab, working order, broker_order_id assigned). It is **NOT yet proven to a real FILL** — see open items.

---

---

## Older LIVE OPS heads (superseded; kept for history)

## 🟢 LIVE OPS STATE (2026-07-28 EOD head below; older heads kept for history)

- **2026-07-28 EOD head — SIX deploys today, fleet FLAT at every one.** HEAD `b9fd715`.
  PIDs: oms **1725295** · schwab-1m-v2 **1736517** · strategy **1733721** — all NRestarts=0, 0 errors,
  real shares held **NONE**, non-terminal open intents **0**.
  **Deployed today:** #580 resting-order orphan (15:15 ET, attended, fleet flat) · #582 scanner CONFIRM
  timestamp · #583 `cw_entry_n` off-by-one · #585 exit-capture hardening (bounded 429 retry + multi-exit
  key + close-log labels) · #587/#588 liquidity-floor coverage + default alignment · #590 cooldown
  REMOVED · #592 backtest-vs-live config parity. Reverted: **#579 reverts #578** (never deployed).
  **Live flags now:**
  `..._ATR_FLIP_VOL_FLOOR=10000` · `..._CW_V2_RECLAIM_ENABLED=true` · `..._CW_V2_RECLAIM_GAP_BARS=1` ·
  `..._CW_V2_RESTING_ENTRY_ENABLED=true` · `..._CW_V2_EH_RESTING_ENTRY_ENABLED=true` ·
  `OMS_V2_EH_ENTRY_ENABLED=true` · `..._DUAL_BROKER_FANOUT_ENABLED=true` (`WEBULL_FANOUT_QUANTITY=1`) ·
  `OMS_NATIVE_OCO_EXIT_POLL_ENABLED=true` **(NEW)** · `OMS_RECORD_NATIVE_OCO_EXIT_FILLS_ENABLED=true` ·
  `WEBULL_BRACKET_REALIGN_ON_FILL_ENABLED=false` **(turned OFF — broken at the broker)** ·
  `ORB_ENABLED=true` (qty 10).
  **Live results today:** 10 round trips, **median +1.81%, 7/10 wins** (INLF ×6, EGG ×2, STKH, CNET).
  ⚠️ **Entry behaviour changed late in the day** — the liquidity floor now gates the three live paths,
  so expect FEWER entries tomorrow. That is intended; watch the open.

### older heads (history)

- **2026-07-16 EOD head PIDs (fleet was stopped 10:02 ET for the deploy window; bots inactive at EOD):** oms **323327** (#477/#478 v2 overnight-flatten + retry-fix) · schwab-1m-v2 **304206** (#475 P1.3+P1.4; `[V2-BOOT-HOLD] released — 0 reconstructed-uncapped`) · orb **304293** (#475, untouched since). **Deploys today:** #475 (P1.3+P1.4 armed-segment safety), #477/#478 (v2 19:55 flatten), **B v2-overnight-naked backstop** (#479 + exec-bit #480; 20:05 ET ground-truth cron). **Merged to main (docs/CI, no restart):** #481 (2.4 docstring 10:00 · 2.5 default auto-merge DISABLED · this handoff). **Flags:** `MAI_TAI_OMS_V2_OVERNIGHT_FLATTEN_ENABLED=true`, `MAI_TAI_ORB_WINDOW_FLATTEN_ENABLED=true` (10:00 cap). Protected: **CYN, CELZ**. **⚠ Schwab refresh_token expires Mon 2026-07-21 07:43 ET (~5 days).** v2 took ZERO real positions (both CW emits Schwab API-open REJECTED); RUBI (ORB) was the only real-money trade (+2.57%). Group 1 + Group 2 CLOSED; the ENTRY is the sole remaining v2 lever (exit optimal on 3 instruments).
- **2026-07-14 EOD head PIDs (after today's 5 deploys, fleet FLAT):** oms **35087** (06:51 ET, #446 window+churn) · schwab-1m-v2 **35100** (06:51 ET, #446; CW-v2 intrabar **qty 2**, entry window **7 AM–6 PM ET**) · orb **64822** (11:46 ET, #450 — **resting stop-buy ENABLED**, trail **5%**, qty 2, running-high+resting) · control **44840** (08:20 ET, #448 token-expiry warning + cron) · strategy **4188365** · reconciler **3631771** · market-data **3631761** — all NRestarts=0, 0 tracebacks, OMS+v2 heartbeats healthy/flowing. Protected: **CYN** (5000 sh live:schwab_1m_v2), **CELZ**. **New live config today:** v2 entry gate 7–18 ET + OMS fillable-exit gate 7–20 ET (`MARKET_CLOSED` abandon); `MAI_TAI_ORB_RESTING_ENTRY_ENABLED=true`, `MAI_TAI_ORB_RECLAIM_TRAIL_PCT=5.0`; Schwab token warning cron (`2 12,13,22,23 * * *`) + seeded `refresh_token_expires_at=2026-07-21T11:43Z`. Env baks: `.bak.pre-orb-resting-trail5.*`. **Manual holdings note:** operator manually closed the stuck AGEN/SOBR after-hours legs 07-14 ~07:01 ET (reconcile clean since). See 2026-07-14 Recent Activity.
- **2026-07-13 EVENING head PIDs (after the v2 re-activation restart, ~18:52 ET, fleet FLAT):** oms **4188310** (deploys #441 v2-exit phantom reconcile), strategy **4188365**, schwab-1m-v2 **4188364** (deploys #440 CW-v2 reclaim fix; CW-v2 intrabar **qty 2**, ACTIVE), orb **4188363** (**qty 2** via `MAI_TAI_ORB_RECLAIM_QUANTITY=2`; resting entry flag **OFF** — reactive path unchanged), market-data 3631761 — all NRestarts=0, 0 tracebacks, OMS + v2 heartbeats healthy. Protected: **CYN** (5000 sh on live:schwab_1m_v2, frozen), **CELZ**. OMS exit path carries all fixes: #436 (reverse-conflict / 40-char coid / phantom reconcile) + #438 (native-guard re-arm queue) + **#441 (v2 CW-exit phantom reconcile — same class as ORB Bug C, `_v2_close_reconcile_flat`)**. See 2026-07-13 Recent Activity. [[project_mai_tai_oms_orb_exit_fixes]] [[project_mai_tai_v2_cw_v2_fixes_and_stopped]] *(Earlier 07-13 heads: #436 restart ~10:14 ET oms 4132235; #438 Bug-A restart ~10:55 ET oms 4136520 / orb 4136537 / v2 4136538 / strategy 4136539.)*
- **2026-07-10 v2 CONFIRMED-WINDOW deploy (~00:07 ET, attended, market closed, fleet flat):** v2 now runs the **CW ruleset
  live at canary qty 2** on HEAD `b94ba7d` (full config + rules in the STATUS block above). Kill switch
  `strategy_schwab_1m_v2_confirmed_window_enabled=true`. CW code spans **both** the strategy (entry) and the OMS (exit
  legs #409/#411/#413), so both were on the new HEAD at deploy. **Protected still CYN, CELZ.** ⚠️ **v2 (and OMS) PIDs
  after this deploy are NOT captured in this handoff — reconfirm via `systemctl show <svc> -p MainPID --value`** (the
  07-07/07-08 PIDs below predate the CW restart). First-session ntfy watch armed (remove that cron after the first session). OMS **3553602** (F2; **NOT touched by PR-E**). v2 **3558374** + ORB **3558545** (restarted for PR-E DB timeouts, FAST profile; fleet-flat one-at-a-time, 0 tracebacks, heartbeat/state advancing). strategy **3558009** · reconciler **3557982** · control **3557990** · market-capture **3557971** · trade-coach **3557960** (all PR-E, SLOW profile). Protected: **CYN, CELZ**. Fleet FLAT at every deploy moment today. DB migration head = `20260707_0011`. **Fleet-wide DB-hang hardening COMPLETE** (OMS #391 + all non-OMS PR-E). SPOF track CLOSED (Option C); F2 restart-safety LIVE (verdict pending next organic ORB fill). Watchdog + readiness crons armed. *(Earlier today: OMS 3544872 #393 PR-A 07:33 ET; OMS 3553602 #394 F2 08:53 ET.)*
- **2026-07-02 head PIDs:** OMS **3215039** (restarted ~10:12 ET after the 2nd zombie — **🔴 SPOF re-hang is RECURRING (2× in 12h); until fix #1 ships, if the fleet goes order-quiet mid-session check `oms-risk` heartbeat FIRST — likely re-zombied**). ORB **3163866** (#389 DB-reconcile, PROVEN live). v2 **3146429** (`protected_set=CYN,CELZ`). Protected: **CYN, CELZ** (CANF tradeable by ORB+v2). Fleet FLAT (0 open `oms_managed_positions`).

- **2026-06-23 evening deploy (attended):** **v2 restarted → PID 2668268** (#362 EH-routing LIVE — *supersedes the v2=2319110 line below*). **ORB restarted → PID 2667440** (reclaim shadow). **⚠️ strategy-engine NOT restarted (still 2415361)** — its box disk code (main `e76d8b5`, incl. #362's byte-identical leaf import + #363) is AHEAD of runtime; the **next strategy-engine restart deploys #362/#363 — do it attended.** OMS untouched (#362 doesn't change it).
- **#366 snapshot-persist throttle (#350 piece 1) — NOT deployed:** built, flag-gated default-off; awaiting **ATTENDED close-deploy** (`snapshot_persist_throttle_secs`>0 + re-arm the #350 py-spy capture at a 16:00 ET close → confirm gaps <50s).
- **🟢 ORB = LIVE real-money → PID 2825677** (restarted 2026-06-25 14:22 ET for the Piece-1 deploy; was 2765863). Running-high mode, `live:orb`→**webull margin** (D4GUJ…), **qty 5** (CORRECTED 06-25: running-high path uses `orb_reclaim_quantity=5`, NOT `MAI_TAI_ORB_QUANTITY=10` which only applies to the inactive classic-OR path — my earlier "keep 10" note was wrong; live size is 5), 3% trail, 9:30–10:00 ET window, 1.5% gap-cap, **OMS-quote-priced entry flag ON (Piece 1, see Open Items)**. Plumbing proven green (buy→`[HARD-STOP ARMED]`→sell→`[HARD-STOP CLEARED]`→flat on real AZI fills). ⚠️ **restart-while-holding UNTESTED — don't restart OMS while ORB holds.** Dashboard shows ORB provider "alpaca" (display-only `active_broker_providers` cosmetic; routing is webull). **OMS → PID 2825688** (restarted 14:22 ET with ORB for the Piece-1 cross-process flag; flat, 0 tracebacks). *(Prior OMS PIDs: 2801063 premarket no-op; 2765200 had the 4 Webull fixes #374–#377 + #373.)*
- **⚠️ ORB heartbeat caveat (running-high mode):** `bar_counts` counts **classic-OR bars only** → stays **0 all day** in running-high mode; pre-09:25 ET state is dropped by design (the running-high observe anchor is 09:25), so **empty `bar_counts`/`last_tick_at` + "waiting for Polygon market data" placeholders premarket are EXPECTED, NOT the 1970-bug** (`_normalize_trade_ts_ns` fix confirmed in running code). The real open-time signals are **`last_tick_at`** populating + decision status `building_or`→`watching`→`entered`.
- **🟢 FCUV manual-position conflict — VERIFIED SAFE (06-25, do NOT protect).** `live:orb` (webull) holds **400 sh FCUV @ $6.87** (operator's MANUAL position; no ORB order created it) and FCUV is on ORB's watchlist. Operator trades FCUV by hand and chose to leave it **unprotected/tradeable** (`MAI_TAI_PROTECTED_SYMBOLS=CYN` only). **Code-verified the OMS will NOT touch it:** `oms_managed_positions` has a single writer gated to `schwab_1m_v2` only; ORB exits run off the OMS native hard-stop, which arms **only** on a fill from an intent ORB emitted (`_armed_hard_stops[key]` must pre-exist) — armed stops are in-memory, empty on restart, re-armed only from new bot fills. Reconciler will emit a benign position-mismatch finding for FCUV (like CYN). If ORB enters FCUV today it adds its own qty-10 managed leg; its exit sells only the managed qty, leaving the manual 400.
- **CYN 8000 sh** still held on `live:schwab_1m_v2` (protected/frozen/inert).
- **2026-06-19 Deploy Main (Juneteenth holiday override) rotated all 5 CORE PIDs** — strategy **2415361** + OMS / control /
  market-data / reconciler (all `since` ~13:29Z, NRestarts=0). **v2 UNCHANGED = 2319110 (untouched, still current).**
  polygon_30s flipped to `paper:polygon_30s` + `simulated`, `MAI_TAI_STRATEGY_PERSIST_OFFLOAD_ENABLED=true` ACTIVE. The
  offload path validates Mon premarket (closed market = no bars yet). Re-fetch any PID via `systemctl show <svc> -p MainPID --value`.
- **Service PIDs (06-17 set):** v2 **2319110** still current (#335 TIMESALE, flag OFF/inert); strategy 2299529 / OMS 2299517
  (#333) **RETIRED by the 06-19 deploy → now 2415361 etc.** *(Retired earlier: v2 2252021 [#326], OMS 2207792 / strategy 2207786 [#333], pre-go-live 2104716/2121312.)*
- **#326 — Schwab-ineligible watchlist eviction: DEPLOYED + restart-verified 2026-06-17.** v2 now evicts symbols Schwab
  refused to open today (`schwab_ineligible_today`, per-account, 60s-cached) from its watchlist, so it stops *emitting*
  for them (the OMS already blocked *re-submission*; this halts the bot at the source — parity with the old schwab_1m
  bot). Proven on the fresh boot: scanner confirmed 6, v2 watchlist = 3 (CLWT/EHGO/YMAT evicted = exactly today's
  ineligible set). ⚠️ **Known ≤60s stale-carryover window at the 04:00 roll** (cache TTL not coordinated with session
  roll) — benign (over-conservative, self-corrects, 3h pre-trade); optional hardening = key the cache on session_date.
- **Mid-session RESTART recovery (FLAT) — measured 2026-06-17:** WS re-subscribe **~4s**; `state.bars` hydrated via
  DB-seed **~2s** (Fix-b) + REST warmup **~17s** (all `warmed=3/3`); buffered streamer bars drained. **Effectively blind
  ~17s, NOT the old ~135-min blackout** — DB-seed + REST warmup backfill the strategy buffer. (Supersedes the 135-min
  worst-case in [[project-mai-tai-v2-entry-warmup-gate]] for the DB-history case.) **Note:** the snapshot `bar_counts`
  telemetry resets to live-only on restart (≠ the eval buffer `state.bars`, which is the warm one).
- **Forward-test watcher** `/tmp/atr_fwd_watch.py` → `/tmp/atr_fwd.log` (flags any live fire age≥5 as GATE-BROKEN).
- **Go-live confirm captures (VPS):** `/tmp/v2_golive_cp1.txt` (04:00 roll), `/tmp/v2_golive_cp2.txt` (7AM session),
  `/tmp/v2_golive_firstfill.txt` (first-fill watch; transient timers `v2-golive-cp{1,2}`, watch fired + exited).
- **Tick-capture retention:** prune-ticks `--keep-days 30`; first effective deletion ~2026-07-15; `market_*_ticks` only.
- **Deploy discipline:** PR + Validate mandatory (CI `validate` GREEN again — open item #2; admin-merge still available),
  direct push forbidden; attended + explicit-GO before any live-money merge/restart; restart ONLY named services + capture PIDs.
  See [[project-mai-tai-multi-agent-deploy-rules]], [`vps-deployment.md`](vps-deployment.md).

---

