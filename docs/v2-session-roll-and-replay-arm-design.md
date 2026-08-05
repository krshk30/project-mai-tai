# v2 session roll + replay-arm decline — design note

**Status:** design only. No code. Nothing ships until the operator approves this note.
**Scope:** the live entry path on the only real-money bot. Attended deploy, after the close.

---

## 0. Provenance (standing rule, new as of 2026-08-05)

> A design note must state the commit it was read against, and that commit must be verified an
> ancestor of deployed HEAD.

| | |
|---|---|
| **Read against** | `786bbb6805caca6626c39f018cc4b468aac010d2` — deployed HEAD on the VPS, branch `main`, 2026-08-04 17:06 |
| **Branch base** | `67215d0` (origin/main). `786bbb6` verified ancestor via `git merge-base --is-ancestor` |
| **Drift in audited files** | **none** — `git diff 786bbb6..67215d0` over the four audited source files is empty (the 4 intervening commits are docs-only) |
| **Working tree at read time** | 2 dirty files, both `ops/health/*.sh`. No `src/` modifications. |

**Why this rule now.** The prior analysis of this exact question was read against
`6ae26511bbed75c78b624e3f419fffdfbec6eea5` on a local branch `codex/local-clean-main`. That commit
**is not a valid object in the deployed repository** — the branch is divergent/unpushed, not merely
behind. Its `strategy_engine_app.py` is 9,720 lines against 10,440 deployed, so every cited line
number was wrong by 20–340 lines. The descriptions happened to survive because that code path
changed little. That was luck, and it was the **fourth** authoritative artifact in one day
describing something that is not running (the others: `docs/schwab-1m-v2-entry-criteria.md`
documenting MACD Momentum v1.32; `journalctl -u schwab-1m-v2` matching a nonexistent unit; the
`capped`/`dangerous` snapshot fields derived from a retired counter).

⛔ **The v2 log sink is `/var/log/project-mai-tai/schwab-1m-v2.log`.** The systemd unit is
`project-mai-tai-schwab-1m-v2`; `journalctl -u schwab-1m-v2` matches nothing and returns a single
header line, so **every grep against it reports a false zero.** Assert a source is non-empty with a
known-present control probe before trusting any zero it returns.

---

## 1. The two defects

This is **two** defects, not one. Fixing either alone leaves the trade loss in place.

| | Defect | Effect |
|---|---|---|
| **D1** | **Accumulation.** v2 is never included in the 04:00 ET session roll, so `_symbol_states` grows without bound and armed segments outlive watchlist membership. | State from days ago persists invisibly. FUSE / HYFM / AXTL armed ~33h on 2026-08-05 while the watchlist read `["BJDX","GTE","YXT"]`. |
| **D2** | **Arm-instead-of-decline on replay.** `_cap_reconstructed_segment` caps the *entry counter* on a segment whose flip predates watch-start, but leaves `cw_armed = True`. | Arming is **edge-triggered**, so the stale armed flag **swallows the next genuine flip**. This is what costs trades. |

⛔ **D1's fix does not prevent D2's loss.** The 04:00 purge is undone within minutes by the next
re-confirm. Today's sequence, from the logs:

```
08:00:00,916 UTC  strategy.log  scanner session-roll fired | prev=2026-08-04T08:00Z new=2026-08-05T08:00Z
08:00:01,006 UTC  schwab-1m-v2  watchlist updated count=0 sample= warmed=0      <- purge equivalent, 90ms later
08:00:01,232 UTC  schwab-1m-v2  [V2-WS-SUB] cmd=UNSUBS count=5 sample=AMIX,BJDX,CDTG,XGN,ZJYL
08:07:25,049 UTC  schwab-1m-v2  [V2-CW-SEED-CAP] BJDX reconstructed armed segment capped —
                                the flip predates our watch (entries->2,
                                arm_bar_ts=1785883440000, watch_start=1785917245007, stage=db-seed)
11:30    (07:30 ET)             BJDX genuine BUY flip, close 1.3900, volume 214,530 (floor 10,000)
                                => cw_armed ALREADY True => NO fresh arm => NO trade
```

ZJYL is the same shape at 09:50:12 UTC. A purge at 04:00 that is re-populated at 04:07 changes
nothing about 07:30.

---

## 2. Evidence (observed, not reasoned)

Five explanations for this behaviour were advanced and withdrawn on 2026-08-05 — `min_bars=135`,
ATR trail seeding, warmup retention, deploy cadence, and "the scanner clears everything". Every one
was reasoned from code shape rather than read. The findings below are each a direct read with a
non-empty-source assertion.

**2.1 The roll is real, and it is a clock boundary.**
`current_scanner_session_start_utc()` (`strategy_engine_app.py:201`) pins the session to **04:00 ET**,
rolling back a day before that hour. `_roll_scanner_session_if_needed()` (`:5573`) fires once per
boundary and genuinely resets: `confirmed_scanner.reset()`, `alert_engine.reset()`,
`top_gainers_tracker.reset()`, empties `all_confirmed` / `current_confirmed` / `retained_watchlist`,
then calls `purge_non_protected_session_state()` on **every bot in `self.bots`**.

**2.2 The page is a session-scoped view — the clear-down there is cosmetic.**
`_snapshot_matches_current_scanner_session()` (`control_plane.py:285`) keeps a snapshot only when its
`scanner_session_start_utc` marker `== session_start`. Prior-session rows stop matching at 04:00 ET
and the page renders empty **with nothing cleared**. Because 2.1 and 2.2 pivot on the same instant,
they have always looked like a single event.

**2.3 v2 is not in `self.bots`.**
`schwab_1m_v2` appears **zero times** in `strategy_engine_app.py` (control: 10,440 lines; the single
case-insensitive match is an unrelated comment about try/except isolation). v2 runs as its own
systemd unit and is not reachable by the engine's `for bot in self.bots.values()` loop.

**2.4 v2 implements none of the engine's bot interface.**
Every attribute the engine calls on a bot object — 29 of them, including the 15 optional
`getattr(bot, ...)` forms that silently no-op when absent — is **absent from both** v2 files
(control probes: 31 `def` in the bot file, 45 in the strategy file; greps live).

```
_roll_day_if_needed · purge_non_protected_session_state · discard_watchlist_symbols
refresh_lifecycle · set_prewarm_symbols · set_broker_blocked_symbols · set_manual_stop_symbols
flush_completed_bars · flush_pending_persists · handle_live_bar · handle_quote_tick
stream_symbols · trade_tick_service · builder_manager · definition · active_symbols
set_watchlist · update_market_snapshots · update_candidates · set_entry_blocked_symbols
seed_bars · required_history_bars · rebuild_indicator_state · monitor_completed_bar_flow
handle_trade_tick · apply_order_status · apply_execution_fill · use_live_aggregate_bars
_symbol_requires_feed
```

⛔ **Do not read this as "29 missing methods."** v2 is a **parallel implementation under its own
names**, not a partial migration — it plainly does handle bars, quotes and watchlists. A
name-by-name diff over a process boundary produces a 29/29 false alarm. The correct question is
which *behaviours* have no v2 counterpart, and on that basis the answer is narrow and specific:

| Engine behaviour | v2 counterpart | Verdict |
|---|---|---|
| `_symbol_requires_feed` (protect open positions/in-flight from purge) | `_protected_symbols` (`schwab_1m_v2_bot.py:1325`) | **exists** |
| watchlist rebuild from handoff | `_apply_strategy_state_event` (`:1112`) | **exists** |
| bar / quote ingestion | `_handle_bar` (`:1649`), `_handle_quote` (`:1696`) | **exists** |
| session-boundary computation | `_current_scanner_session_start_utc` (`:172`) | **exists** |
| **acting on the session boundary** | — | ⛔ **ABSENT** |
| **evicting a symbol's strategy state** | `drop_symbol` (`schwab_1m_v2.py:576`) | ⛔ **DEAD CODE — 0 call sites, 0 tests** |

**2.5 v2 computes the boundary and never acts on it.** `_current_scanner_session_start_utc` is
referenced at only `:1127` and `:1323`, both **snapshot-currency checks**. The string `session-roll`
or `purge` appears **0 times** in v2's log (control: 7,383 lines).

**2.6 The safety flags are derived from a retired counter.**
`cw_entries_this_flip` is annotated in-code (`schwab_1m_v2.py:214`, `:1588`) as *"kept for
labelling/back-compat; NOT the cap"* — the #644 cap is **composition** (≤1 resting AND ≤1 reclaim).
But `cw_armed_segments()` (`:2033`) computes, at snapshot time:

```python
"capped":    st.cw_entries_this_flip >= max_e,
"dangerous": reconstructed and st.cw_entries_this_flip < max_e,
```

Neither is an event, which is why no `[V2-CW-SEED-CAP]` line exists for FUSE. FUSE reads
`3/2 capped=true dangerous=false` — safe **only because the DB-seed replay incremented the label
counter three times while emitting no order at all** (traced via `[V2-CW-STATE-PROBE]`: 0→2 at
23:01:02, →3 at 23:33:03, with zero intent lines after the boot). Had replay produced 0 or 1, the
identical state would read `dangerous=true`. **The boot-hold release depends on this flag.**

---

## 3. Design

### D1 — session-roll purge, self-fired

v2 implements the **same contract** the engine already defines, rather than a bespoke mechanism:

```
purge_non_protected_session_state() -> set[str]
```

* **Trigger:** self-fired inside v2 on its own `_current_scanner_session_start_utc()` boundary,
  checked on an existing periodic loop. v2 is a separate process and **cannot** be reached by the
  engine's `self.bots` loop — a control-plane signal is the alternative and is rejected below (§6).
* **Carve-out:** built on the existing `_protected_symbols()` (`:1325`). A symbol with an open
  position or in-flight open/close **must survive the purge** — this is the #580 orphan constraint,
  and the engine's own docstring calls the equivalent carve-out load-bearing.
* **Action on non-protected symbols:** `drop_symbol()` (wiring the existing dead method), removing
  the entry from `_symbol_states` so `cw_armed`, the deque and the ATR trail all go with it.
* **Idempotent**, safe to call when there is nothing to purge, returns the purged set for logging.

**Observability (mandatory).** Emit a single line naming the count and the protected carve-out, e.g.
`[V2-SESSION-ROLL] purged=N protected=M symbols=...`. A purge that leaves no trace is
indistinguishable from a purge that never fired — the exact failure this note exists to correct.

### D2 — decline the replayed arm instead of arming and capping

`_cap_reconstructed_segment` (`schwab_1m_v2_bot.py:1486`) currently sets
`st.cw_entries_this_flip = max_e` and leaves `cw_armed = True`. **A flip that predates watch-start
should not arm at all.**

* Replace cap-after-arm with **decline-at-arm**: when `0 < st.cw_arm_bar_ts <= watch_start`, end the
  segment through the **existing disarm path** so all of `cw_armed`, `cw_arm_bar_ts`,
  `cw_entries_this_flip`, `cw_resting_taken`, `cw_reclaim_taken` are reset coherently
  (`schwab_1m_v2.py:1412-1420`). Setting `cw_armed = False` alone would leave the rest inconsistent.
* **Log it as a distinct reason** (`reason=seed_predates_watch`) so segment identity stays derivable
  from the ARM/DISARM log — which is the authoritative source, because the resting path writes
  `cw_arm_bar_ts=0` and grouping by `cw_flip_level` is invalid.
* Consequence: a later **genuine** flip on the same symbol finds `cw_armed = False` and arms
  normally. That is precisely the BJDX 07:30 trade this recovers.

⛔ **This changes what the bot trades** — it re-enables entries that are currently suppressed. It is
a behaviour change on the live entry path, not a cleanup, and must be validated as one.

### D3 — intra-session max-age: **measure before building**

A symbol that never leaves the watchlist can still hold a stale arm, which neither D1 nor D2
addresses. A max-age bound is the right shape — but **the threshold must not be invented.**
Required first: the distribution of how long *live, legitimate* segments stay armed before trigger
or disarm, taken from the ARM/DISARM log over ≥20 sessions. A max-age below that distribution
silently cancels valid setups; above it, it never fires. This is the vol-floor-guarding-dead-code
lesson and the Ship-1 floor-threshold lesson in one.

**D3 does not ship with D1/D2.** It is queued behind a measurement.

---

## 4. Why this ordering

D2 alone recovers the lost trades but leaves unbounded state growth. D1 alone provably does not
prevent the loss (§1). **Ship D1 and D2 together**; they touch adjacent code and share validation.

---

## 5. Acceptance criteria

Per the standing rule: **green is not evidence until a deliberate break turns it red.**

1. **D2 — replay under a known-bad tape.** Replay BJDX 2026-08-05 through the live code. Current
   behaviour: seed-cap at 04:07, no arm at 07:30, no entry. Required after the change: **decline at
   04:07, arm at 07:30**. Pin it by mutating the watch-start comparison and confirming the test goes
   red.
2. **D2 — the protective case still holds.** GTE 2026-08-05 must still **not** trade: its only flip
   (07:00 ET) predates its 07:14:47 watch-start and there is no later flip. If GTE starts trading,
   the decline has been turned into a no-op.
3. **D1 — purge with a live position.** A symbol holding an open position at the 04:00 boundary must
   survive with `_symbol_states` intact. This is #580; assert it, do not print it.
4. **D1 — idempotence.** Two consecutive rolls purge nothing the second time.
5. **Counts on corrected denominators.** Live arms only; warmup-replay arms excluded.

---

## 6. Rejected alternatives

* **Control-plane signal to trigger v2's roll.** Adds a cross-process dependency on the entry path
  and a new silent-failure mode (signal not delivered ⇒ no purge, no error). v2 already computes the
  boundary locally; self-firing has no such mode.
* **Registering v2 in `strategy_engine.bots`.** v2 was deliberately isolated (own unit, own DB
  session, own streamer). Re-coupling it to the engine reverses that for one method.
* **Max-age only, no purge.** Leaves `_symbol_states` growing for de-watchlisted symbols forever.
* **Fixing the restart cadence.** There is no scheduled restart and there never was — 117 starts
  since 2026-05-22 with bursts of 6–11 per afternoon and gaps of 4–6 days is deploy churn;
  `Restart=always` is crash-recovery only, `NRestarts=0`, `Result=success`, no timer, no cron. ⛔ A
  scheduled restart would **restore an accidental mask over a latent defect** and is out of scope.

---

## 7. What this design cannot see

* **Whether any *other* engine behaviour has no v2 counterpart.** §2.4 audited the bot interface as
  called by the engine. Behaviours the engine performs *around* the loop were not enumerated.
* **Whether `dangerous=false` is ever correct.** §2.6 shows the flag derives from a retired counter;
  this note does not fix it. Until it does, **the boot-hold release is unvalidated.** Separate item.
* **GTE's warmup gap.** 1,147 bars fetched at 11:14:52 UTC, `[V2-REST-WARMED]` complete, zero
  out-of-order drops — and no arm, no seed-cap, no ingestion evidence. GTE's *outcome* (no trade)
  matches the watch-start design, but the *mechanism* is unaccounted for. **Open.**
* **The composition checker's blind spot.** `/home/trader/entry_fix_watch/check.py` builds every
  segment from a `[V2-CW-ARM]` line and drops replay arms by design; the string `reconstructed` does
  not appear in it. The composition cap is therefore **unvalidated on reconstructed segments**, not
  narrowly validated. If D2 lands, the checker must also learn the new decline reason or those
  segments become invisible in a second way. Separate item, tracked.

---

## 8. Numbers that were wrong first

Recorded so the next reader does not re-derive them.

| Claim | Status |
|---|---|
| `min_bars = macd_slow + macd_signal + settling` (135) blocks GTE | **wrong** — an explicit ATR carve-out sits below it, and the constant belongs to a strategy v2 does not run |
| The ATR trail was under-seeded, so GTE never flipped | **wrong** — the oracle gives the same BUY flip (07:00 ET, vol 76,069) on the full 1,212-bar warmup and on the 300-bar deque tail |
| The volume floor blocked the flip bar | **wrong** — GTE 76,069 and BJDX 214,530, both far above 10,000 |
| Warmup history was fetched and discarded | **wrong for the in-memory path** — warmup bars are deliberately not persisted (`PERSIST_BAR_AGE_LIMIT_SECONDS = 300`), so `strategy_bar_history` was never expected to hold them |
| Deploy cadence was the daily cleanup | **wrong** — fails against full data seen after a Monday-night deploy, and against the clear-down persisting through the 06-24→06-30 and 07-01→07-07 no-deploy stretches |
| FUSE `3/2` is a live cap breach | **wrong** — `capped=true, dangerous=false`; the counter is a label, and the 3 came from replay increments that emitted no order |
| "The engine tells every bot to roll its day" | **true, and inapplicable** — `self.bots` does not contain v2 |
