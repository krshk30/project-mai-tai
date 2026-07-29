# OPEN ITEMS

> Threads that are genuinely **open**. Summarised one line each in
> [`session-handoff.md`](session-handoff.md); this file holds the detail.
>
> **Pruned WITH the operator 2026-07-29** — 47 resolved / superseded items were moved to the bottom
> of [`handoff-log.md`](handoff-log.md) (nothing deleted; git history has the rest). Before the
> prune this file was 309 lines and mixed live threads with things fixed weeks earlier, which made
> it unreadable and therefore unread.
>
> ⛔ **Keep it that way.** When an item closes, MOVE it to the log — do not leave it here marked ✅.
> A file where most entries are closed is a file nobody trusts.

---

## ⚠️ UNVERIFIED — confirm or close (operator call 2026-07-29)
These could not be verified from outside and have not been touched in ~4 weeks of work on adjacent
code. They are kept rather than closed, but **treat them as unproven, not as known-live problems.**

- **`INTENT_MAX_AGE` (30s) kills resting stop-entries** — pinned by live probe 2026-07-15 (F qty1
  abandoned at 34.6s), fix NOT built. ⭐ The constant EXISTS and is buy-only; what is missing is the
  exemption + a lifecycle owner (ORB has no window-close cancel, so the cap is accidentally the only
  thing stopping a resting buy-stop outliving its window). Full detail:
  [[project_mai_tai_false_flat_naked_position]] and the 2026-07-15 entry in the log.
  ⚠️ ORB resting entry is currently **OFF**, so this is dormant, not active.
- **BUG #3 `ORDER_NOT_SUPPORT_REVERSE_OPTION`** — root-caused as #436 Bug A verbatim (Webull
  fill-settlement lag), mitigation #438 exists. **Loose end: no DEFER log line**, so a deferred
  re-arm is invisible. Detail in the 2026-07-15 log entry.
- **Webull `NO_SUCH_TICKER` (universe gap)** — SHPH rejected 2026-06-26; Webull did not recognise
  the ticker. Unknown whether the universe still diverges.
- **OMS record-desync** — `broker_order_events` missing sell-fills ("record state on FILL not
  submit"). May be superseded by the fill-gating in #392; not confirmed.
- **"What else does the dead ladder write?"** — standing audit from 2026-07-16, before trusting any
  ladder-written column as evidence. See [[feedback_fossil_db_columns_trace_read_path]].

---

**🔬 2026-07-29 — FIRST JOB: re-run the backtest-vs-live comparison on a STABLE-CODE day.** 07-28 is
unusable for parity: six deploys mid-session means live ran >=4 code versions while the replay runs
the final one. Config parity itself is FIXED and verified (89/90, #592) — what is still unproven is
whether the ENGINE reproduces a live day end-to-end. Only STKH matched so far (+1.90% both).

⛔ When comparing, respect the three structural limits: the replay takes **ONE round trip per
symbol-day** (`if exit_done: break` — so "1 replay vs 6 live" is expected, compare the FIRST live
trade only) · quote density ~1/4s vs a continuous live feed · sparse-bar symbols (CNET: 71 bars,
1 quote/118s) are uncomparable. Scripts on the box: `_parity_diff.py`, `_live_today.py`,
`_cnet_probe.py`, `_density.py`. [[project_mai_tai_backtest_live_parity_audit]]

**🔴 2026-07-28 — OPEN, with evidence attached (none of these are fixed):**

- **A Webull 429 can still lose an exit fill after the bound.** #585 retries up to
  `_MAX_EXIT_FETCH_DEFERRALS=3` (~45s) and then closes the row anyway, deliberately (an open managed
  row blocks fan-out re-entry). Trades whose fetch fails for >45s stay unpaired.

- **Only ONE exit per entry order was recordable before #585.** Fixed forward, but 07-27 history is
  permanently short: the backfill recovered **4 of 8** exits and the rest are unrecoverable.

- **36% of in-window BUY flips never armed on 07-28** (8 of 22; ENTX 4/4, CNET 2/3). ⛔ NOT the
  cooldown (CNET logged `pos_qty=0 cooldown=0`) — it is watchlist ABSENCE (2-5 symbols live while the
  cap is 25; the confirmed set itself churns). ⚠️ The number is NOT trustworthy yet: the windows come
  from `scanner_confirmed_events`, which #582 fixes FORWARD ONLY. **Re-run on a clean day.**

- **Hand-cancelling at the broker does NOT stop the Webull fan-out leg** (it fires from a software
  price-cross detector). Procedure: hand-cancel **AND** set `global_manual_stop_symbols`.
  [[feedback_hand_cancel_needs_the_manual_stop_lever]]

- **⛔ Three settings where the CODE DEFAULT DISAGREES WITH PRODUCTION** — vol floor (5000 vs live
  10000, now aligned), reclaim gap (0 vs live 1), entry cap (flag-derived). **Check the ENV before
  quoting any default as the live value.** Worth a deliberate sweep of the whole env-vs-default set.

- **P2 measurements still not run:** entry-quality study re-sourced onto DB reject reasons, and the
  gap-through caps (`ASK_PAST_BAND` / `ASK_PAST_CROSS_CAP`).
  ⛔ **Reclaim-trigger is NOT measurable the obvious way** — `entry_price` and `cw_flip_level` are
  written from the SAME variable on the resting path, so "premium over flip level" is 0.00% by
  construction. Use the FILL price vs `cw_segment_high`. [[project_mai_tai_after_close_batch_0728]]

**🏛️ 2026-07-17 — ARCHITECTURAL: the INJECTION SEAM is a convention, not an invariant → DEFAULT FLIPS
NEED THE FULL SUITE.** `settings.py:~1064` is `@lru_cache def get_settings(): return Settings()` — a
process-global bare Settings (defaults + env), consumed as **`settings or get_settings()` across ~15 sites**
(oms, orb, v2, market-data, control-plane, strategy-engine, reconciler, market-capture, trade-coach,
runtime-seed…). ⇒ **any code path can silently fall back to a global nobody injected.** Discovered flipping
`strategy_macd_30s_enabled`'s default: 68 tests broke because a scanner/dashboard path reads `get_settings()`
(sees the DEFAULT) while the test service injected the other way — a SPLIT (reverting only the default with
the fixture kept made the 68 vanish). **LIVE is inert** (env sets the field on every service, so
global==injected) — but that is luck of *which fields have env overrides*, not a closed seam. **Every
default-flip PR must run the FULL `tests/unit` suite (not one file) and expect a global-read split.**
**Fix (own workstream, NOT behind a paper bot): make the injected-settings contract an invariant — the
fallback raises, or the ~15 global readers take injected settings.** [[feedback_mutate_the_code_pin_the_threshold]]

**⚠️ 2026-07-17 — LABELLED LIMITATION (not a bug, likely UNRESOLVABLE — an acceptable answer): ON A SHARED
BROKER ACCOUNT, OUR POSITION LEDGER IS NOT GROUND TRUTH.** Surfaced by the NXTC 07-14 correction: our `fills`
said we held 2; Schwab rejected a sell of 2 as **oversold**; nothing of ours reserved the shares. The
operator hand-trades the same Schwab account, so a manual sale is invisible to us **by construction**. **This
is the [[project_mai_tai_oms_scoping_invariant]] WORKING, not failing** — the OMS acts only on positions it
placed and clamps sells to its own ledger, so a manual trade structurally cannot be sold by the bot; the cost
is that our ledger can be **stale-high** and the symptom is a **bounded, loud** oversold reject. **⇒ Do NOT
"fix" this. Do NOT reconcile our ledger against broker positions on a shared account** (that is the ERNA path
— a broker read that says flat deletes our protection; #464 exists because of it). **DO** read an unexplained
oversold reject as *"the operator may have traded this name"* before suspecting the exit path, and **never
cite a shared-account symptom as evidence for a code bug** — that is how NXTC carried the P0.3 blocker three days.

**⛔ 2026-07-16 — STANDING AUDIT: what else does the dead ladder write? (before trusting it as evidence).**
`oms_managed_positions.floor_pct`/`floor_price` are **FOSSILS** — written every row by the DEAD tiered
ladder inside the position object (`update_price`→`_persist_v2_price_state`), NEVER read by the live
`cw_exit_decision` (live floor = config +2%). Fully populated + model-consistent ⇒ looked like ground
truth ⇒ nearly voided P4.1 (false alarm). **Before using ANY position-object column as evidence, trace its
READ path in the live code.** Not yet audited: `tier`, `scales_done`, `scale_pnl`, `current_profit_pct`.
(`peak_profit_pct` PASSED — bid-based record, floor arms at exactly `entry×1.015`.)
[[feedback_fossil_db_columns_trace_read_path]]

- **v2 CW-v2 forward test** — continue collecting name-days (NXTC 07-14 = first 2 wins); watch flip-exit slippage (exits filled a hair under the idealized +2% targets).

**📌 SAVED STUDY — [`v2-exit-floor-ratchet-study-2026-07-15.md`](v2-exit-floor-ratchet-study-2026-07-15.md) (operator asked for this to be keepable + re-runnable; NOTHING was changed).** Does ratcheting the +2% floor up as price rises earn free money? **Yes to free, yes to earns, small.** 50 trades / 35 winners, 07-09..07-14, qty 2: **LIVE pinned +2% = +$4.70** · **operator's `max(2%, int(peak))` (fires at +3%) = +$4.74 (+$0.04)** · `peak−0.50%` +$4.77 · `peak−0.25%` +$4.83 · **`peak−0.10%` = +$5.03 (+$0.33)**. Every mode is `max(2%, …)` ⇒ **can never book below +2% — free by construction**, confirmed on every winner; finer trails pay monotonically more. **Why small:** 34/35 winners peak **+2.01%..+2.43%** before the pullback that exits them; exactly ONE exceeds +3%. **⭐ The tails are real but unreachable by ANY pullback exit** (VEEE +96%, SOBR +44%, LEDS +26% available — all taken at +2%): the dip ALWAYS precedes the run (room-variant +$3.05, pure-ride **−$23.18**) ⇒ **the lever is RE-ENTRY after the shakeout, not the floor.** **⚠️ Two cautions recorded there:** (1) the first run reported the operator's mechanic as "$0.00, never fires" — it had tested `int(peak)−1%` (needs +4%) not `int(peak)` (needs +3%); **the operator rejected it on instinct and was right.** (2) **`/home/trader/wt-atr-ab/atr_cw_v2_variants.py` CANNOT IMPORT** (`cw_ratchet_exit_decision` has never existed in any commit — `git log -S --all` empty) ⇒ **the 07-14 trailing-floor numbers below are NOT reproducible**; left as-is per the operator (live == backtest, don't touch). **⭐ LIVE REFERENCE CASE RECORDED — KUST 2026-07-15 (real money, qty 2): the ratchet's scenario, observed.** Entry **1.4999** 08:45 → armed the +2% floor (1.5299) at 09:16:46 on bid 1.53 → rode **1.54 → 1.55 (+3.34% peak)** → bid fell back → `CW_FLOOR ref=1.5299` → **fill 1.5201 = +1.35%**. It gave back the whole round trip because the floor is PINNED at +2% and never moves. **The operator's rule fires here:** peak +3.34% ⇒ `int(3.34)=3` ⇒ floor +3% (1.5449) ⇒ exit at the next tick **1.54 = +2.67%** — **+1.32 pts, ~double the trade**. Also a **31-min slow mover** ⇒ the 'slow movers are where it pays' claim is vindicated. **🆕 TICK-GRID FINDING:** on a $1.50 stock 1¢ = **0.67%**, so the 1.5299 floor sits BETWEEN the 1.52/1.53 ticks and is **skipped** — the '+2% floor' is really a **+1.35% floor**; #453 flipped us from filling the tick ABOVE +2% (old hard target, 1.53) to the tick BELOW (1.52), a ~1.3-pt swing per winner the backtest cannot see (it books both at 1.5299). Trail granularity finer than a tick is moot on sub-$2 names (B==G on KUST). **Operator 07-15: record it, implement decision DEFERRED — nothing changed.** Re-run: `scripts/legacy/floor_sweep_2026_07_15.py` (raw output + the KUST tape archived in `docs/research-output/`).

- **OMS record-desync** ("record state on FILL not submit" — `broker_order_events` missing sell-fills; JEM's auto-reconciled on tonight's OMS restart but the class remains). + **#386 `STOP_PRICE_MUST_BE_LESS_THAN_MARKET` handling** (real, NOT the zombie cause).

- **📄 ORB resting-bracket entry — DESIGN DOC written** ([`orb-resting-bracket-entry-design.md`](orb-resting-bracket-entry-design.md)): replace the bar-close "buy-back-at-broken-level" (a de-facto pullback that gappers never fill — CELZ's fillable window was **114ms**) with a **resting native OTOCO bracket** at ~9:25 (buy-stop entry + attached stop, stop live AT fill, never naked). **Q1 confirmed: Webull v3 OpenAPI has native combos (`OTO`/`OCO`/`OTOCO`/`STOP_LOSS_PROFIT`), US-supported** — but our adapter uses the single-leg path (new v3-combo adapter work) and only a SELL `STOP_LOSS` is verified. **GATE = STEP 1: a far-from-market OTOCO validation test (like the F test) confirming our account accepts the combo shape AND the attached stop goes live at fill — BEFORE any real ORB use.** Sequenced AFTER #387 + the phantom fix (the 2-entry cap must count fills, not emits).

- **🟡 Webull `NO_SUCH_TICKER` (universe gap) — SHPH rejected 2026-06-26 9:31.** `buy LIMIT` rejected: Webull doesn't recognize the ticker (`instrument_id` resolution gap or symbol not in Webull's tradeable universe). Harmless (no fill, no position) but a missed entry → ORB's Webull-tradeable universe is narrower than the scanner surfaces (cf. the Schwab API-open restriction on v2). Only 1 symbol today. Decide lookup-bug-vs-non-listing; if non-listing, evict like `schwab_ineligible`. Tracked in the design doc "Out of scope" section.

- **✅ ORB Piece-1 (OMS-quote-priced entry) — VALIDATED LIVE 2026-06-26.** First real-money quote-priced fills: IVF (entry 1.8569, `[OMS-ORB-QUOTE-PRICED] ask=1.86 break=1.90 bound=1.9285 limit=1.87`) + SDOT (entry, `ask=12.27 break=12.24 bound=12.4236 limit=12.28`) — both repriced at placement off the live quote and **filled, NO `QUOTE_DRIFT_CANCEL`** (yesterday's AZI bug is fixed). 2 fills + 1 `NO_SUCH_TICKER` reject (SHPH); closed flat. Net per-trade ~scratch (IVF ≈ −$0.18); the mechanism is the validation, not the P&L. Flag stays ON.

- **#366 snapshot-persist throttle — ATTENDED close-deploy + validate** (enable `snapshot_persist_throttle_secs`, re-arm the #350 py-spy capture at a 16:00 ET close, confirm snapshot gaps <50s). Then decide #350 **piece #2 (offload) / #3 (encode-once)** from the re-capture. **PRs #365 (design) / #366 (throttle)**.
