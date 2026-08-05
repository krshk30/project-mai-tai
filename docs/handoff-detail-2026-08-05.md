# Handoff detail - 2026-08-05 (dated; superseded by the next dated file)

> Split out of `session-handoff.md` on 2026-08-05. That file is OVERWRITE-ONLY and had
> reached 408 lines against its own ~150 target - the same failure shape as the stale
> entry-criteria doc: nobody reads it whole, so a wrong section survives inside it.

## ⚖️ THE HONEST LEDGER (2026-08-05) — read before quoting any finding from this board

**Demonstrated money cost of everything found on 2026-08-05 is approximately ZERO.** Say that
plainly and first. The operator's own read — *"on the chart and the page everything I bought closed
cleanly and nothing is pending"* — was correct on 2026-08-05, and was correct against three separate
alarms raised during the day.

**EIGHT claims were withdrawn on 2026-08-05.** Each was reasoned from code shape or a pooled query
rather than read from the authoritative source:

| # | Claim | Killed by |
|---|---|---|
| 1 | `min_bars = 135` blocked GTE | an explicit ATR carve-out sits below it; the constant belongs to a strategy v2 does not run |
| 2 | GTE's ATR trail was under-seeded | same flip on the full 1,212-bar warmup and the 300-bar deque tail |
| 3 | the volume floor blocked the flip bar | GTE 76,069 / BJDX 214,530, both far above 10,000 |
| 4 | warmup history was fetched and discarded | warmup bars are deliberately not persisted (`PERSIST_BAR_AGE_LIMIT_SECONDS`) |
| 5 | deploy cadence was the daily cleanup | the clear-down held through 06-24→06-30 and 07-01→07-07 with no deploys |
| 6 | FUSE `3/2` is a live cap breach | `cw_entries_this_flip` is a LABEL; the 3 came from replay increments that emitted no order |
| 7 | **a stale arm swallowed BJDX's 07:30 flip (D2)** | that flip does not exist — an UNSLICED oracle artifact. BJDX armed 08:50:02 on the real 08:49 flip |
| 8 | **GTE was a double entry / 4 shares held** | broker truth said 2. Summed fills instead of reading `account_positions` |

Two more were caught *before* acting: `%hard_stop%` reporting "385/394 on the guard path" (truth:
1/394), and the A2/A3 shared-root hypothesis, which the data rejected.

**⇒ THE CASE FOR THIS WORK IS NOT "IT IS BLEEDING MONEY."** It is: **the books and the broker
disagree, and size cannot be scaled until they don't.** Overstating it once costs more credibility
than the whole board is worth.

### What survives with evidence

* **24 blocked hard-stop episodes over ~12 days, 4 of which never closed the same day.** Real,
  measured, and small at qty 2. This is the strongest money-linked finding on the board.
* **Books-vs-broker divergence, fresh instance dated 2026-08-05.** GTE's 14:54 Schwab lot @ 9.655
  exited with **no `fills` row and no trade record**, while `account_positions` showed only the
  later 9.37 lot — the OCO exit-fill blackout class (#565/#566). `[OMS-OCO-EXIT-MISS]
  reason=broker_reported_no_filled_exit_leg` repeated for ~20 min against it. Cost if it tracked
  its Webull twin (−4.92%): **≈ $0.95.** The defect is real; the loss is not the point.
* **Session state never clears** (v2 absent from the 04:00-ET roll) — fixed in #657, deployed dark.
* **Two Ship 1 pager defects**, both found and fixed 2026-08-05: false urgents on a filled-then-
  rejected exit, and a dedupe key that collapsed every symbol into one (INLF's page was silently
  swallowed by JLHL's). Noise and a missed page — no money.

⛔ **Do not let a clean board read as a validated one.** Where a fix is unexercised rather than
proven, say which. See §8 of `v2-session-roll-and-replay-arm-design.md`.

---

## ✅ WHAT SHIPPED 2026-08-04 (attended, after close)

| | |
|---|---|
| **Gate 0.5 — #647** | deployed. **All flags OFF** — code only, no behaviour change |
| **`EOD_OCO_TRANSITION_ENABLED=false`** | ⚠️ **a BEHAVIOUR CHANGE, operator-directed, separate from the #647 rollout** — disarms the 16:00 jam trigger. The jam mechanism is untouched |
| **Flags written EXPLICITLY** | all four now in the env, so every kill is a flip, never an append (the P0a lesson, applied to P0a) |
| **#366** | ⛔ **STRUCK** — already deployed AND enabled (control has carried the throttle since Jul 14) and `dashboard_snapshots` is still 103 MB and growing. **An investigation, not a deploy** |
| **A7 reject alarm** | live, `*/15`, read-only |
| **EOD counts** | fired 18:05:30; **10 Schwab entries**, not the 2 a mid-session read showed |
| **Bar backfill** | 276 bars, `source='rest'`. ⭐ AMIX's 54 "missing" were **11 LULD halts**, not data loss |

⛔ **Rollback anchors:** code `71c6c2c` · env `/root/env.bak.2026-08-04-combined`.
⛔ **v2 was NOT restarted and must not be** — Bug 2 re-issues the entry cap on every armed segment.
Pre-flight: `sudo /home/trader/ops_preflight/preflight_v2_restart.sh` (asserts; exit 0 = GO).

---


## 🔴🔴 TOP OF QUEUE — THE REJECT BACKLOG (operator reframe, 08-04)

⛔⭐ **EVERY BROKER REJECTION IS OUR DEFECT.** Tested against the whole population: **zero classes
are market-caused.** The reject log is a ranked, dated defect backlog that had never been read.
⛔ The reason lives in **`broker_order_events.payload->>'reason'`**, NOT `broker_orders.payload`.

**Design note: [`v2-a1-oversell-and-exit-abandonment-design.md`](v2-a1-oversell-and-exit-abandonment-design.md). Nothing built.**
**Alarm LIVE:** `/home/trader/reject_watch/` — `*/15`, ET-guarded 07:00–20:00 (deliberately wider
than the exit watch: the 16:00 jam ran after it had stopped). Real money pages; paper never does.

### The settled numbers (enumerated — never pattern-matched)
| exit rule | episodes | rejects | orders/ep |
|---|---|---|---|
| `CW_HARD_STOP` | **24** | **985** (87%) | 41.0 |
| `CW_FLOOR` | 26 | 114 | 4.4 |
| **`CW_TARGET`** | **0** | **0** | — |

57 episodes total (FLOOR-ONLY 12 · STOP-only 10 · both 14 · neither-CW 21), 28 never closed.
**Worst state: 4 post-#566 episodes where a stop fired and no sell filled that day.**

### Three mechanisms, all new today
1. **A storm needs a PERSISTENT RESERVATION.** Rejects/episode is bimodal (1–6 or 62–129) and
   **stable since 07-13** — the largest predates the fan-out, #625 and #566. ⇒ **#608 caps the noise
   and cannot be the fix. DEMOTED.**
2. **E5 has two sources** — A1a unconfirmed-cancel (~126, KUST) / A1b live protective leg (~242,
   07-13 + 08-04). Double-derived. ⭐ **E5 is DOWNSTREAM of the churn** (KUST: 12 cancelled limits
   carry ZERO oversell rejects; the 125 market rejects are strictly sequential) ⇒ **C1/P0a is NOT
   chasing a symptom; its scope stands.**
3. **MODE 2 — exits are ABANDONED, not just delayed.** FLOOR-ONLY **10 of 12 never closed (83%)**
   vs stops 11 of 24, on 114 rejects vs 985. Cause is a **precondition asymmetry**: the stop's
   condition is *absorbing* (only more true as price falls) and retries forever; the floor's is
   *transient* (armed only while price ≥ target) so it gets ~4 attempts and can never re-arm.
   ⇒ the floor question is a **RATCHET**, connecting to the floor-ratchet item already REOPENED.

⛔ **`CW_TARGET = 0` is the design.** The target IS the resting order and fills in place; floor and
stop **emit a second sell alongside a live protective order**. **Emit-a-second-sell collides;
modify-the-resting-order cannot.**
⛔ **`_cw_flip_pending` = ONE structure, TWO defects** (disarm-on-emit; armed for one account only ⇒
Webull deafness, 27 arms vs 0). Disarm-on-fill fixes neither. Keep separate.
⚠️ **Mode 2's COST is unresolvable for its era** — 14 of 16 floor episodes pre-date #566, where an
OCO close left no `fills` row. Answer prospectively; do not infer. E0's *"exit is essentially
optimal"* carries this unmeasured leak — **not settled, not reopened.**

**Tomorrow, one at a time, validated, attended: A3 (position-state) → A2 (check #438's defer queue
first) → A1(a/b) → A5 → A8.**

---

## 🔴 THE 16:00 EXIT JAM (second)

**Design note: [`v2-eod-oco-jam-design.md`](v2-eod-oco-jam-design.md) (#651, merged). Nothing built.**

**2026-08-04 AAOG, real money: 426 rejected sells in 16 minutes** (`live:orb` 313,
`live:schwab_1m_v2` 113) — worse than the 145-in-55-min incident #608 exists to prevent. **Neither
the bot NOR the operator could sell**; a hand-placed TOS sell was rejected oversold, which is how it
surfaced. Intended stop **−5%**, executed **−5.58% Schwab / −6.01% Webull** ⇒ ~0.6–1.0 pt of pure
slippage per leg. It cleared only when the broker legs **expired** — nothing we did fixed it.

**Two independent defects:**
- **D1** — `_v2_eod_oco_transition` releases the stand-down on a still-held position while the
  broker's OCO legs still **RESERVE** the shares. Its docstring reasons a `session=NORMAL` DAY order
  *"cannot fill in EH, so nothing is lost by letting it lapse."* ⛔ **Cannot FILL ≠ does not
  RESERVE** — the handoff goes to a ladder structurally unable to sell.
- **D2** — the retry bound cannot see a structural block. #608 correctly narrowed the reset to
  **HELD-only**, but HELD *is* this case, so the accumulator resets every pass and the bound of 8 is
  unreachable. The model knows *flat / held / unknown*; it has no **held-but-BLOCKED**. ⭐ The
  discriminator exists and is discarded — the broker returns the reason verbatim and the loop keys
  on position state without ever reading it.

⛔ **NOT NEW, NOT RARE.** The same jam ran **66× on 07-28** unrecognised; the transition flag has
been live since **07-27**; a position is held through 16:00 on **7 of ~20 sessions (~1 in 3)**.
08-04 is merely the first time the **Schwab** leg jammed too.

⇒ **Ship D1 first** — D2 only stops the hammering, D1 restores the invariant. ⚠️ D2 edits the exact
function #608 hardened, so "NCRA's 145-retry case stays bounded" is a real regression risk.
**P1 (the trigger) is now SECOND in the queue.**

---

## 🔬 P1 — THE TRIGGER (third)

**`b117d89` re-shipped the rule of #467**, verbatim and unconditionally: the reactive/reclaim
trigger is `trig = state.cw_segment_high` — the **running segment high**, not the **frozen 2-bar
high** (`cw_trigger`). ⛔ Say "frozen 2-bar high", **never "flip+2"** — that shorthand reads as
flip × 1.02 and misled two readers into benchmarking against 2%.

⭐ **Structural proof, tape-independent — bucket 2 is EMPTY BY CONSTRUCTION.** Both fields seed to
`bars[-1].high` at the flip; `cw_segment_high` takes `max()` on **every** armed bar,
`cw_trigger` only while `cw_bars_waited < 2`, then freezes. `cw_trigger`'s bars are a strict
**subset** ⇒ **`segment_high ≥ cw_trigger` always**, and reactive cannot fire before
`bars_waited ≥ 2`. `max(segment_high, cw_trigger)` would be redundant, not protective. So it is
**#467 scoped to the reclaim slot** — the resting slot still uses the frozen 2-bar high.

**Why it matters regardless of today's tape:** `cw_segment_high` was measured **net-negative** in
the July port, rolled back by **#469 on a void justification** (its byte-identical test fed a break
*at arming*, where `segment_high == cw_trigger` by construction and could not diverge), and
**re-shipped 08-03 as a side effect of a different PR, with no backtest.**

⛔⭐ **AND THE CONVERSE — a normal count at the close is NOT evidence the rule is good.** July's
mechanism was that it *delays entries to a higher price*: that lands in entry **QUALITY**, not
entry **COUNT**. **Measure fill-vs-flip distance regardless of what the count does**, or we accept
a live rule on evidence that cannot see the failure it is meant to rule out — #467's mistake in a
new costume.

**The measurement (after the close, never during RTH — R&D CPU contends with the OMS loop):**
- denominator = **LIVE arms** · both buckets · the **2×2** (price touched the resting level while
  the order was off-book **×** price exceeded `cw_segment_high` — both can be true, so a split
  mis-assigns)
- ⚠️ **combined-commit caveat**: `b117d89` shipped the composition cap **and** the trigger together,
  so a raw before/after estimates the whole commit. Separate them or say "combined".
- ⭐ **natural control**: 07-30→08-03 = flap present, **old** trigger; 08-04+ = flap present, **new**
  trigger. The flap is held constant across the boundary.
- **fill-vs-flip distance as a first-class output.**

⛔ **n=1 settles nothing.** Even a final 12 against a median of 17 sits inside ordinary variance.
The measurement is the **multi-day** before/after.

---

## ⛔⭐ THE STAND-DOWN-CLEAR CONSTRAINT (binds open thread 11)

**Emitting a bracket is NOT sufficient. "OCO ⇒ churn-immune" is false.**

A native OCO bracket makes the BROKER own the exit, so timer-driven cancel/replace structurally
cannot happen — *while the bracket is live*. But when it resolves or stands down,
`[OMS-OCO-STAND-DOWN-CLEARED] ... OCO gone; ladder deferred` hands the exit **back to the bare
timer ladder** — which is KUST, now on a bracketed entry.

**The evidence it is real, not theoretical.** Cancelled/rejected sells within 60 min of an
**OCO-bracketed** entry: **NVVE 07-23 = 11**, KUST 07-22 = 6, FIEE 07-27 = 6, several at 3.
*(Caveat: symbol-level count in a time window; some sells may belong to another position that day.)*

⇒ **The requirement.** On stand-down-clear the exit must either **re-arm a bracket** or **inherit
the P0a marketable-hold** (`_managed_exit_refresh_exempt`, `oms/service.py:3770`). It must **never**
fall back to the bare refresh cadence. Any pre-market-OCO design that does not state which of those
two it does on stand-down-clear is incomplete.

⚠️ P0a alone does not close this: the hold engages only while `limit <= bid`. A bracket that stands
down while the exit is **not** marketable still lands on the plain ladder.

---

## 📜 HISTORY

- **What happened, day by day:** [`handoff-log.md`](handoff-log.md) (append-only)

| archive | covers |
|---|---|
| [`handoff-archive/2026-07.md`](handoff-archive/2026-07.md) | 07-16..07-25 — OCO build, wrong-bars root cause, resting flip-entry, EH trading, fan-out |
| [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) | go-live, #326, OMS exits, ATR qualifier, 04:00 race |
| [`handoff-archive/2026-05.md`](handoff-archive/2026-05.md) | v2 build-out — bar-build, ATR-flip design, exit-engine |
| [`handoff-archive/2026-04.md`](handoff-archive/2026-04.md) | token-SPOF saga, early v2 scaffolding |
| [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md) | the v2 bot's own design/status history |
