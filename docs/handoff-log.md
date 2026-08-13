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

## 2026-08-13 — the close was fighting our own exit legs

**Seven PRs merged and deployed (`3ac4721`), both new flags ON at the operator's explicit
direction.** The day started on "make the Webull leg rest at the broker like Schwab's does" and
ended somewhere more interesting.

### What we set out to do

The operator had been saying the same thing for two days: *a limit order will not fill in a fast
market; I want a REAL resting order at Webull, sitting there waiting, like Schwab's.* #688 (merged
earlier today) did that. But a mirrored Webull rest fills **bare** — Webull 417s a stop-limit master
carrying a bracket — so #689 attaches a target+stop pair seconds after the fill, using the
no-master `[STOP_PROFIT, STOP_LOSS]` shape Probe W4 proved at HTTP 200.

### Then the operator asked about the 58 rejects

`live:orb` took 58 rejects today against 24 fills. Every one was the same thing, and it was ours:

> **A resting exit leg RESERVES the position.** The software ladder then sells the same shares,
> Webull sees available-to-sell = 0, and refuses it as a naked short.

56 of the 58 were **one XHG share**. 48 of those inside five minutes — a single share drawing a
rejected market sell roughly every six seconds while its own OCO leg sat there working.

**The asymmetry is a missing capability, not a broker bug.** `-close-` filled 4/62 at Webull vs 5/6
at Schwab. Schwab stands the ladder down while a bracket is armed; Webull exposes no
`fetch_armed_native_oco_symbols`, and `routing.py` *fails open by design*, so the ladder fires into
its own reservation. Nothing detected it either: the `= 8` abandon bound resets on any
positively-HELD read, and we genuinely **do** hold the share — it is merely reserved — so the bound
is unreachable. No marker, no surviving counter, no alert.

**This retro-explains 08-12 CRWU** — yesterday's "held position with nothing trying to sell it" had
the same two reject strings. It was never a mystery.

### The discipline that mattered most today

**The count screamed and the money shrugged.** Before proposing anything I priced it: bid at the
first blocked attempt vs the price actually taken, n=5. Better in 2, **worse in 3**, median
**−0.51 pp**. Every position exited via its OCO leg. Had I skipped that step I would have sold a
fix for a problem that was costing roughly nothing — the vol-floor-flap lesson, again.

### Wrong turns, recorded

- **I wiped my own implementation with `git checkout` during a mutation run — for the second time.**
  Same mistake as 08-12. It is now a memory: **commit before you mutate.**
- **Two mutations "survived" and both were my error, not weak tests.** One removed only a sleep
  rather than the retry loop; one targeted adapter code the test replaces with a double. A third
  survived *legitimately* and found a real gap: the attach was minting its base coid with a uuid and
  **throwing it away**, so the pair it places could never have been cancelled. Now pinned.
- **I branched three PRs off a pre-squash branch.** Every one came back `DIRTY` with no CI. Rebasing
  onto `origin/main` fixed it each time, but it cost three round trips.
- **My first "is it flat?" query printed a clean result while erroring.** stderr buffered after
  stdout, so `--- (nothing above = flat)` appeared under a query that never ran. A false clean is
  the exact shape we have a memory about; caught only because the column names looked wrong.

### The deploy fought back twice

`refusing deploy because repo has local changes` — `bar_gap_watch_cron.sh` and
`reconcile_alert_cron.sh` were 100644 in git and had been chmod'd by hand on the box. The obvious
unblock (`git checkout` them) was the **wrong** move: root's crontab invokes both directly by path,
so reverting to 100644 would have silently stopped the v2 bar-hole watch and the reconciler drift
alarm. Committed the exec bit instead (#693), then verified both files were still executable after
the pull. `schwab_token_expiry_cron.sh` is also 100644 but runs from a separate `/home/trader/`
copy — deliberately left alone rather than "fixed".

The guard also runs *before* the pull, so the box could never reach the fix that would clean it;
the checkout had to be reconciled by hand once.

### #691's own hazard, and why #692 exists

Cancelling the resting legs to clear the way for a close **removes the net**. Before, a close that
kept failing was survivable — the OCO legs stayed put and took the position out at +2%/−5%. After,
they are gone, so a persistently failing close leaves the position naked. #692 re-attaches
protection after 3 refused closes **on a positively-HELD read only** (an inconclusive one could
place a pair against shares we no longer own — the oversell shape). Without #692, #691 would have
been strictly worse than the storm it replaced.

### Deployed

`3ac4721`, 17:35 ET, OMS + strategy + schwab-1m-v2. **No bar hole** — verified with a per-minute gap
query rather than by eyeballing the newest bar. Both flags confirmed from each process's own
`/proc/<pid>/environ`. A 1209-line error count in `oms.log` looked alarming and was not this deploy:
the Webull 429/417 storms are stamped 19:45 UTC, hours earlier, and the only ongoing errors are
Alpaca at a flat 12/min **before and after** the restart.

**Both flags are UNEXERCISED.** The account went flat before the deploy and stayed flat to the
close. Tomorrow is the first real test.

---

## 2026-08-11 EVENING — the fill price was there all along

**After the EOD wrap.** FRTT protection reverted, two probes/scripts built, and a claim of mine
withdrawn.

### FRTT: protected 16:08, unprotected 20:11 — same evening
Protected at the operator's direction after he manually bought 5,000 FRTT while the bot traded the
same name. He closed it into the bell, so the reason evaporated. Reverted at 20:11 in the window we
had originally wanted: EH closed at 20:00, account flat, none armed, newest bar 19:59 — **no bars
were due, so no hole was possible.** 0 tracebacks. Verified from each process's own
`/proc/<pid>/environ`, not from the env file.

### ⭐⭐ The correction that matters: `fills.price` exists, 100% coverage
Earlier today I reported that **"we never store a broker fill price anywhere"**, escalated it to a
board item, and said it *blocked* #676's acceptance and made *every stop and target wrong by the
slippage*. That was **wrong**. I had checked `broker_orders.payload`, found no fill-price key, and
generalised from one table to the database.

`fills.price` is the broker execution price — populated by **every** adapter via
`ExecutionReport.fill_price`, persisted at `oms/store.py:629`, **schwab 168/168 and orb 302/302 over
11 days**. The operator's chart marker `-2@1.535` matched `fills.price = 1.53500000` exactly. The
value was one JOIN away the whole time.

⇒ **No code change was needed. The query was missing, not the data.** When the operator asked
whether to build storage, the right answer was "it is already stored" — arrived at only by grepping
the WRITE path (`fill_price=` → who consumes it) rather than trusting the earlier conclusion.

### The validation script, and two defects it caught in itself
`scripts/resting_entry_slippage.py` (#682). Both defects were found by RUNNING it, not by reading it:

1. **Tick rounding.** The log prints `stop=1.3742`; the order stores `1.37`. Exact matching missed
   **48 of 48** resting fills — and the script still printed confident, well-formatted numbers with
   zero attribution.
2. ⛔ **Silent-empty.** `except (OSError, PermissionError): continue` swallowed the fact that v2 logs
   are `root:root 640`. Run as `trader`, every log was unreadable, the slot index came back EMPTY,
   and the output was indistinguishable from "no placements found". This is the exact failure class
   boarded this morning, committed by me four hours later.

Both are now guarded: a key ladder at raw/2dp/4dp precision, and loud lines for unreadable files
AND an empty index, stating it is a **tool failure, not a finding about the strategy**.

### ⭐ #676 has its first priced fill — and it corrected me again
```
reclaim  n=1   median -35.2bps        <- filled BETTER than the decided level
first    n=3   median -20.4bps  worst  +12.6
market   n=34  median  -0.4bps  worst +116.3  SD 36.6
```
Earlier I said **no `slot=reclaim` order had ever filled**. That came from arithmetic on cancel
reasons; the direct join found one. ⚠️ n=1 on one symbol is not a result — the script refuses to
print a drop-one when only one name is present.

⛔ Also withdrawn: my claim that the 0.50% band caps slippage at +50bps. It caps the fill against the
**stop trigger**, not `reference_price`, and the same output shows **+58.3**. What the band does
guarantee is that the unbounded market tail (+351.7) is impossible.

### Probe W (#681)
Preview-first probe for whether a Webull combo MASTER can be a STOP_LIMIT — the guard at
`webull.py` refuses it client-side and has **never asked the broker**. The operator's manual bracket
screenshot (plain LIMIT + SL/TP legs) is *consistent* with the restriction being real; the note now
reads UNPROVEN-BUT-PLAUSIBLE rather than confirmed, because the UI not offering it is not proof the
API refuses it. Session stamped beside every result; cancel-and-verify in a `finally:` with a final
sweep that exits 3 if anything survives.

### Tally for the day
Five mechanisms proposed and abandoned, plus this fill-price claim withdrawn. Every one died on
reading the actual tape, call sites, or write path. The ones that survived came from `grep`, not
from reasoning about how the system ought to behave.

---

## 2026-08-11 — three wrong mechanisms, one real root cause, and a deploy that came out clean

**Shipped:** `cb30fcd → 32926b6`. **#678** held-symbol exit coverage · **#679** orphan-watch
ownership + oversell · env `MAI_TAI_PROTECTED_SYMBOLS=CYN,TE → CYN,TE,FRTT`. Suite 1996/0, ruff
clean, 0 tracebacks, no bar hole.

### The day: an operator screenshot, not an alarm, started it
The operator opened a TOS ladder at ~15:15 ET and saw **four `-2` sell orders against a +2 FRTT
position**, plus a `+2 STPLMT` buy from 13:00 still working. Our DB showed **one** working order.
That gap — 5 at the broker, 1 in our records — drove the whole afternoon.

### ⛔ Three mechanisms were proposed and each died on reading the data
1. *"the bot re-enters while holding"* — FALSE. Last resting placement 14:54, **ten minutes before**
   the 15:04 fill. The held gate works.
2. *"the position-held gate clears `resting_active` without cancelling"* — that code does read that
   way, but the timeline rules it out: we were FLAT at 13:00.
3. *"the orphan watch is pointed at the paper account and is blind"* — **asserted twice, FALSE both
   times.** The env sets `live:schwab_1m_v2`; the hand-run failed only because the service env was
   not loaded. The cron wrapper loads it via `systemd-run -p EnvironmentFile=`.

### 🔴 The actual root cause
```
13:00:03  accepted   resting buy-stop placed (slot=reclaim, stop 1.5200)
13:01:02  rejected   THE CANCEL FAILED — "upstream connect error or disconnect/reset before
                     headers. reset reason: connection termination"
15:00:01  RED        the orphan watch's stale-trigger heuristic fires (13% away, 120min)
15:17:41  cancelled  by the OPERATOR, by hand — 136.6 minutes later
```
**A cancel is fire-and-forget.** We emit it, clear our own state as though it worked, and never
verify. 11-day census: exactly **2** rejected cancels — rare, and unbounded in consequence.

### ⭐ The watch is a PASS — record it as one
`ORPHAN ORDER RED - 15:00 ET` was classified, pushed **and received on the phone**. First alarm this
week to catch something real and deliver it end to end. Its only limit is that it is a heuristic on
PRICE DISTANCE, so it could not fire until price had drifted 13% — 120 min late. #679 adds
`classify_unowned`, which asks *does anything OWN this order?* — a fact about our records — and on
the same tape fires at **13:03**.

### What else the day produced
- **#676 is EXERCISED** — 21 `slot=reclaim` placements. The irony: the FRTT order that consumed the
  afternoon **was** a #676 reclaim rest. The `slot=` field earned its keep; the bare
  `[V2-RESTING-PLACE]` marker fired 201× yesterday on the OLD path and would have read as a pass.
- **The resting-cancel split** (5 sessions, n=655): reprice 58.2% · liquidity_floor 35.4% ·
  **flip_no_fill 6.0%** · window_closed 0.5%. The earlier "76% never fill" headline was per-ORDER
  over an opportunity-level question. ⚠️ A DB test using a 30s successor threshold returned zero and
  briefly read as "no reprices at all" — wrong, because bars are 60s. Threshold shorter than the
  mechanism's period ⇒ false negative.
- **A 2h22m FLEET-WIDE entry suppression** (04:38→07:01 ET) from a post-boot promotion whose stale
  warmup series held `just_warmed=False`, so the seed-cap never ran. Largest measured loss of the
  day. Two wrong mechanisms were boarded for this one too before the call sites were read.
- **The operator manually bought 5,000 FRTT** at ~15:40 and closed it into the bell. FRTT was
  protected at his direction; the reason evaporated before the deploy ran, and the revert is one
  line.

### Deploy-window note, recorded deliberately
Deployed at **16:08 ET with EH still open**, on the operator's explicit call, with MSGY armed-but-
flat accepted. It came out clean — **no bar hole, 0 tracebacks**. That is one clean sample, not
evidence the 20:15 quiet window is unnecessary.

### Lesson boarded
**Before scoping a fix as N parts, check which parts already work** — #678 was reported as four
broken behaviours and turned out to be one broken INPUT (the subscription); the three exit rules
and the resting-cancel were correct all along. Third instance this week. The check cuts both ways;
the value is knowing which, not expecting it to shrink.


---


## 2026-08-10 (Mon) — three PRs deployed; five claims of mine withdrawn

**Deployed 20:21 ET `ca0cf92 -> cb30fcd`** (#672 -> #674 -> #676). Gate: **GO BY OPERATOR OVERRIDE,
exit 0** — one block, one documented token, complete audit trail. First run where the mechanism
worked as designed; Friday and today's 16:08 run both bypassed tokenless blocks.

**Shipped.** #672 — P0a census `submitted=N` denominator, `[OMS-INTENT-DROPPED]` on both broker
short-circuits, `flip_no_fill_soft_rest`. #674 — RTH reactive = band-capped marketable LIMIT
(**a STOPGAP**, superseded by #676; decide deliberately whether the cap stays) + `[V2-CW-RULE7-BLOCK]`.
#676 — the RECLAIM entry RESTS at `cw_segment_high` instead of chasing the break.

**Validations.** #663 COMPLETE (both drivers). #666 **PASSED** at 16:00 — `[V2-RESTING-CANCEL]
reason=window_closed` x2, zero old-symptom lines, 0 live orders after. D1a #657 validated **08-08**
(not 08-10). #668 and #664 remain **UNEXERCISED**, labelled as such.

**Measured.** Broker-vs-broker fills: median signed **0.0 bps**, 18/16/4 — scatter, not bias. The
real finding is the **order-type asymmetry** (`rth_resting`: Schwab STOP_LIMIT vs Webull MARKET) and
that market orders own **all 8 entries >=200 bps**. P0a is **structurally unreachable** (27/27 exits
fill inside one 15s sync tick). Vol-floor flap: 279 cancels/7d but only **4** crossed a level the
segment still wanted. Fan-out is **not** sequential-fallback — 87 pairs where Schwab FILLED and
Webull fired anyway.

**⛔ FIVE CLAIMS OF MINE WITHDRAWN TODAY, ALL IN THE SAME DIRECTION.**
"the roll path disarms silently" (`armed=0` was on the line I quoted) · "repricing halves the fill
rate" (0 of 17 were churn; 13 never rested at all) · "rule 7 is a capability wall" (binds on <=1.3%) ·
"`limit_price=0`, 83 rejects" (zero since 07-24, closed by #547) · "a second resting slot is needed"
(the code has enforced mutual exclusion all along). ⇒ **"There's a defect here" reads as diligence
and costs a build; "this is fine" costs nothing when it's right.** The felt asymmetry is inverted
from the real one — that is why it kept happening.

**⭐ Rules earned today.** An absence is evidence only against a known denominator · never size a
defect without its date range · an instrument that counts observations without counting
opportunities is unreadable alone · the tool's status is not the thing's status · dead guards
dominated by an earlier return (3 in 4h, only mutation finds them).

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



---

## 📦 Resolved / superseded OPEN ITEMS, moved here 2026-07-29

> Moved verbatim out of `handoff-open-items.md` when it was pruned with the operator.
> 47 items: 22 already self-marked closed, 7 verified stale against live state
> (v2 overnight-flatten live · ORB resting entry OFF · token cron live · P1.3 boot-hold
> shipped · vol-floor watch superseded · strategy-engine drift · reclaim-cooldown replaced
> by the per-segment cap), and the rest EOD summaries / priority stacks that are narrative,
> not open work. **Nothing was deleted.**

- **✅ 2026-07-20: #487 (account default live→paper) and #490 (token-refresh default True→False) MERGED —
  full suite green (1228 / 1229 passed), no split; the prediction held (no `get_settings()` reader touches
  those fields). The seam itself remains OPEN.** `strategy_macd_30s_enabled` (True→False) is still deferred —
  correct + live-inert but it DOES hit the split (68 tests); ships when the seam closes.

**🗓️ 2026-07-14 EOD SUMMARY (5 things shipped live today; full detail in Recent Activity below):**
1. **v2 trading-window + exit-churn fix (#446)** — v2 entries hard-capped **7 AM–6 PM ET**; OMS abandons unfillable `close` intents (`MARKET_CLOSED`) so exits don't churn overnight. Root-caused: NOT a recent regression — v2 (isolated bot) never had the 6 PM cutoff the shared bots enforce via `TradingConfig`; the overnight churn had fired unnoticed for 2+ wks (CLRO 07-02→03 = 3,002 cycles). Closes the long-standing "v2 EH exit routing" item.
2. **Schwab refresh-token expiry warning (#448)** — captures `refresh_token_expires_at` at re-auth + ntfy cron (AMBER≤48h/RED≤12h). Operator re-authed 07-14 ~07:43 ET; seeded expiry 2026-07-21. [[project_mai_tai_schwab_token_expiry_warning]]
3. **ORB resting stop-buy entry ENABLED (#450)** — plumbing gate passed (pre-market buy-STOP-LIMIT accepted + held through the 9:30 open + clean cancel). `MAI_TAI_ORB_RESTING_ENTRY_ENABLED=true`. **⏳ FIRST REAL fill = 2026-07-15 09:30.**
4. **ORB trail reconciled to 5% (#450)** — was silently 3% while logs said 8% (display bug, fixed); operator set 5% (`MAI_TAI_ORB_RECLAIM_TRAIL_PCT=5.0`).
5. **v2 CW-v2 first live WINS** — NXTC scalped twice on real Schwab fills (6.72→6.85 +1.9%, reclaim 6.94→7.05 +1.6%), both +2% target, 2/flip cap.

**🔜 NEXT SESSION — 2026-07-15 — WATCH (all deployed; nothing to build first):**

- **ORB resting-entry FIRST real trigger-and-fill @ 09:30 ET** — the test only proved place/persist/cancel (far-above orders can't fill). Watch `[ORB-OPEN] ... trail_pct=5.0`: does it fill AT the break vs gap through the limit? qty 2, young mechanism. Rollback = `MAI_TAI_ORB_RESTING_ENTRY_ENABLED=false` + ORB restart (env bak `.bak.pre-orb-resting-trail5.*`).

- **v2 entry-window / OMS churn fix** live-proof — confirm `[V2-ENTRY-WINDOW-BLOCK]` fires pre-7AM/post-6PM and no overnight `MARKET_CLOSED` churn on any held-past-8PM position.

- **Token warning** armed (no action) — AMBER fires ~day-5; real `refresh_token_expires_in` self-captures on the next re-auth (~07-21).

**🚦 2026-07-15 EOD STATE — READ THIS FIRST. 8 PRs, 4 deploys, 1 revert, 6 findings. Fleet FLAT.**
> **LIVE NOW:** oms **230687** · schwab-1m-v2 **230700** · orb **177630** · strategy 4188365. All NRestarts=0, 0 tracebacks.
> VPS = origin/main `f5cdd00`. **Nothing pending deploy.** Protected: CYN, CELZ.
>
> **⭐ THE ONE-LINE SUMMARY OF THE DAY: the instruments were wrong, not the bots.** Five times a number
> was wrong and the reasoning behind it was right. Three separate studies produced answers that
> collapsed under a drop-one or a unit change. **Before trusting any number, ask what instrument
> produced it and whether that instrument was checked against ground truth.**
>
> **DEPLOYED + LIVE:**
> - **#464 false-flat fix** — tri-state read + 120s fresh-fill grace. Closed the naked-position path on **ORB and v2**. Live-validated on ASTN 07-15 16:37 (`[RECONCILE-READ] FLAT_INFERRED (n=1)` → cleared, no churn = the *don't-over-correct* half). ⚠ The **grace itself is still unvalidated** — 26min is nowhere near the 0–120s window it governs.
> - **#459 `decided_at`** — self-validated live at 15:31 (marker logged 2.6s AFTER the decision it stamps).
> - **#468 settlement probe** — `[SETTLE-LAG]`/`[SETTLE-PENDING]`, per broker, rides the existing 5s poll. **Needs an ORB fill for WEBULL data** (the broker that broke). Schwab anchors came from ASTN/CPHI.
> - **#471 WINDOW FLATTEN — ON.** `MAI_TAI_ORB_WINDOW_FLATTEN_ENABLED=true` (env bak `.bak.pre-window-flatten.20260715T234124Z`). **ORB must be FLAT after 10:00 ET** — if it holds, the OMS closes it (guard cancelled first) and screams at error level if the close fails. **This is a RULE, not a safety net.**
> - **#465 CRLF + `.gitattributes`** — my Windows tooling made #464 a 9022-line unreviewable diff. Fixed at the root.
>
> **REVERTED:** **#467** (stale-trigger fix) → **#469**. NOT because a thin sample said it lost (that was one
> price-weighted name), but because it shipped on a claim of mine — *"byte-identical on a prompt break"* —
> that was **false**: 24 of 50 entries changed, including plain +0.15% prompt breaks. **The SOBR chase is
> knowingly LIVE again.** Its `[V2-CW-ORB-BLOCK]` log was KEPT (read-only, never in question).
>
> **⚠ TOMORROW 09:30 IS THE TEST:** ORB reactive path (resting OFF) · window flatten fires at 10:00 if ORB
> holds · `[V2-CW-ORB-BLOCK]` gives its first-ever number (how often the ORB blackout eats a v2 setup) ·
> settlement probe gets its **Webull** anchor on ORB's first fill.
>
> **STANDALONE DOCS (deep detail, `C:\Users\kkvkr\Downloads\`):** `open-issues-register-2026-07-15.md`
> (the prioritised board) · `v2-segment-state-bugs-2026-07-15.md` · `P0.1-reverse-conflict-CLOSED-2026-07-15.md` ·
> `P0.6-orb-overnight-naked-2026-07-15.md` · `P0.6-eod-flatten-design-2026-07-15.md` ·
> `P1.3-cap-reset-on-restart-2026-07-15.md`

**🔴 P0.6 — ORB OVERNIGHT-NAKED: FIXED 2026-07-15 (#471, ON). v2 IS NOT FIXED.** ORB held overnight
**3 times in 3 weeks** (ERNA 07-15, AGEN+LGPS 07-13) with **no protection at all**: the native broker STOP is
`time_in_force=day` **AND Webull stops are RTH-only** (none of 2137 ever terminated later than **15:16 ET**),
so it is gone by 16:00; the OMS software stop **cannot fill outside 7:00–20:00**. **All three were closed BY
HAND** — ORB has never once exited an overnight-bound position itself; **the operator noticing was the only
control.** ⭐ **The data made the fix obvious and my first design wrong:** no completed ORB trade has EVER
lasted >**5.0 min** (median <1 min) and every entry lands in the **first 8 minutes** (09:31–09:38) — so a
10:00 flatten clips **zero** winners. And the 3 survivors didn't *run*, their **exits broke** (ERNA's trail
fired **7×**, every close rejected). ⇒ holding past 10:00 = **broken exit**, wants a loud alarm NOW, not a
15:55 tidy-up. **⚠ v2 IS WORSE AND UNFIXED:** arms **zero** native stops at any hour, window runs to **16:30
(past the close)**, held **two** past the close on 07-15 (ASTN, CPHI — ASTN closed by hand). **Do not read
ORB's fix as covering v2.** [[project_mai_tai_orb_overnight_naked]]

**🔴 P1.3 — a v2 RESTART re-issues the per-segment entry cap. LIVE, fires on EVERY restart.**
`cw_entries_this_flip` is in-memory only; the DB seed rebuilds the segment from bars and the counter returns
**0**, silently re-issuing the 1-entry-per-segment allowance. **CPHI 07-15 proves it:** same `trig=1.4200`,
same `flip_level=1.1390`, **both entries `n=1`**, 6 seconds after a restart → −5% stop. **⚠ FLEET-FLAT DOES
NOT COVER THIS** — it checks *positions*; the cap resets on *armed segments*, which hold no position. And
**armed segments are unobservable** — nothing reports them, so the stricter rule ("don't restart v2 while a
segment is armed") is not merely unenforced, it is **unrunnable**. No operator error needed:
unattended-upgrades bounces the fleet. Fix shapes in the doc; **A (fail-closed on boot) is favoured** —
strictly safer, no schema, and forfeits ~0 entries given how rarely v2 restarts outside deploys.

**⭐ P4.1 REOPENED — the floor-ratchet numbers were price-weighted illusions.** Rerun in PERCENTAGES
(50 trades, 21 names, live frozen-trigger entry): **B (the operator's 1%-step ratchet) Δmedian = +0.0000pp**
— *zero* — and Δmean +0.02pp. **G (0.10% trail) Δmedian = +0.037pp** (~3.7bps/trade). Drop-one: nothing
flips. ⇒ **the dollar ordering survived but the magnitude was fiction**: `B−A=+$0.04` / `G−A=+$0.33` were
substantially a **VEEE readout** (VEEE $25–29 vs everything else $1–7 = **38.7% of the notional** off 4 of 21
names). **Verdict changes from "small but positive" to "measurably nothing."** The defer was right; the
reason was wrong. **HARD RULE NOW: the harness must report per-trade %, median-first, and refuse a bare
dollar total.** A discipline that failed twice in one day is not a discipline. *(Also owed: a sanity floor —
a `−99.99%` artifact poisons every mean on the 07-15 detail run.)*

**🟢 FALSE-FLAT RECONCILE — FIXED + DEPLOYED 2026-07-15 (PR #464 `ae2e909`, CI-green no-admin). Was a LIVE NAKED POSITION (ERNA, real money).** Design [`false-flat-reconcile-design.md`](false-flat-reconcile-design.md) (#463). **INCIDENT:** first live day of ORB's resting entry — buy-stop **FILLED 2 ERNA @ 9.47** 09:33:17 (real Webull fill `9KGME18JSJK753VLVQ780EBGSB:2`) → protective sell-STOP **rejected `ORDER_NOT_SUPPORT_REVERSE_OPTION`** → bid fell through the 9.196 trail, **3 closes failed** → `[HARD-STOP RECONCILE-FLAT] broker flat -> clearing phantom armed stop` **while we held 2 shares** → **NAKED 09:34:18**; `oms_armed_stops`+`virtual_positions` empty ⇒ **the OMS was then STRUCTURALLY INCAPABLE of closing it** (sell clamps to virtual_position=0 — the scoping invariant) ⇒ **operator closed by hand ≈−17.5% (−$3.32)**. Ground truth: a buy fill exists, **no sell fill exists**. **ROOT CAUSE** (`oms/service.py` `_broker_symbol_is_flat`): a bool with no way to say *"I don't know"* — symbol-absent / empty-list / None all fell through to FLAT, and both callers DELETE protection on FLAT. **It backed the v2 CW exit reconcile too**, so v2 was armed on it as well. **⭐⭐ THE UNIFYING ROOT CAUSE OF THE WHOLE DAY = WEBULL FILL-SETTLEMENT LAG.** One cause, two symptoms: an unsettled fill makes a protective sell look like it would *reverse* the book (→ `ORDER_NOT_SUPPORT_REVERSE_OPTION`, #436 Bug A) **and** makes the positions endpoint omit the position (→ the false flat). ERNA's stop triggered **61s after its own fill**. **FIX (deployed):** tri-state read — `FLAT_CONFIRMED` (symbol present @ qty 0) / `HELD` / `UNKNOWN` (raised or unparseable → **never clears**) / `FLAT_INFERRED` (absent or empty → **ambiguous**). ⚠️ **`[]` is deliberately NOT UNKNOWN**: brokers OMIT closed positions, so a genuine out-of-band close on a single-position account returns exactly `[]` — treating it as UNKNOWN would churn forever (the 07-13 AGEN 181× loop #436 Bug C fixed). It is also what a silent read failure looks like, and a ledger check is **circular** (the armed stop IS our belief, and it is what we are deciding to delete). **TIME is the only sound discriminator** ⇒ `oms_reconcile_fresh_fill_grace_secs=120` refuses an inferred flat while our fill is fresh, honours it after. `armed_at` is **in-memory only** (F2 rehydrate → None → no grace = correct: a restored stop is not fresh; no migration). **Fix 0 `[RECONCILE-READ]` logs every read** — today's cause could only be INFERRED because nothing recorded it; that log is what will tell us if 120s is the right number. **Trade: wrong "flat" = naked/unbounded; wrong "held" = bounded, noisy, visible churn.** 11 tests, ERNA replay = the anchor, all verified to fail with the guard disabled; #436 Bug C tests unchanged/green; 1184 unit green. Rollback `oms_reconcile_require_positive_flat=false` (reproduces pre-fix EXACTLY — incl. mapping a *raised* read to not-flat, which the old code got right; mapping UNKNOWN→flat would have made the lever MORE dangerous than what it restores). **DEPLOYED 11:14 ET** attended, fleet-flat, choreography stop-v2 → restart-OMS → start-v2: **OMS 81006→181406 · v2 122780→181424**, ORB untouched, NRestarts=0, 0 tracebacks, `[OMS-BOOT-PROTECTION] all 0`, `require_positive_flat=True/grace=120` confirmed live. [[project_mai_tai_false_flat_naked_position]]

**🔴 NEXT SESSION — BUG #2: `INTENT_MAX_AGE` (30s) KILLS RESTING STOP-ENTRIES. PINNED BY LIVE PROBE 2026-07-15, NOT BUILT.** **Proof (zero-risk, through the REAL intent path):** published one genuine ORB resting intent for **F qty 1, stop 21.46 = 50% above market (cannot fill)** → placed 11:23:30 → **`[OMS-ABANDON-INTENT] code=INTENT_MAX_AGE symbol=F intent_age_s=34.6`**. A resting order that physically cannot fill survives **34 seconds**. That is the exact kill-chain that suppressed **KUST/VIVS/SOBR** this morning (abandon → `[ORB-ENTRY-RESET]` → re-enter → attempt burned → **2 attempts in ~60s → suppressed for the day**; ERNA only filled because it broke *inside* the 30s). Probe: `/home/trader/probe_resting_intent.py` (publish `{"data": event.model_dump_json()}` to `mai_tai:strategy-intents` — the field is **`data`**, not `payload`). **⭐ THE AXIS IS *RESTING-LEVEL vs CHASE-PRICE*, NOT buy/sell.** `INTENT_MAX_AGE` is **ALREADY buy-only** — the gate requires `intent_type == "open"`; sells never reach it (they are covered by `MARKET_CLOSED`, #446). Empirically confirmed: **every INTENT_MAX_AGE in the logs is `side=buy` (3/3)**; sells have received **no** abandon code, ever. And **the exemption already exists for sells**: `_is_stop_guard_order()` exempts the native resting sell-stop — *"they are the resting overnight protection net"*. The codebase already accepts that a **resting order must not be age-capped**; it just never extended it to the entry side. A quote-priced buy LIMIT **still needs** the cap (it IS a stale chase price — the AUUD 414-retry it was built for). **⛔ BUT IT IS NOT A SYMMETRIC COPY — the sell-side exemption is safe because a resting SELL-stop is PROTECTIVE; a resting BUY-stop is ACQUISITIVE.** **ORB has NO window-close cancel (verified — it does not exist),** so `INTENT_MAX_AGE` is accidentally the ONLY thing stopping a resting buy-stop outliving its 09:30–10:00 window ⇒ a naive exemption trades a 30s bug for **an order resting at Webull all day that fills at 2pm with nothing watching**. **Design: exempt Tier-2 ONLY (NEVER `MARKET_CLOSED`, or it rests overnight) + ORB cancels at window close + a long window-aware backstop so an ORB crash cannot orphan it.** Also check Tier-3 (`_intent_setup_invalid_reason`) and `WORKING_ORDER_REFRESH` (`service.py:4275`), which also reset ORB today.

**🟡 BUG #3 `ORDER_NOT_SUPPORT_REVERSE_OPTION` — ROOT-CAUSED, NOT A NEW BUG, ONE LOOSE END.** It is **#436 Bug A** verbatim: *"native-stop-guard (re)arm reverse-rejected when **the just-cancelled guard / entry fill had not settled**"* — i.e. the SAME settlement lag as the false flat. **#438 already mitigates it** (`_retry_pending_native_guard_rearms`, a periodic re-arm queue). **Why it failed on ERNA:** #438's safety argument is explicit — *"the **IN-MEMORY hard stop** (tick-evaluated) **protects throughout**"* — and **bug #1 DELETED exactly that stop**, then the retry loop dropped the pending re-arm (`if stop is None: pop(...)  # stop closed -> nothing to arm`). **Both protective layers died from the same deletion.** ⇒ **fixing #1 (deployed) restores #438's assumption**; a reverse-reject should now be survivable rather than fatal. **🔴 LOOSE END — do not call #3 closed:** there is **NO `[NATIVE-STOP-GUARD DEFER]` log for ERNA**, so it is unproven the re-arm was ever queued (either the reject took a different path than the guard-arm, or `_is_reverse_conflict_reject` did not match). Worth one look with fresh context. **NOTE: reverse-conflict is NOT resting-entry-specific — it was found 07-13 on the REACTIVE path (AGEN/VEEE), so it can still occur at tomorrow's open; it is just no longer catastrophic.**

**🟢 ORB TOMORROW (2026-07-15 EOD state) — SAFE, NOTHING TO DEPLOY.** Resting entry **OFF** (ORB **177630**, env bak `.bak.pre-orb-resting-off.20260715T143700Z`) ⇒ ORB runs the **quote-priced LIMIT reactive path** (`ORB_OMS_QUOTE_PRICED_ENTRY_ENABLED=true`: ORB omits the price, the **OMS re-prices a LIMIT off the live ask at placement**, bounded by the 1.5% gap cap — **NOT a market order**) — i.e. **exactly 07-14's open path, PLUS the false-flat fix**. Strictly safer than yesterday. **Honest trade: back to the known entry leak the resting order existed to fix — NXTC 07-14 broke 9.58 → filled 8.73 (~6–9% give-up).** P&L cost, not a safety cost. **⚠️ NOTHING about the resting entry is validated** — it is parked with 3 known problems (30s kill · no window-close cancel · reverse-conflict). **Re-enabling requires #2 built AND a gate that runs through the REAL intent path** — `validate_buy_stop.py` bypasses it and therefore proves only that *Webull accepts the order shape*; that blindness is why all of this shipped.

**🟢 CRLF NORMALIZED (PR #465).** My Windows tooling rewrote `oms/service.py` + `session-handoff.md` with CRLF, turning #464's 176-line semantic change into a **9022-line diff** — making a **live-money stop-path PR effectively unreviewable**. Normalized (content sha256 identical; `--ignore-cr-at-eol` empty; 5409/5409 line swap) + **`.gitattributes` added** (the repo had none — that is why it happened). No redeploy needed: semantically identical.

**🔬 CW-v2 EXIT R&D OUTCOME (2026-07-14 EOD — NO live change made; conclusions to act on):**

- **KEEP the live exit as-is:** fixed floor +2% / **flat −5%** / gap1. The 4-day sweep **REJECTED the price-tiered stop** (−$29 vs the deployed +$3.25 — whipsaws expensive names; today's +$2 was a 1-day fluke) and showed the **trailing floor is only a safe *equal* swap** (byte-identical over 4 days; ride upside is option-value only). Do **not** re-litigate the tiered stop.

- **🟢 v2 −5% STOP SLIPPAGE — LAG GATE CLEARED 2026-07-15 (read-only, off broker fills). There is NO decision lag and NO feed problem. The ~3.9s was a MEASUREMENT ARTIFACT + Schwab's own market-order fill time.** The 07-14 sizing still stands and is the headline: stop slippage cost **$1.19** over 07-13..07-14 @qty2 (~$5.97 @qty10); live net those days **−$9.24**, and with EVERY stop filling exactly at −5% it would still be **−$8.05** ⇒ **~13% of the loss; the other ~87% is the ENTRY EDGE** (50% win vs the **~72% payoff-implied breakeven** at +2.2%/−5.7% — i.e. the pre-committed stopping rule's own kill condition). **⭐ THE MEASUREMENT TRAP (this is the durable lesson):** `[OMS-V2-MANAGED-EXIT]` is logged **after** `submit_order` **and** `_record_order_reports`, so **its timestamp is the end of the broker round-trip, not the decision** — verified **30/30 markers postdate the broker's own fill stamp, median +1.4s, max +4.5s**. Both 07-14 claims were read off that marker and both are now **DISPROVEN**: (1) ~~"UNEXPLAINED ~3.9s decision lag ⇒ v2 reads a different/slower feed or an in-OMS delay"~~ → **NO.** On the same SOBR 07-13 stop, Schwab's own `enteredTime` is **18:26:09** — the *same second* the bid crossed the trigger (18:26:09.43). The OMS decided and submitted immediately; **#333's "within ms" holds.** The elapsed time is **Schwab filling a MARKET order in a collapsing tape** (`enteredTime 18:26:09` → `closeTime 18:26:13` ≈ **4s**), then the marker logging post-fill. (2) ~~"the OMS triggered on bid `1.2587`, which appears NOWHERE in the Polygon NBBO"~~ → **NO — `ref=` was never a bid.** It is the **computed trigger level**: `1.3250 × 0.95 = 1.25875` (confirmed across exits, e.g. AGEN `CW_TARGET ref=5.1507` = `5.0497 × 1.02`). It *cannot* appear in the NBBO. The real triggering bid was **1.25**, which IS in the tape; widening the search window past the fill finds the crossing for **13/13** stops. **⇒ Per the 07-14 decision rule ("if the feed is slow, native stop is the answer; if it's an OMS delay, fixing it helps BOTH legs") the answer is NATIVE STOP** — the OMS cannot be made faster (it already submits in the same second); only an **exchange-resident** stop skips the ~4s market-order fill. **v2 arms ZERO broker-resident stops — VERIFIED** (`broker_orders` payload ? `native_stop_guard` for v2 = empty); ORB/Webull has the suspenders, v2/Schwab never got it. **⛔ BLOCKING HAZARD UNCHANGED:** a resting sell **reserves the shares** → the OMS's own +2% floor market-sell is then rejected as oversold — **demonstrated live 07-14** (NXTC 17:53 ×3 *"may result in an oversold/overbought position"*). Needs **OCO / cancel-then-sell** (#436 Bug-A reverse-conflict class, on the live stop path). **⇒ SEQUENCE NOW: (1) ~~pin the lag~~ DONE — no lag, no feed fault. (2) A native Schwab stop is the only lever left on this leg, but it is worth ~$1.19/2d @qty2 and needs the oversell hazard solved first — DO NOT build it ahead of the entry. (3) The real bleeder is the ENTRY (50% vs ~72% needed).** *(Also corrected: `oms_v2_exit_quote_max_age_ms` age-from-`received_at` is real but did NOT bite here — the trigger quote was fresh.)* **Marker fix = PR #459** (`decided_at=` stamped pre-submit, log-only) so this class of misreading can't recur. [[project_mai_tai_v2_stop_slippage_rootcause]]

- **⚠️ RETIRED/RE-AIMED — the old "resting take-profit bracket (Phase-2)" item targeted the WRONG LEG.** It assumed live under-earns the backtest *on every floor exit*. **Live fills say the opposite: the +2% side is HEALTHY** — winners median **+2.27%**, mean +2.86%, only 3/15 below +2%, and because `oms_v2_cw_floor_exit_enabled=True` the floor **RIDES past +2%** and beats the backtest's flat +2% booking (VEEE +7.86%, SOBR +5.89%). **The leak is the −5% STOP** (losers median −5.14%, 8/15 worse than −5%, worst −13.21%) → folded into the stop item above. **✅ RE-CONFIRMED INDEPENDENTLY 2026-07-15 off broker fills (same numbers derived from scratch: winners median +2.27%, 3/15 below target) — and the last steelman for the bracket is now DEAD too:** the better argument was never fill price but *converting missed wins*, so all 13 stop-outs were checked against the captured quote tape — **12/13 never had a bid anywhere near +2%** (they went straight down from entry); exactly one grazed it (VEEE 07-13 14:26, max bid 29.10 vs 29.06 needed). **Upside = 1 trade in 30 (~+$4.60 @qty2)**, against re-introducing the oversell/OCO hazard on the live stop path. **Do not build the resting take-profit.** [[project_mai_tai_v2_no_exits]]

- **Reclaim cooldown — PARKED** (revisit): the floor→next-bar-reclaim pairs churn to ~breakeven on volatile names (n1 floors +2%, reclaim buys the top and −5%s). 1-bar gap is live+backtest today; lever = bump gap to 2-3. Single knob (`cw_v2_reclaim_gap_bars`) drives both.

- **Trusted backtest harness:** `/home/trader/wt-atr-ab/atr_cw_v2_variants.py` now has the real-confirm filter (`atr_cw_v2.py::confirmed_windows`) + regular-trade entry gate (`prep`). Canonical config = `sim(gap=1, trailing=True, hard_stop=5.0)`. Do NOT trust older runs of this harness (pre-fixes) — they traded seed-carryover names + odd-lot phantom breakouts.

**🔜 NEXT SESSION — 2026-07-02 — PRIORITY STACK (HISTORICAL — superseded above; kept for context):**

> **ET/time-pinning:** only the **ORB verify (#4) is hard-time-pinned to the 09:30 ET open** (attended). The **09:12 ET readiness cron** auto-fires (green/red ntfy). Everything else (OMS SPOF, watchdog, health system) is build-during-day / attended-deploy — NOT market-window-bound. Sequencing tip: the **watchdog (#2) is quick — stand it up FIRST as a fast safety net WHILE the SPOF fix (#1) is built**, so a re-zombie before #1 lands is caught in minutes.

1. **✅ OMS SPOF FIX — SHIPPED + DEPLOYED 2026-07-06 ~11:21 ET (#391 `10ea1de`, squash-merge on genuine-green CI, NO admin) — was 🔴🔴 BLOCKING, now DONE.** Built fresh-eyes: **Fix1** OMS DB timeouts (`statement=5s`/`lock=3s`/`connect=5s`/`pool=5s`, new `build_oms_session_factory`, ON by default, rollback `MAI_TAI_OMS_DB_TIMEOUTS_ENABLED=false`); **Fix2** `_run_db` `asyncio.to_thread` off-loop on `sync_broker_positions` (THE incident method — broker awaits on-loop, flush off it) + the two stop-guard checks; **Fix3** hard-stop DECOUPLE — pre-close native-guard check + post-close reconcile both best-effort (**P2 proof = a passing test**: pre-close position-sync raises TimeoutError → protective close STILL submits); **Fix4** the two fatal control-loop gaps skip-continue + heartbeat beats through a DB outage. **Deploy verified:** OMS PID 3399733→**3457554**, NRestarts=0, 0 tracebacks, heartbeat advancing+healthy (not zombied), timeouts live (options string yields 5s/3s; `0` without), fix ON; **v2 (3146429)/ORB (3163866) untouched** (OMS-only choreography stop-strategy→restart-oms→start-strategy, fleet-flat verified at the restart moment). **P3 off-load track ✅ CLOSED 2026-07-07 at Option C.** PR-A SHIPPED (#393, tick-path off-load; OMS 3544872, verified). PR-B + PR-C SKIPPED (collapsed to marginal boot-only / post-loop sites). PR-D = **Option C, not built** (`docs/oms-spof-p4-pr-d-design.md`): the interleaving map PROVED the post-submit stop-arming braid is IRREDUCIBLE (every DB span chopped by an on-loop-required dict-mutation/broker-await; no locks) → a full PR-D would leave the braid on-loop anyway + only off-load the prologue, at high-risk restructure of the live-money fill/stop-arm path + multi-commit atomicity change = not worth it. **SPOF is CLOSED:** #391 (unbounded→≤5s-bounded) + PR-A (per-tick off-loop); residual = bounded ≤5s self-recovering stall covered by the watchdog + F3. All 5 cold sites stay on-loop, Fix-1-bounded, accounted for. PR-E (fleet-wide non-OMS timeouts) = separate tidy, not SPOF-critical. RE-OPEN Option A only on a real post-#391 bounded-stall event (proof plan in the PR-D doc). Designs: `docs/oms-spof-p3-offload-design.md` + `docs/oms-spof-p4-pr-d-design.md`. Watchdog stays the running safety net; ultimate verdict = the next real DB-stall event. [[project_mai_tai_oms_zombie_blocking_db]] **(history below kept for context):** **CONFIRMED RECURRING: zombied AGAIN 2026-07-02 ~10:03 ET (2nd time in ~12h); py-spy shows the IDENTICAL hang** (`/home/trader/oms_zombie_stack_20260702.txt` — same `sync_broker_state`→`sync_account_positions`→`session.flush()`→`psycopg wait`, this time via a quote-tick hard-stop trigger). Recovered by restart (PID 3166858→3215039, healthy). **The OMS is UNTRUSTWORTHY until this is fixed — it re-hangs on any stalled DB connection during a hard-stop tick eval (high-frequency trigger).** Root cause py-spy-PINNED 2026-07-01 ([[project_mai_tai_oms_zombie_blocking_db]]): a **synchronous Postgres `session.flush()`** in `sync_broker_state`→`sync_account_positions` (`oms/store.py:662`) runs **inline on the asyncio event loop** and hangs forever on a stalled DB connection (`psycopg wait`, no timeout) → freezes the whole OMS. Fix: (a) get blocking DB I/O OFF the event loop (executor / async), (b) Postgres `statement_timeout` + connection timeout so a hung `wait` can NEVER block forever, (c) harden `_run_tick_consumer` so a DB hang/exception can't zombie the loop (SPOF class — one bad order/sync must not kill the OMS; mirror the strategy-engine SPOF hardening). Design-first, attended deploy. **THIS IS THE DAY'S JOB.**
2. **🟡 INTRADAY OMS-LIVENESS WATCHDOG — "know in 2 min not 5 hours."** ntfy alert (same topic `mai-tai-preopen-28806a5a97b7`) when `oms-risk` heartbeat is absent >N min (~3-5). Quick to build (extend the readiness infra: a short-interval cron checking heartbeat age → urgent ntfy if stale). This incident (5h undetected) is its justification. Do this FIRST as the fast net.
3. **🟢 BROADER HEALTH-VALIDATION SYSTEM (function-not-process, ground-truth, independent cron).** The general version of #2: don't just check "service active / heartbeat present" — validate the fleet's ACTUAL function (can it place orders? are fills happening? is each service doing its core job vs the DB/broker ground-truth?), on an independent cron, alerting on silent functional failure. Design-first.
4. **✅ ORB reconcile #389 — PROVEN LIVE + BROKER-VERIFIED 2026-07-02 (DONE, drop from stack).** DSY exercised the full CANF-class sequence at 09:34–09:35 ET and it's confirmed in the `fills` table (real Webull fills, not log markers): buy 4.3399 → sell 4.20 (exit) → **buy 4.22 (RECLAIM, `attempt=2/2`)** → sell 4.3001 (exit) → cap-suppressed. `[ORB-ENTRY-FILLED]`→`[ORB-POSITION-FLAT]`→re-break→`attempt=2/2` all fired. The close-handling that #388 lacked works on real money. Just monitor going forward.

**Also queued (don't lose — adjacent to the OMS work):**

- **✅ v2 EXTENDED-HOURS EXIT ROUTING — CLOSED 2026-07-14 (#446).** Routing was fixed by #390 (LIMIT+session); the residual **overnight unfillable-close CHURN** is now fixed too — OMS abandons a close intent (`MARKET_CLOSED`) outside the fillable session (7 AM–8 PM ET) + v2 entries hard-capped 7 AM–6 PM ET. See 2026-07-14 Recent Activity + [[project_mai_tai_v2_trading_window_and_exit_churn]].

- **Standing watches:** v2 volume-floor experiment (save:kill, real-fill spread), broker-stop Part 2 (restart-while-holding on next organic ORB fill), ORB restart-while-holding `held_qty` rebuild (#389 follow-up).

**🆕 2026-06-30 (today's threads):**

- **🟢 ORB PHANTOM-POSITION / `traded`-suppression bug — FIXED + DEPLOYED 2026-06-30 (PR #388 → `ae48a10`, manual squash on genuine green, NO admin; ORB PID 3095509→3097444 clean, flat; pull-time drift clean = only orb_app.py + 3 orb tests). Both #387 (lag) + #388 (phantom) now LIVE on ORB — tomorrow's open exercises both. WATCH: [ORB-ENTRY-FILLED]/[ORB-ENTRY-RESET] behave, re-entry works, 2-attempt cap HOLDS (no churn). Re-attempt = EXPECTED (second-chance reclaim), new-but-intended.** Fix: reconcile against the OMS order-events stream (new `_drain_order_events`/`_handle_order_event`) — `traded` now = holding a CONFIRMED FILL, `entry_price` = real fill (set on fill not emit), new `pending`+`attempts`; all 3 emit paths gate on `_can_enter` + set pending/attempts; **re-enterable up to `_ENTRY_ATTEMPT_CAP=2` (original + reclaim, filled-or-abandoned), then suppressed** (operator-set, prevents gapper churn; same fill-counted state the bracket's 2-entry cap will key on). Heartbeat now shows real fills only → no phantom. 7 new tests + updated running-high/reclaim assertions, 29 ORB green, ruff clean. Branch `claude/orb-phantom-fill-reconcile`, worktree `C:\Users\kkvkr\wt-orb-phantom`. **(Original bug below, kept for context):**

- **(WAS 🔴) ORB PHANTOM-POSITION / `traded`-suppression RECONCILIATION BUG (found 2026-06-30, the mirror of the naked-position fear).** ORB commits internal state on the `[ORB-OPEN]` intent-**EMIT**, not on a broker fill: it sets **`st.traded=True`** (`orb_app.py:351/401/441`) and records the position (`position_count`/`positions` derived from `st.traded`+`entry_price`, L559/575). When the OMS **abandons** the entry — which Piece-1's quote-priced path newly enables (e.g. CELZ 06-30: `[OMS-ABANDON-INTENT] ASK_PAST_GAP_CAP`, ask 3.27 past the 2.70+1.5% gap-cap) — ORB is left with **(a)** a phantom position (heartbeat `position_count=1`, ORB thinks it holds CELZ @2.70 `exit_owner=oms_trail8`) while the broker is FLAT (0 ORB broker_orders, no fill), and **(b)** `traded=True` which **SUPPRESSES re-entry of that symbol for the rest of the session** (the `not st.traded` gate at L343/397/427) — so a clean later setup on the same name is silently skipped. **Confirmed harmful** (operator's exact question — yes, a phantom suppresses a real entry). Benign-ish today (no churn: `pending_open/close=[]`, no exit attempts; clears at the 04:00 day-roll) but it's bot-state ≠ broker-state. **FIX: mark `traded`/open only on a CONFIRMED FILL (reconcile against the OMS fill), or reset `traded` + clear the phantom on `[OMS-ABANDON-INTENT]`.** Design-first. [[project_mai_tai_orb]]

- **✅ Broker-stop VERIFIED 2026-06-30 via direct-adapter qty-1 F test** (the organic CELZ path was gap-cap-abandoned). Webull accepts the mapped `STOP_LOSS`, it rests, cancels clean. See the RESOLVED item above + 2026-06-30 Recent Activity. (No committed test harness — 06-24 was ad-hoc; the direct-adapter `stoploss_plumbing_test.py` is in scratch if needed again.)

- **~~🟡 HOLD-CONFIRM SPIKE-SLIPPAGE~~ → ✅ MOOT 2026-07-10** (hold-confirm is OFF under the confirmed-window ruleset, `HOLD_CONFIRM_ENABLED=false`; the CW entry is a bar-HIGH break after a 3-bar wait, not a 20s hold — this failure mode no longer exists). Kept for history. **(historical:)** design-first finding (live evidence, bears on the Path-B decision). INTZ 06-30 (first live volume-floor entry): the 20s hold-confirm "confirmed" (net **+1445 bps**) because the price went **vertical during the wait** (touch **0.9262 → fill 1.055, +14%**), so the **market entry filled at the TOP of the spike it was confirming**, then faded to exit 1.0301 (−$0.25). So the hold-confirm doesn't just filter false flips — on a vertical mover it **chases the spike** (the handoff's flagged "R2 slippage of the 20s wait," now with a live datapoint). **Implication for the Path-B decision:** this is evidence AGAINST naively forcing every entry through the hold-confirm — on fast vertical movers the hold makes the fill worse, not better. Capture in the Path-B analysis ([[project_mai_tai_v2_atr_validation]] / [[project_mai_tai_tick_confirmation]]); consider a max-drift / chase-cap on the hold so it skips (not chases) when price has already run past the touch by >X% during the window. Not urgent.

- **👁️ STANDING WATCH (next few days) — v2 volume-floor live experiment.** Track on the new volume-gated ATR entries: (1) **save:kill ratio** (vs the age-gate era), (2) **do entries clear SPREAD on real fills** — the +0.42% avg-net from the gate study was **GROSS of spread**; the live question is whether high-volume entries actually net positive after the real bid/ask (INTZ's −$0.25 spike-slippage is a caution flag, though that's the hold-confirm not the floor), (3) entry volume not flooding. Markers: `[V2-HOLD]` decisions, fills vs touch_price drift, `_volfloor_skip` rate. The floor is a live experiment, not a proven win.

- **🟢 ORB consume-loop latency fix — DEPLOYED 2026-06-30 (PR #387 → `fa87cd0`, manual squash on genuine green, NO admin; ORB PID 2825677→3095509, clean, ORB flat).** Pull-time drift CLEAN (only orb_app.py + the test). Restart also cleared the phantom CELZ state. **⏳ THROUGHPUT PROOF = TOMORROW's OPEN: confirm the 09:30 bar finalizes ~09:31:0x, NOT ~09:32:47 (the intent should land seconds after the 09:31:00 close, not ~1:47 later).** (was: BUILT → PR #387) Pins the CELZ 1:47 entry lag to ORB's tick-consumption throughput: ONE `xread(count=500)` per `sleep(1)` loop + a `_refresh_universe()` DB read every iteration → effective **~196 ticks/s** vs the open-burst **~547-740/s** (whole scanner-universe stream) → ORB fell minutes behind, surfacing the 09:30 bar + its entry ~1:47 late. **Fix mirrors strategy-engine #175/#179:** `_drain_market_data` drains to a budget (20k, first-pass-block then non-blocking, stop on `<count`; count 500→1000), the run loop loops-immediately-while-backlogged, and the universe DB read + heartbeat moved to wall-clock timers OFF the hot path. **No order/entry/aggregator behaviour change** — purely throughput. 4 new drain tests + existing ORB suite green, ruff clean. **Deploy = ORB-flat (outside 9:30-10:00, it's flat now), `git pull` + restart ORB ONLY (isolated; OMS/v2/strategy untouched).** Branch `claude/orb-consume-loop-latency`, worktree `C:\Users\kkvkr\wt-orb-lag`.

- **🔧 SEQUENCING (operator-set 2026-06-30):** (1) ORB consume-loop fix #387 (prerequisite — a tick-driven entry is worthless if the stream is 1:47 behind) → (2) ORB phantom-position fix (count fills / reset on `[OMS-ABANDON-INTENT]`; gates the 2-entry cap) → (3) STEP-1 OTOCO validation → (4) bracket build.

**🆕 2026-06-26 (today's threads):**

- **✅ RESOLVED 2026-06-30 (operator-called DONE) — was 🔴🔴 PRIORITY: ORB ran REAL MONEY with NO working broker-resident stop since go-live (2026-06-24).** **FIX LIVE + VERIFIED (PR #386 → `206aa3d`; flag ON, OMS PID 3080133): direct-adapter qty-1 F test proved Webull ACCEPTS the mapped `STOP_LOSS` (no 417) + it RESTS as a working order + cancels clean, account flat. "Never naked" on restart CODE-CONFIRMED (no shutdown order-cancel → broker stop survives an OMS restart; worst case un-ratcheting orphan, never naked). #1 safety gap CLOSED — ORB's next organic fill arms a Webull-accepted broker stop.** Flag stays ON. **PART 2 (restart-while-holding, empirical) DEFERRED — do it on the NEXT ORGANIC ORB fill (not a manufactured position); belt-and-suspenders, never-naked already code-proven. Do NOT hand-inject a restart-while-holding test.** Follow-on (separate): rehydrate ORB's native-stop registry on boot so the surviving stop is re-tracked/ratcheted (the orphan residual). See 2026-06-30 Recent Activity. Found 2026-06-26 RTH: the OMS arms a **native broker-resident STOP backup** (`HARD_STOP_NATIVE_BACKUP`, `_arm_or_rearm_native_stop_guard`) on every fill — belt-and-suspenders by design (in-memory trail = belt, broker stop = suspenders). **On Webull that backup is REJECTED on every trade** (`ILLEGAL_PARAMETER … correct order type`, http 417): the adapter passes the literal `order_type="STOP"`, but Webull's OpenAPI stop enums are **`STOP_LOSS` / `STOP_LOSS_LIMIT`** (confirmed in the installed SDK `webull/trade/trade/order_operation.py`: `stop_price` valid only for those). **Evidence-pinned 2/2 today** (`broker_orders`): IVF 9:31 + SDOT 9:41 — each `buy LIMIT` filled → `sell STOP` REJECTED → `sell LIMIT` exit filled via the in-memory trail. **No naked position today** (both closed flat, verified 3 ways: exits filled, `oms_managed_positions`=0, `/api/positions` FLAT) — but the broker-side net is missing. **Fix = adapter-only** (map `STOP`→`STOP_LOSS`, `STOP_LIMIT`→`STOP_LOSS_LIMIT` in `broker_adapters/webull.py`; OMS/ORB/other adapters untouched). **Design-first, after-close attended deploy** → [`webull-native-stop-order-type-fix-design.md`](webull-native-stop-order-type-fix-design.md). **OPERATING RULE until fixed: "don't restart OMS while ORB holds" is LOAD-BEARING — if ORB holds and OMS looks unstable, FLATTEN ORB FIRST, never restart-while-holding** (a restart drops the in-memory trail and there is no broker stop to catch the position). This fix is also the direct remedy for the long-standing restart-while-holding open risk. [[project_mai_tai_orb]] · [[project_mai_tai_webull_fill_arm_verified]]

**🆕 2026-06-25 (today's threads):**

- **🟢 ORB OMS-quote-priced entry (Piece 1) — DEPLOYED LIVE (flag ON) 2026-06-25 14:22 ET; AWAITING OPEN VALIDATION.** Fixes the stale-entry cancel (06-25 AZI `BUY 5 @ 1.90` → `QUOTE_DRIFT_CANCEL`, 0 filled: the bot shipped its signal-time break-level limit, ~3.5s stale at the broker). Design **PR #382** + code **PR #383** (`docs/orb-oms-quote-priced-entry-design.md`) — both **merged on genuine green** (validate SUCCESS, auto-merged by the repo merge-on-green action, NOT admin-bypass; full unit suite 939 passed). When on: ORB omits `limit_price`/`reference_price` (fail-closed); OMS re-prices at placement from its live Polygon quote `limit=min(ask+1tick, break×(1+gap_cap))`, abandons on `MISSING_BOUND`/`NO_FRESH_QUOTE`/`ASK_PAST_GAP_CAP` (instrumented). **DEPLOYED (attended, operator GO): fleet verified flat (only protected CYN), env `MAI_TAI_ORB_OMS_QUOTE_PRICED_ENTRY_ENABLED=true` (backup `.bak.pre-piece1.20260625T182151Z`), live tree ff→`37ccdc5` (exactly the 3 Piece-1 src files), `git pull` + restart BOTH → orb PID 2825677 / oms PID 2825688, 0 tracebacks, flag confirmed in both `/proc`.** Done in-window (14:22 ET) but ORB's 09:30–10:00 window was CLOSED → no entry today; this pre-positions the flag. **⏳ VALIDATION = the 2026-06-26 open: watch `[OMS-ORB-QUOTE-PRICED]`→`[ORB-OPEN]` with `oms_quote_priced=true`, OR a clean `[OMS-ABANDON-INTENT] code=ASK_PAST_GAP_CAP|NO_FRESH_QUOTE`.** Rollback = flag false + restart both. ORB-only; v2 + stop path untouched. **Pieces 2 & 3 (per-venue Webull quote book) PARKED** — Webull market-data NOT entitled (probe: `MarketData.get_snapshot` → 401 "subscribe to stock quotes"); entry+stop run off Polygon NBBO while executing on Webull (accepted basis risk; first suspect if thin-name fills look off). [[project_mai_tai_orb]]

- **🔴 Schwab token DIED again 2026-06-25 (~07:38 ET) — refresh_token `invalid_grant` (weekly expiry).** v2 401-ed on every Schwab call (552×) until operator re-auth; recovery confirmed (v2 warming symbols by ~11:30 ET, FCUV ATR fired 12:53). **The dedicated refresher stays alive + retries but CANNOT fix a dead refresh_token — only human re-auth does** (then it self-heals; no restart needed this time, streamer reconnected). Recurs ~weekly; surfaced loudly via `[SCHWAB-TOKEN-REFRESHER-DEGRADED-PERSISTENT]`. [[project_mai_tai_context]]

**🆕 2026-06-24 (today's threads):**

- **✅ Webull real ORB account — GO-LIVE DONE 2026-06-24 night.** 2FA was a red herring (real cause = wrong account_id + host); adapter built (#364), `live:orb`→webull margin wired, qty-1 live plumbing test PASSED (fill→arm→flatten verified) **after fixing 4 go-live blockers (#374/#375/#376/#377, +#373 logging)**, ORB service STARTED (PID 2765863). **RESIDUAL OPEN:** (1) **restart-while-holding UNTESTED** — don't restart OMS while ORB holds; (2) ORB real-money profitability still to accumulate; (3) first real entry = 9:30–10:00 ET 2026-06-25 (watch `[HARD-STOP ARMED]` in oms.log). [[project_mai_tai_webull_fill_arm_verified]]

- **strategy-engine restart drift** — box disk (`e76d8b5`, #362+#363) is AHEAD of the running strategy-engine (PID 2415361, not restarted). Next restart deploys #362's byte-identical leaf import — attend it.

- **ORB trail width** — regime-classifier rejected; default a FIXED trail (3% leading on 1 week, idealized fills). Confirm on more days / realistic fills before committing. Also reroute the backtest decider off `market_trade_ticks` onto validated `market_capture_trades`.

- **~~🆕 V2 ATR hold-confirm Path-B LEAK — DECISION PENDING~~ → ✅ RETIRED 2026-07-10 (moot).** The confirmed-window ruleset (see STATUS + 07-10 Recent Activity) **replaced the entire ATR touch/flip entry**, so there is no Path-A/Path-B and no bar-close fallback left to decide. Kept below for history only. [[project_mai_tai_v2_confirmed_window_ruleset]] **(historical detail:)** Hold-confirm IS live/enabled (N=20s/5bps), but **83% of actual entries (5/6 in the 06-23/24 era; 100% historically — re-confirmed 06-25: ALL 25 recent ATR entries are `ATR Flip B`, zero Path A) leak through the UNCONFIRMED bar-close fallback (Path B)**, and Path-B is net-negative (live −$4.89/32%win; backtests −$5.91 & −$18.78/~15%win) — i.e. the bar-close fallback undoes the hold-confirm edge. **Decide: (1) apply the 20s net_delta confirm to Path-B too, or (2) skip bar-close-only flips entirely** (backtest option 2 first — dropping Path-B may itself flip net positive). **FOLD-IN (06-25, operator): force FRESH-PROMOTION entries through hold-confirm (never Path B) — same fix.** A symbol's first 1–2 bars after a (re)promotion are the least-trustworthy flip: the scanner promotes AT the breakout (selection bias) so the entry coincides with the most volatile bar AND goes in unconfirmed (e.g. FCUV 06-25 12:53 entered Path B on the first bar after a 12:52 re-seed, +2% scalp). Requiring hold-confirm on Path B automatically gates these. Deeper: the ATR ENTRY EDGE (~15% win, buys faders that reverse) is the real weak link, NOT the watchlist — the change%<30 scanner-fade rule (#277) is net-PROTECTIVE for ATR, keep as-is. **Warmup phantom-flip RULED OUT (06-25 code + data + determinism test):** db-seed replays the 250 hydrated bars through `on_bar`→`_update_atr_state` (runs every bar before any gate; historical ts suppress the intent, not the ATR), so trail STATE is RECONSTRUCTED, not fresh-flip-by-construction; empirically FCUV had 11 promotions→1 entry (AZI 1 promotion→4 spread entries) = promotions don't manufacture flips. Seed-vs-continuous determinism test: trends + long-then-flat reconstruct EXACTLY; the only mismatch is **short-then-quiet >250 bars** (seed inits "long" and never re-flips → reads long when chart is short) — but it is **strictly conservative** (BUY-only entries → at worst a MISSED entry that self-heals on the next down-move; can NEVER manufacture a phantom BUY). Optional belt-and-suspenders: require ≥1 live ATR flip / N live bars since promotion before trusting state (low priority — the error direction is safe). **🆕 2026-06-29 AZI same-day A/B (real decision data, replay-validated to the live state — touch 2.6896/seg_age 45 matched exactly):** AZI produced exactly 2 entry candidates all session, BOTH stale reclaims screened by the fresh-flip age gate (`max_state_age=5`): **11:39 ET reclaim (seg_age 17) — screening AVOIDED a ~−3% loser** (would've entered ~2.86, flipped short 12:11 ~2.77); **12:57 ET reclaim (seg_age 45) — screening COST a ~+7.8% winner** (entry ~2.69 → ran to ~2.90 by 14:15). One stale reclaim would've lost, one would've won → the fresh-flip gate is a genuine tradeoff, not a clear win/bug. This is the canonical "gate doing its job" example: both were slow-grind dead-cat-bounce reclaims (17/45 bars into the short seg), exactly the shape the gate targets; v2 took 0 AZI trades = correct-by-design, NOT a miss. (AZI also API-open-blocked → double-blocked even if a fresh flip passed.) [[project_mai_tai_v2_atr_validation]]

1. **🟢 RESTART-WHILE-HOLDING — ADDRESSED by F2 (#394, deployed 2026-07-07), code-proven; live verdict pending.** The real gap
   was **ORB/Webull going naked** on restart (in-memory-only stop, no boot rebuild, native STOP rejected); v2/Schwab already
   survived (managed-row rehydrate). F2 adds the durable `oms_armed_stops` mirror + boot rehydrate + **protected-before-serving**
   reconcile (OMS-owned only; manual holdings untouched). 6 dual-broker tests green (T-ORB-REHYDRATE full-fidelity, T-MANUAL-IGNORED,
   T-ORB-LOST-RECORD, T-BROKER-FLAT-CLOSE, T-V2-REHYDRATE, mirror round-trip). **LIVE VERDICT still pending** = the next ORGANIC ORB
   fill → confirm the arm mirrors to `oms_armed_stops`, ratchets update it, and a subsequent restart-while-holding rehydrates it
   (do NOT manufacture a fill). Design `docs/restart-while-holding-design.md`. [[project_mai_tai_v2_entry_warmup_gate]]
2. **✅ RESOLVED (confirmed 2026-06-17) — CI `validate` is GREEN again and can gate.** The JSONB-on-SQLite harness
   incompatibility (`market_trade_ticks`/`market_quote_ticks.raw` → JSON variant) + stale assertions that made every
   push red are fixed on main. **Proof: PR #333's `validate` ran fully green** (unit + integration/replay + ruff, 1m24s)
   — a branch off main could not pass if the ~150 JSONB CompileErrors were still present. Merges no longer *need*
   `--admin` to bypass red CI (admin-merge stays available). Keep running the targeted test file + ruff locally anyway.
3. **✅ RESOLVED (2026-06-29) — "first real fill" / "$0 fills" was a STALE framing; v2 has filled real money since
   2026-06-17 10:55 ET.** DB-pinned (`fills`): real qty-10 fills at real prices — LNAI (06-17 10:55 buy 4.275→sell 4.295),
   BIRD, then CDT/WKSP/CRVO/CAST (06-18), CDT/SKYQ (06-22), FCUV (06-25). The 06-17 *morning* `$0` (when this item was
   written) was overtaken by the 10:55 fill and never updated. **STOP chasing a fill-plumbing bug — there isn't one.**
   The real v2 open thread is two known, separately-tracked things: **(a) UNIVERSE RESTRICTION** — Schwab API-open-blocks
   AZI/CUPR (and foreign names) `"Opening transactions … must be placed with a broker"`, so even valid ATR flips on the
   primary recent mover (AZI) can't fill (open #4); #326 evicts per-day after the first daily rejection. **(b) ATR-EDGE /
   PROFITABILITY** — the fills that land are tiny scalps; the weak link is the ATR entry edge + Path-B leak (open #5 +
   the Path-B item), NOT execution. *(Also: the early `SETUP_INVALID` cancels were the pre-#358 after-hours guard, fixed
   06-22.)* Forensics: [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) → 2026-06-29.
4. **Schwab API-open RESTRICTION narrows the live universe.** 3 of 4 06-17 names were Schwab-refused for API opening
   (foreign/manual-handling). A meaningful share of the momentum scanner's small-caps are likely un-openable via the v2
   live API path — the tradeable universe is **narrower than the scanner surfaces**. #326 now auto-evicts these.
5. **Profitability-after-spread — the open validation gate (now POST-go-live). → As of 2026-07-10 this gate IS the
   confirmed-window LIVE forward test** (`docs/atr-confirmed-window-forward-test.md`, pre-committed stopping rule: 30
   name-days; kill if median negative OR flip-exit avg worse than −5% OR win-rate below payoff-implied breakeven). CW
   replaced the ATR edge being validated here; the canary runs at qty 2 → step to 10 only after the confirmed-only edge
   shows live. Same honesty caveat holds: idealized `reference_price` fills flatter the live read — **watch flip-exit
   fills for real spread/slippage.** [[project_mai_tai_v2_confirmed_window_ruleset]] *(historical: still wants a real
   kept-win sample, not 2 events; replay Phase 2 clock since 2026-06-15 #282.)*
6. **Exit-fill QUALITY — Phase 2 (resting-limit brackets) is the design-first follow-up.** Phase 1 (#333, below) made the
   OMS decide tick-by-tick within ms, but a market-order-on-decision still slips on a violent spike-and-collapse. Phase 2
   = pre-stage scale/floor/stop as **broker-resident bracket orders at entry** so fills execute at exchange speed,
   independent of any OMS reaction. Design-first (lifecycle: partial fills, cancel-on-other-exit, reconciliation across
   restart). Also: **`deploy_preflight` blocks every in-window OMS deploy on the protected-CYN holding** (CYN
   `position_quantity_mismatch` critical + "1 open position" + reconciler-degraded cascade — all benign) and its 5.0s
   HTTP timeout is too tight for the 5.4s `/api/overview` — whitelist `MAI_TAI_PROTECTED_SYMBOLS` + bump the timeout.
7. **⏸️ TIMESALE capture ENABLE is PENDING (attended, next-session open).** PR #335 (`59500bc`) added additive,
   capture-only TIMESALE_EQUITY (true trades) to the v2 streamer — **MERGED + DEPLOYED with the flag OFF (inert,
   byte-identical; v2 PID 2252021→2319110, clean)**. Why pending: our `market_trade_ticks` are LEVELONE quote-snapshots
   (throttled), NOT true trades; TIMESALE was never subscribed (0 rows); Schwab has **no historical T&S endpoint** so it
   must accrue LIVE. ENABLE = set `MAI_TAI_STRATEGY_SCHWAB_1M_V2_TIMESALE_CAPTURE_ENABLED=true` + restart schwab-1m-v2 —
   but do it ATTENDED at the next open (after-hours has no v2 watchlist → can't read the SUBS/entitlement; arming
   unattended risks a flap on the shared CHART_EQUITY streamer that feeds the live ATR bot; zero capture lost — first
   real trades are next RTH). Watch `[V2-WS-SUB]` (services incl TIMESALE_EQUITY) + any `[V2-WS-RESP-ERR]
   service=TIMESALE_EQUITY` (= not entitled) + reconnect/flap; flag-disable ready. Design: `docs/timesale-capture-design.md`.
8. **Tick-confirmation entry research (parallel track, NOT deployed).** After a setup bar, enter only if upticks>downticks
   in the next ~15s. HELPS P5 (6.4:1)/P1 (8.5:1), HURTS P4-burst. P4 is NOT a loss engine (+$7.11 2-day; the real bleeder
   is **P1 MACD-cross −$14.53**); mixed (P4-base + P1/P5-tick) = +$3.41. Tonight's ticks are LEVELONE-grade (see #7);
   DECIDER = a faithful 10-day TIMESALE test ~early July ([[project-mai-tai-tick-confirmation]]). Docs in `/home/trader/`:
   tick_confirmation_findings, combined_tickconfirm_2day, p5_3path_baseline_2day, p4_tickconfirm_optionB_plan,
   intrabar-execution-design, timesale-capture-design.
8. **ORB (P6 OPEN) — fix #352 DEPLOYED, FULL validation gated to the NEXT RTH OPEN.** ORB was **silently inert since
   deploy**: the gateway `trade_tick.timestamp_ns` carries **milliseconds** for Polygon/Massive ticks but ORB read it as
   nanoseconds (`ts/1e9`) → every tick stamped **1970** → the session-anchored aggregator dropped all → **0 OR bars, 0
   trades** (heartbeat `bar_counts:{}`/`last_tick_at:{}` while the stream had live ticks). The strategy-engine already
   defends with `_normalize_tick_timestamp_ns`; ORB didn't. **PR #352 (`f404544`) MERGED + DEPLOYED 2026-06-22 10:39 ET**
   (ORB-side `_normalize_trade_ts_ns` magnitude ladder; CI green, editable-install git-pull + ORB-only restart, fleet
   untouched). **Mechanical fix VALIDATED same-session** (heartbeat `last_tick_at` now shows 2026 timestamps, bars
   complete). **⏳ REMAINING GATE — validate at 2026-06-23 09:30–09:40 ET: `bar_counts` POPULATES (or_bars fill
   09:30–09:34), an OR builds, a breakout evaluates (`[ORB-BREAKOUT]`).** **Real-money flip (qty 10, was targeted 06-22)
   stays BLOCKED until that passes.** Cloud /schedule can't reach the VPS → validate attended/VPS at the open.
   [[project-mai-tai-orb]]
9. **🔴 EXTENDED-HOURS EXIT ROUTING — now a CONFIRMED LIVE FAILURE (2026-06-30), PRIORITY JUMP. Design-first, fresh session.** v2 can now ENTER extended-hours (the #362 PORT fix: entries route limit+session), but its **EXITS still route MARKET** — and a market order **cannot fill in extended hours** → **any after-hours v2 entry is UNCLOSABLE until RTH.** Live proof: CELZ 10@1.2687 entered 16:30 ET (after-hours), the OMS-managed exit churned `-10 MKT` every ~30s (`watchdog_refresh`), all cancelled/accepted-never-filled; the bot's leg sat stuck inside the operator's 1,010-share account holding (~1,000 manual @ ~$1.75 + the bot's 10). **Operator closed it manually via a ToS SELL LIMIT (limit fills EH; deliberately NOT routed through the desynced OMS).** **FIX (design-first): route v2 EXITS as LIMIT + correct session, mirroring what #362 did for entries.** Until fixed: after-hours v2 entries are a real risk. CELZ now PROTECTED (see below) to stop the recurring entanglement; broader after-hours names remain exposed.

   - **🔴 OMS marked the managed record CLOSED on exit-SUBMIT, not on FILL — same bug class as the ORB phantom (#388).** `oms_managed_positions` CELZ flipped to `status=closed`/`current_quantity=0` + heartbeat `position_count=0` while the **broker still held the shares** (the market exit never filled). **Check the OMS v2-exit path with the #388 lens: mark closed on a CONFIRMED FILL, not on intent-submit.** Design-first.

   - **🟢 CELZ PROTECTED (2026-06-30 after-close):** `MAI_TAI_PROTECTED_SYMBOLS=CYN,CANF` → **`CYN,CANF,CELZ`** (backup `.bak.pre-celz-protect.20260630T205314Z`). **NOT yet applied to the running OMS/v2** (still CYN,CANF) — applies at the **pre-open restart**; until then v2 could enter another (small, stuck-till-open) CELZ leg. Why: v2 took TWO CELZ legs today (15:30 RTH-exited clean, 16:30 after-hours stuck) on the account that holds ~1,000 manual CELZ — same entanglement as FCUV/CANF. [[project_mai_tai_v2_real_account_routing_risk]]

---

## ✅ CLEARED (was a go-live blocker)

- **OMS exit path is TICK-BY-TICK — FIXED (#333 `c79e8f5`, deployed live 2026-06-17 ~19:30Z).** Diagnosed off the live
  LNAI ATR-Flip trade: the +2% scale fired **~70s late at 4.345** (not ~4.45). Root cause (DB+code pinned): market-data
  had bids above +2% (4.43–4.46) for a ~14s window during the spike, but `sync_broker_state()` REST ran **inline every
  5s on the same loop that read quotes** → ticks backed up; AND the 5s staleness guard was blind because `received_at`
  was stamped at processing-time, not event-time. Fix = dedicated `_run_tick_consumer` task (market-data on its own task,
  never starved by broker-sync/intents) + last-quote-wins `_coalesce_ticks` + event-time staleness from `produced_at`.
  Behavior-identical for intents/sync/heartbeat + ladder logic; 57 passed/1 xfailed; deployed flat (only protected CYN
  held), clean (0 tracebacks). Design: [`oms-tick-consumer-design.md`](oms-tick-consumer-design.md). **Phase 2
  (resting-limit brackets) = open item #6.** True verdict still wants the next live intrabar spike on a v2 position.

- **04:00 ET watchlist-staleness race — FIXED (#324, deployed) + VERIFIED LIVE 2026-06-17.** At the 08:00 UTC / 04:00 ET
  roll: `bot day-roll fired` (08:00:00.654) → `scanner session-roll fired` (08:00:01.106) → scanner reset; v2 watchlist
  → count=0 with yesterday's 5 symbols UNSUBSCRIBED. **Zero stale symbols survived; no re-promotion race; no errors.**

- **Whole exit ladder live-proven** (2026-06-15, CUPR — scale/floor legs on simulated).

- **ATR fresh-flip qualifier MECHANISM** ✅ validated/complete (2026-06-16, live both directions).

---



---

## 📦 OPEN ITEMS closed 2026-07-29 (second pass, with the operator)

> Operator: *"I still wanna make sure to close all the open items... let's bring it to zero."*
> Closed 16 of 19. Reasons, so none of these silently return:
> - **5 UNVERIFIED** (INTENT_MAX_AGE · BUG #3 DEFER log · Webull NO_SUCH_TICKER · OMS
>   record-desync · dead-ladder audit) — operator call: close rather than carry unproven claims.
> - **env-vs-default sweep** — DONE, tool shipped (`ops/health/env_default_drift.py`, #598).
>   74 divergences, 14 numeric; the 3 that mislead trading reasoning are recorded in the PR.
> - **dual-broker fan-out validation** — satisfied by production use: 30 Webull fan-out fills
>   across 9 symbols since 07-24.
> - **INTENT_MAX_AGE exemption / ORB resting-bracket design** — DORMANT, ORB resting entry is off.
> - **floor-ratchet study** — a saved re-runnable resource, not a task.
> - **429 past the retry bound** — accepted trade-off (protection outranks bookkeeping).
> - **07-27 exit history short (4 of 8)** — unrecoverable, won't fix.
> - **entry-quality / gap-through caps / reclaim-trigger studies** — closed. The exit-side
>   search is already CLOSED ('the ENTRY is the problem'; >100 configs on 27 trades). They
>   return with evidence if a live loss points at them.
> - **fossil-warmup guard** — closed. The attempt (#552) blocked every arm for 68 min and was
>   reverted; its own design says do not build without a base rate we do not have.
> - **injection-seam / Webull OCO Ph3** — standing RULE and a triggered CHECKLIST, not tasks.
>   Rules live in memory; a gate fires when the operator decides to run it.
> - **missed-flip re-run** — folded into the post-close work of item 1.

⭐ The lesson recorded for next time: the list grew because items were only ever ADDED. A study
nobody runs, a rule that is never 'done', and a dormant feature's item all sat as open work.


---

## 2026-08-03 — the entry cap breached four times, and three phantom rows

**Nothing deployed.** Everything below is built-or-designed and waiting on an attended session.

### The operator found the entry bug on a chart
He saw three trades inside one unbroken ATR trail and said so. He was right: exactly ONE
`[V2-CW-ARM]` and ONE `[V2-CW-DISARM]` per run. **Four breaches, real money** — HYFM 12:09/17:31/18:11
and FUSE 17:03 — three entries each against a cap of two.

Root cause is two defects that compound. The resting buy fills **intrabar**; the arm confirms the
**same cross** at the bar close 21s–706s later and ran `cw_entries_this_flip = 0`, wiping the entry
that caused it. And the resting fill **never consumed a slot at all** — the only increment lives on
the reactive path, so the live default entry type since 07-22 counted for nothing. That second one
is what let the reclaim path see two free slots and fire two reclaims.

⭐ Operator revised the spec mid-build: **the cap is COMPOSITION, not a count** — ≤1 resting and
≤1 reclaim; two reclaims is "very bad". Degenerate case confirmed: if the resting never fills its
slot is **forfeit**, and reactive may not substitute into it. Built as `35b46e1`, 209 v2 tests green.
Six characterization tests **retargeted, not deleted** — including one that existed to document the
SOBR stale-trigger chase as live-and-accepted, which this fix closes.

### Three phantom managed rows
`live:orb` FUSE/HYFM and `live:schwab_1m_v2` HYFM. Broker-flat, no exposure, but they block fan-out
re-entry. **All three produced zero miss lines**, which is the confirmation they are the
never-enrolled shape: the exit poll iterates an **in-memory set**, not the table it services.
Collision-skip, all five discard sites, rehydrate, `_v2_accounts()`, the store lookup and a
loop-abort are each ruled out with evidence; how the keys left the set is **still unpinned**, and
the fix is deliberately cause-agnostic.

### P&L, per-trade %, median first
Real money: **+1.10 (EH), +2.04, −4.43, −6.00** on UPC, then HYFM **+2.28, −4.46, +2.06**. Median
across the day is thin and the shape is the known one: **7 wins clustered at +2%, 5 losses clustered
at −5%**, so a 58% win rate still nets negative. Drop-one by name: **EZRA alone (−5.04, −5.10)
carries almost the entire loss** — remove it and the day is ≈ −0.8 instead of −10.9.
⭐ UPC ran 7.38 → 5.70 → 6.66 — a genuinely **oscillating** name, the profile the operator wants.
The losses on it were execution and timing, not selection.

### Fixed / shipped-to-PR today
`#639` bar-gap dead band (a 1-missing-bar hole alerted forever and could never be repaired; 8,496
such holes in the v2 series) · `#641` pager scoping — **paper `polygon_30s` could RED-page the
naked-position alarm** — plus a halt-downgrade gated on `REST_FAILED == 0` (Schwab REST 401'd for
2h41m that morning; only the window guard kept the two from overlapping) · `#642` the design note.

### Ops
Schwab refresh_token died Sunday ~15:02 ET; the warning chain fired correctly (AMBER 45h → RED 7h →
RED expired) and the operator re-authed Monday 06:40. **Next expiry lands Mon 08-10 06:40 ET, 19 min
before the EH window** — a Wednesday re-auth reminder is now live to park it midweek.

## 2026-07-31 — the KUST execution day, and a backward study that turned out to be a dead end

**One live loss opened a chain.** KUST entered pre-market 09:11 ET and lost **-5.17%** on a signal
that was RIGHT (price ran to 1.79). The Webull fan-out leg, given the identical instruction, made
**+1.76%**. ~6.9 percentage points of pure execution loss on the same signal.

### Root cause, proven on the captured tape
The pre-market entry got **no native OCO** (`[V2-OCO-EMIT] SKIPPED (outside RTH)`), so the software
ladder owned the exit. It placed a sell LIMIT 1.74 and the working-order refresh **cancel/replaced it
NINE times in six minutes**. The real Schwab bid across that window:

    13:26:13 1.76 | 13:26:54 1.75 | 13:27:34 1.74 | 13:28:02 1.78
    13:26:14 1.77 | 13:27:13 1.76 | 13:27:38 1.75 | 13:28:04 1.78

**The bid was >= the limit at EVERY tick.** The order was fillable the whole time and we kept taking
it off the book. Webull's identical 1.74, placed once and never cancelled, filled in **34 ms**.
Then the hard-stop market sells collided with each other -- **125 rejects** -- each submission racing
the previous one still in flight ("oversold"). Slowing the cadence WAS the fix: after a 30.9s gap the
next attempt filled immediately.

### Shipped
| PR | What | State |
|---|---|---|
| #631 | backtest models the 07-30 per-symbol watch-start cap (#618/#619) | merged |
| #632 | open items 8/9/10 (reconciler severity, Redis eviction, SELECTION) | merged |
| #633 | **P0a** -- HOLD a marketable managed exit instead of cancelling on a timer | merged + **deployed** |
| #634 | OCO-exit-poll MISS path made visible (log-only) | merged + **deployed** |
| #635 | OCO entry lookup must resolve to a FILLED / partially-filled entry | merged + **deployed** |

**AXTU's 2 unrecorded exits backfilled** from Schwab history via the canonical
`_persist_oco_exit_fill`, provenance-stamped. AXTU now pairs 3/3: **-5.26%, +2.27%, -0.77%**.

### Four false alerts before 10:30 -- none was a fault
| Alert | Reality |
|---|---|
| readiness AMBER | trade-coach deliberately stopped |
| reconciler CRITICAL (AZIO) | the operator's manual trade; **the OMS never touched it** |
| OMS "fleet down" RED | Redis evicted the 47 KB heartbeat key |
| fleet-health RED | three symbols had just been promoted |

Common root: monitoring cannot distinguish *intended state* / *normal transient* from *breakage*.

### Theories tested and KILLED (do not re-run)
- "the OCO exit poll never fires" -- **disproved**, it recorded FCUV 4x including in-process
- "a cancelled buy shadows the entry lookup" -- dismissed on a bad FCUV comparison, then re-opened
  and shipped as #635 on its own merits; still NOT proven to be the miss cause
- throttle starvation -- ruled out (30s/symbol on a 15s sync)
- propagation lag as a *window* problem -- ruled out (the poll uses the 3600s default)

### THE BACKWARD EXECUTION-% STUDY IS A DEAD END -- do not attempt it
Scoping it killed it. July v2 real money = 135 filled entries, and:
- only **17 (13%)** pair unambiguously without the FIFO inference that once invented a -8.40% trade
- **77 filled exits take the `close` route, which has NO link to its entry** -- a data-model gap
  Schwab history cannot repair, because the linkage never existed
- the **56 recoverable** entries are ALL native-OCO and **07-22 or later** (native OCO went live 07-22)

**The disqualifier is BIAS, not sample size.** On the native-OCO path the BROKER owns the exit, so
churn-to-stop structurally cannot happen -- while the failure lives on the software-ladder path,
which is exactly the unrecoverable population. **KUST's own 07-31 entry carries no
`native_oco_bracket`** and is excluded from the 56 by construction. Any execution-% from that frame
would understate and falsely reassure.
=> Accept the pre-OCO era (79 entries, incl. the 07-13/14/15 churn days) as permanently
unrecoverable. The forward path -- **OCO everything, then let the live run be the clean test** -- is
the measurement.

### But "OCO => churn-immune" is NOT unconditional
Cancelled/rejected sells within 60 min of an **OCO-bracketed** entry: NVVE 07-23 **11**, KUST 07-22
6, FIEE 07-27 6, several at 3. The mechanism is in the log:
`[OMS-OCO-STAND-DOWN-CLEARED] ... OCO gone; ladder deferred` -- when the stand-down clears, the
software ladder resumes and can churn **even on a bracketed entry**. The pre-market-OCO fix must
design for the stand-down-clear path, not merely emit a bracket.
*(Caveat: symbol-level count in a time window; some sells may belong to another position that day.)*

### Cleared
The 2 `sells > buys` symbol-days (CWD 07-02, CLRO 07-07) are **not** the #605 claimed-a-manual-trade
shape -- share quantities balance exactly (40=40, 20=20). Partial-fill splitting on the sell side.

## 2026-07-30 — 11 PRs, an entry-path root cause, and the day the alerts got wired

**Median of the day's 3 morning round trips: −4.92% (all stopped out). After the entry fix: +2.09%
(3 winners, all hitting +2%).** Same bot, same day, opposite sign.

### ⭐⭐ ROOT CAUSE — the 09:30-10:00 ORB window was DEFERRING armed entries to 10:00
The operator spotted it on a TOS chart within minutes: *"it's not resting, it's not reclaim, it's
been going long a long time, and it bought it all the way at the top."*

`_cw_in_orb_window` suppressed reactive entries 09:30-10:00 but **PAUSED the setup instead of
CANCELLING it**. `cw_trigger` freezes at flip+2 and never expires in RTH, so every armed symbol was
released at ONE clock edge at 10:00:00 and entered on a stale trigger. The log even named the shape:

    09:38:04 [V2-CW-ORB-BLOCK] SNDG break suppressed px=5.6400 trig=5.1100 — setup stays ARMED and
             will enter on the first quote above trig once the gate lifts (the SOBR chase shape)

APLX bought +23.7% past its flip level, SNDG +18.9%, both at 10:00:0x. **The window was reserving
the most volatile 30 minutes of the day for `project-mai-tai-orb`, inactive + DISABLED since 07-23.**

⛔ **RULED OUT, do not re-litigate:** bad bars (the stored NUWE 08:59 bar matched the operator's TOS
chart exactly, volume included), #590/the cooldown (independently verified inert at `ffdf3d6^` —
every reader was dead code or a log argument), and warmup-as-root-cause (contributory only).

⭐ **But the deeper defect was the operator's, not mine:** *"whenever the momentum scanner confirms a
NEW stock it needs to wait for a fresh ATR flip; the ones we've had since 07:00 don't have to."*
The mechanism existed — `_cap_reconstructed_segment` — keyed on **global process boot**, so a symbol
promoted at 09:38 happily accepted a flip from 09:16. Right idea, wrong clock.

### Shipped
- **#616** recorder files each trade under its ENTRY's ET day, not the run date
- **#618** per-symbol watch-start + ORB window REMOVED (both, deliberately together)
- **#619** cap the reconstructed segment AFTER the warmup feed — `[V2-CW-SEED-CAP]` had **never
  fired once**; the boot-hold was masking it by freezing entries bot-wide
- **#620** never compute true range across a bar gap
- **#621** fleet check #4 — v2 bar continuity
- **#623** bar-hole watch + **auto-repair** -> ntfy
- **#624** `strategy_bar_history.source` provenance column + REST repair
- **#625** re-check liquidity while resting; fail-closed on an unconfirmed cancel
- **#626** reconciler CRITICAL findings -> ntfy
- **#627** the backfill insert was missing NOT NULL `indicators`
- **#628** a cancel intent tracks the REQUEST, not the target order's fate

### ⛔ Three things that were WRONG in this project's own records
1. **trade-coach burned 45% of the box while DISABLED**, in a 429 retry storm on a dead OpenAI key
   since 07-25. 07-29's "CPU is inherent, a restart did nothing" measured right and concluded wrong.
   Stopped; load 2.2 -> 1.37. 887 reviews, 93% "good", **zero** for the live-money bot.
2. **The trade recorder could never have written a byte** — its cron sat in TRADER's crontab while
   `trade_records/` is root-owned and the env file root-readable. It had never entered its own ET
   window, and the two files present were root-owned artifacts of manual runs.
   ⭐ **A hand-run as the wrong user is not a test of a cron.**
3. **The reconciler had already caught the IRE drift** — `position_quantity_mismatch` CRITICAL at
   12:55:22, eight minutes after a phantom fill, repeated 71 times. **Nobody was ever told.** There
   was no alerting on reconciliation findings at all. Detection was never the missing piece.

### The liquidity floor was ENFORCED — and stale
Operator asked to validate it. All four below-floor entries were RESTING orders: APLX passed the
floor on a 12,530-share bar at 13:09 and **filled at 13:19 into a 100-share bar — 125x thinner than
the gate approved.** Checked at placement, never re-checked before the fill (#625).

### Data + hygiene
681 bars backfilled (`source='rest'`), 12 stuck cancel intents closed out (backed up first),
2,430,969 findings + 269,051 runs pruned. **DB 12 -> 10 GB. Logs 1.7 GB -> 426 MB.**
⭐ `dashboard_snapshots` **1020 MB -> 14 MB with ZERO rows deleted** — pure TOAST bloat, never a
retention problem. The original "prune it" plan would have destroyed live state and reclaimed nothing.
⚠️ It regrew to **96 MB in four minutes**. #366 is now evidenced twice.

### Lessons worth carrying
- **A circular metric proves nothing.** I "validated" the resting path by comparing its order price
  to the number the bot used to SET that price. It scored 0.00 every day and could never have found
  anything. The operator rejected the conclusion; the non-circular test inverted it.
- **Date-filter every log grep.** An unrotated log made "49,270 MACD crosses today" out of weeks of
  history; the real number was 433. Made the same error twice in one day.
- **The operator's daily eyeball beat my log analysis three times.** Weight it.

## 2026-07-29 (Wed) — a restart-free day, three wrong answers, and the fix for both

**Deployed:** #605 ownership-scoped exit capture · #606 readiness ORB-aware · #608 close-retry
termination bound · #610 all-day trade recorder · #611 recorder captures unpaired entries.
**HEAD `b951b4e`.** No v2/strategy restarts all session — deliberate, so the day's numbers are usable.

### The day's real numbers (ownership-scoped, repaired)

**23 clean round trips · median +1.38% · 14/23 wins.** Per broker (standing rule):
Schwab n=6 median **+1.83%** · Webull fan-out n=17 median **+1.11%**.
Entry slippage vs intended: median **+0.199%**, worst **+1.194%** (NCRA 12:30, reactive).

⭐ Schwab blocked STFS / GMM / NCRA, so **the fan-out is what rescued most of the day** — 17 of 23
round trips only existed because the Webull leg fired. That is the fan-out earning its keep.

### ⛔⭐ We booked the operator's manual TOS trade as one of our exits

An AMIX **1000-share** manual TOS sell — filled **2 minutes BEFORE our own entry** — was recorded as
one of our exits. `fetch_oco_exit_fill` matched on **symbol alone** and walked every order in the
account: it never checked the order was ours, that the exit followed our entry, or that the quantity
was one we trade.

> *"our Mai tai is not supposed to interfer manual trades.. it listens what posted.. this is a core
> concept"*

**Fixed live in #605:** the fetch now requires `entry_broker_order_id` and **fails closed without
it**, `_find_our_entry()` locates our own entry, and it walks **only** that entry's
`childOrderStrategies`. Ownership is now structural, not a heuristic. Both operator manual-fill rows
were deleted from the books (0 suspect of 36 remain).

⭐ **Verified live tonight against a real manual position:** Schwab is holding **CYN 5000 sh
(~$5,350)** overnight and we have **zero** CYN orders, fills, intents, or bars. Untouched.
⚠️ Honest limit: CYN is *not* a test of #605 — the old bug only fired on symbols we also traded. It
confirms the scoping invariant, nothing stronger.

### ⛔⭐ THREE WRONG P&L ANSWERS IN ONE EVENING — and why the recorder exists

1. **FIFO pairing** reached across one missing exit and **manufactured a −8.40% AMIX trade that never
   existed.** The real trade was **+1.78%**.
2. **Coid pairing** then exposed **5 exits dated BEFORE their own entry** — rows written by the
   symbol-only matcher above.
3. The **tiered-stop test inherited the corruption**, so its first two numbers were retracted.

All three retracted; the day re-derived ownership-scoped (23 exits repaired, **0 impossible pairs**).

⭐⭐ **The lesson is not "pair more carefully" — it is that ATTRIBUTION MUST BE CAPTURED, NOT
INFERRED.** Hence #610: an append-only recorder that writes each round trip **with both brokers'
order ids at the moment it closes**, so nothing downstream ever guesses which sell belongs to which
buy. Runs `*/5` all day from tomorrow's open.

### Exit-rule findings on the repaired data (directional only — n=23, one day)

| rule | median | vs actual | wins |
|---|---|---|---|
| actual (live) | **+1.38%** | — | 14/23 |
| tiered stop <$3:−5 / ≥$3:−3 | −3.00% | **−4.38pp WORSE** | 10/23 |
| floor +2% then trail 2 / 3 / 5% | +1.76% | **+0.39pp better** | 15/23 |

⛔ **Do not act on either yet.** The tiered result is stable under drop-one, so the tighter stop
really does intercept trades that later won. But the floor's +0.39pp is **driven by one trade** and
the trail is **inert** — max MFE all day was +4.57%, so a 2% trail never has room to beat a flat +2%.
⛔ And **7 of 23 round trips closed inside one minute**: no completed bar exists, so the bar path
cannot speak for 30% of the day. Needs **3–5 clean days**.

### Other fixes

- **#608 Webull close-retry STORM** — NCRA **145 rejected sells in 55 min** (AMIX 25, STFS 7). ⭐ The
  cause was **NOT a missing bound** but a counter **RESET on inconclusive reads**: a sawtooth that
  could never reach any limit. Only a positively-**HELD** read resets now; `UNKNOWN` accumulates to 8
  and stands down **without** touching protection (⛔ standing down must never close the row — that is
  the ERNA naked position). Mutation 3 hung the suite at bound=99999, so `_MAX_SIM_REJECTS=64` now
  bounds the *test* too.
- **#606** readiness stopped RED-ing on the decommissioned ORB (3 FAIL = all ORB ⇒ a false "DO NOT
  trust the open" every morning).
- **Dead-bot prune** — 1,091,270 rows, **1962 → 815 MB**, backtest re-verified **byte-identical**.
- ⚠️ **`trade-coach` restart was INEFFECTIVE** (43% → 47%). The CPU is **inherent, not drift**; OMS
  heartbeat starvation will recur. Folded into open item 2.
- **ORB decommissioned** — the service was `inactive` since 07-23 but still **enabled at boot**, so a
  reboot would have silently started a real-money bot at qty 10. ⭐ I first recommended
  `ORB_ENABLED=false` and **that would have broken the live fan-out** — the flag seeds the `live:orb`
  broker account. Caught by tracing the reader chain before executing.

### The 15:59 entries — resolved, nothing naked

Two 15:59 entries (Schwab q=2, Webull q=1) had no exit fill. `OMS-V2-EOD-OCO-TRANSITION` **did** fire
at 20:00:01 UTC and handed the exit to the EH limit ladder. ⭐ Both brokers confirm **AMIX not held** —
so the missing rows are the **native-OCO exit-capture gap, not a naked position.** ⛔ This distinction
is the whole point: "no fill row" ≠ "still held". Ask the broker.

That gap is also what #611 fixes in the recorder — 26 entries produced 23 pairs, and the 3 unpaired
were invisible. They now land in `<day>.unpaired.jsonl`, **overwritten as state** (appending would
turn one open trade into a pile of stale duplicates reading as dozens of naked positions).

### Self-caught before it shipped

The recorder's first crontab was `*/5 11-23 * * 1-5`, which *looks* like "07:00–19:59 ET weekdays"
and is not: those are **UTC** hours, so it lost ET 20:00–20:30 year-round **and** all of Friday's
post-19:00 tail (UTC Saturday, dow 6) — precisely where the EH exit ladder runs. Both guards moved
into the script in ET; proven with a stubbed `date` at every boundary.

**Late addition (#614).** The recorder's own output caught a second gap: it reported FOUR unpaired
entries where I expected three, because one had exited via `-close-` rather than `-ocoexit-`. v2 exits
leave by two coids and **only one is attributable** — `<symbol>-close-*` gets a fresh suffix and its
own single-order intent, so nothing links it to its entry. Over 30 days that is **74 `-close-` vs 36
`-ocoexit-`**, and it is the route carrying flip / hard-stop / EH / EOD exits, i.e. the **losses** ⇒
an ocoexit-only view reads optimistic. Now **labelled, not paired** (`exit_route:
"close_unattributed"`, deliberately excluded from `ret_pct`). Real fix, not built: stamp the entry's
`broker_order_id` onto the close order at the WRITE site.

⚠️ Process notes on tonight's shipping: I merged #610 while my check-wait loop had matched a *stale*
passing run, so its fixup's own Validate finished only after the merge (it passed on main). And CI
never created a run for two branches — #612 was reopened, force-pushed and still got nothing, so it
was closed and re-raised as #614 on a fresh branch, which validated normally.

---

# 2026-08-12 — four inverted premises, one probe, two ships, one deploy

**Shipped + deployed: #684 (`867dcd0`) — cancel-verify and the fan-out price ceiling. Both flags ON.**

## The day's method story: every premise checked, four inverted

This session began with a question about resting-entry slippage and spent most of its length
*disproving stated premises before acting on them*. Recording them together because the pattern is
the finding — each was checkable in minutes and each would have driven real work in the wrong
direction.

| premise | what the data said |
|---|---|
| "Resting entries fill badly — +55 bps vs the ask" (**mine**) | ❌ a **stale-quote artifact**. Against the TRIGGER: median **+0 bps**, and **0 of 66** paid above both the offer and the tape |
| "Webull accepts a STOP_LIMIT combo master" | ❌ **417**, twice, CORE/RTH |
| "Schwab's brackets never fill — ladder wins by ~3s, 0-for-94" | ❌ bracket wins **125/141** schwab, **166/174** orb. All 8 ladder wins were **at/after 16:00** |
| "Brackets stopped 08-07 / lost the stop leg 08-04" | ❌ 08-04 = 31 children / 31 stops / 31 targets; **08-07 the bot placed no Schwab entries at all** |

⛔ **My own two method errors produced the same false conclusion from two directions**, which is what
made it feel confirmed: (1) counting brackets over Schwab's order history, which is a **SHARED book**
containing the operator's manual trades — 17 manual buys read as a bot regression; (2) 15-day chunked
queries that **truncated silently**, hiding 67 entries. Fixed by joining on our own order ids and
fetching day-by-day. See `feedback_the_brokers_book_is_shared`.
⭐ The thing that broke it open: a **control with a known answer** (08-04's AAOG, whose legs the
operator had already read by hand) still showed its stop leg. **Two wrong methods agreeing is not
corroboration.**

## Probe W — run live, settled

`live:orb`, FRTT, qty 1, CORE/RTH. **A** LIMIT master + legs → **200** (placed live). **B** STOP_LIMIT
master + legs → **417 `invalid order_type`**. **C** bare STOP_LIMIT → **200** at preview.
⇒ Webull refuses a stop-limit combo master; **Schwab accepts the identical shape**. The fan-out
order-type asymmetry is **the broker's**, not ours. ⛔ Do not remove `webull.py:949` — 174 live
Webull brackets depend on the shape it enforces.

⛔ **The first probe run was INVALID and the control caught it.** Shapes B *and* C both 417'd — but C
is the known-good control (44 historical accepts). The probe had bypassed `_map_order_type` along
with the guard under test and put the broker-neutral name `STOP_LIMIT` on the wire; Webull's enum is
`STOP_LOSS_LIMIT`. **A third refusal category beyond client-side and broker-side: our own malformed
payload.** #681 still carries that bug at line 119 — fix before merging.

## What shipped

**1. Cancel-verify** (`oms_cancel_verify_enabled`). The cure for FRTT 08-11's 136-minute unowned
order. Read the target back until settled → re-submit if still working → `[OMS-CANCEL-UNCONFIRMED]`
otherwise. `accepted`/`PENDING_CANCEL` deliberately excluded from the settled set — believing it is
what cost the 136 minutes. A *raised* cancel is an UNKNOWN, not a failure. Backgrounded, because
inline would stall the intent path, which carries exits.

**2. The fan-out leg gets a ceiling** (`oms_v2_rth_fanout_limit_enabled`). #674 capped only the
Schwab primary — *"the fan-out leg is deliberately untouched here"* — so the Webull leg was an
**uncapped MARKET in RTH on both sources**. Live proof same day: BAOS, primary decided **1.1702**
under its cap, fan-out paid **1.1800**, lost **5.08%**. Probe W is what made this free: a capped
**LIMIT** master keeps the attached bracket, so there is no price-vs-protection trade-off.

24 tests · suite **1989 pass / 0 fail** · ruff clean · **9 mutations, each caught by the right test**.
⛔ Process note: my first mutation run used `git checkout` to revert *before committing*, which wiped
the implementation and made two mutations meaningless. Re-applied, committed, re-ran all of them.

## The deploy — 18:05 ET, both flags ON

Operator's call to enable at deploy rather than flags-off: a flags-off deploy would have forced a
second restart in tomorrow's **pre-market**, the worst window to take one.
⭐ **OMS-only change ⇒ OMS + strategy restarted, `schwab-1m-v2` left running.** It is the bar builder,
so this produced **no bar hole** — bars advanced 18:03 → 18:04 through the restart. Reuse this scoping.
7 services active, `NRestarts=0`, 0 tracebacks, both flags confirmed from `/proc/<pid>/environ`.

## 🔴 Live exposure found during the post-deploy check (predates the deploy)

**CRWU** held 2 (schwab) + 1 (orb), entries 5.8899 / 5.88, bid **5.61** — on the day's low with **no
broker-side stop**: the RTH bracket expired at 16:00 and `EOD_OCO_TRANSITION_ENABLED=false` means
nothing replaces it. The ladder tried twice and both brokers refused (Webull
`ORDER_NOT_SUPPORT_REVERSE_OPTION` 15:30; Schwab `oversold/overbought` 15:54), then **nothing was
attempted for >2h** while the OMS polled for an OCO fill that can never arrive on managed rows
13,736s / 18,618s old. Operator is closing by hand. **This is the live case for #647 Gate 2.**

## Other findings worth keeping

- **Pre-market is 0% bracketed** — bot-only, 14d: RTH 172/172 orb, 131/132 schwab; PRE 0/34 and 0/13.
- **The levels are quantised by ~70 bps.** +2%/−5% computed off the *decided* price then tick-rounded
  ⇒ 08-11 actually ran **+2.11…+2.47 / −4.38…−5.11**, and the 0.5% entry band collapses to **zero**
  on sub-$2 names. Fix the unit, not the number.
- **`virtual_positions` self-healed** a phantom RMCF 2 via `[VIRTUAL-CLEAR] zeroed 1 virtual
  position(s) with no broker backing` — the mechanism works, on its own cadence.
- **CYN cleared** by the operator (all 5,000), so `MAI_TAI_PROTECTED_SYMBOLS=CYN,TE` is now stale.
- **Schwab re-auth 05:21 ET** ⇒ next expiry **Wed 08-19 05:21 ET**, off the Monday slot but before
  the 07:00 EH open.
