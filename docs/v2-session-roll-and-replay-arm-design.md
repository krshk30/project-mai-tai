# v2 session roll — the bar-driven reset's blind spot

**Status:** built 2026-08-05. Flag-gated, default OFF. Attended deploy after the close.
**Scope:** the live entry path on the only real-money bot.

---

## 0. Provenance (standing rule, new as of 2026-08-05)

> A design note must state the commit it was read against, and that commit must be verified an
> ancestor of deployed HEAD.

| | |
|---|---|
| **Read against** | `786bbb6805caca6626c39f018cc4b468aac010d2` — deployed HEAD, branch `main`, 2026-08-04 17:06 |
| **Branch base** | `67215d0` (origin/main). `786bbb6` verified ancestor; the 4 intervening commits are docs-only |
| **Drift in audited files** | **none** — `git diff 786bbb6..67215d0` over the four audited source files is empty |
| **Working tree at read time** | 2 dirty files, both `ops/health/*.sh`. No `src/` modifications |

**Why this rule now.** The prior analysis of this question was read against
`6ae26511bbed75c78b624e3f419fffdfbec6eea5` on a local branch `codex/local-clean-main`. That commit
**is not a valid object in the deployed repository** — divergent/unpushed, not merely behind. Its
`strategy_engine_app.py` is 9,720 lines against 10,440 deployed, so every cited line number was
wrong. The descriptions survived by luck. That was the **fourth** authoritative artifact in one day
describing something not running (also: `docs/schwab-1m-v2-entry-criteria.md` documenting MACD
Momentum v1.32; `journalctl -u schwab-1m-v2` matching a nonexistent unit; the `capped`/`dangerous`
snapshot fields derived from a retired counter).

⛔ **The v2 log sink is `/var/log/project-mai-tai/schwab-1m-v2.log`.** The unit is
`project-mai-tai-schwab-1m-v2`; `journalctl -u schwab-1m-v2` matches nothing and returns a header
line, so **every grep against it reports a false zero**. Assert a source is non-empty with a
known-present control probe before trusting any zero it returns.

---

## 1. The defect

The 04:00 ET session reset **already exists and is already correct**. `_update_atr_state` clears the
entire ATR trail *and* `cw_armed`, `cw_entries_this_flip`, `resting_active` whenever a bar's anchor
differs from the stored one.

**It is BAR-driven.** `if anchor != state.atr_session_anchor_ms` is only ever *evaluated* when a bar
arrives. A symbol that leaves the watchlist stops receiving bars, so **its reset never fires**.

```
2026-08-05 live snapshot
  watchlist:           ["BJDX","GTE","YXT"]
  cw_armed_segments:   FUSE  arm_bar_ts 1785797580000   armed since 08-03  (~33h)
                       HYFM  arm_bar_ts 1785801420000   armed since 08-03
                       AXTL  arm_bar_ts 1785798360000   armed since 08-03
```

Every **watchlisted** symbol self-cleared correctly. Only the silent ones rotted. That asymmetry is
the whole bug, and it is invisible on the page — the page renders the watchlist, and these symbols
had left it.

**Two parts, different justifications. Do not couple them:**

| | | |
|---|---|---|
| **D1a** | **Time-driven boundary reset** | trade-relevant · **built here** |
| **D1b** | **Evict `_symbol_states` on watchlist exit** (`drop_symbol` is dead code — 0 call sites, 0 tests) | resource concern, not a trade concern · **deferred** |

## 1b. ⛔ D2 (decline-on-replay) — PROPOSED AND WITHDRAWN

An earlier draft proposed a second defect: `_cap_reconstructed_segment` caps the entry *counter* on
a replayed flip predating watch-start but leaves `cw_armed=True`, so the stale flag would swallow
the next genuine flip. **This was withdrawn before any code was written.** It is wrong twice:

1. **No measured cost.** The claimed victim was BJDX's "07:30 ET flip". That flip does not exist in
   what the bot computes — it was an artifact of running the ATR oracle over an **unsliced,
   multi-day** series. Live slices at the 04:00 ET anchor (§4). On the real slice BJDX flips at
   **08:49** and the bot armed at **08:50:02**. Nothing was lost.
2. **Transient anyway.** The anchor reset clears `cw_armed` on the first new-session bar, which is
   exactly why BJDX was *absent* from `cw_armed_segments` while FUSE/HYFM/AXTL were present.

Adding an entry-path change with no demonstrated loss is the pattern this week has been spent
correcting. Operator's directive: **measured cost or it doesn't ship.**

## 2. Evidence

Five explanations were advanced and withdrawn on 2026-08-05 (§8). The findings below are each a
direct read with a non-empty-source assertion.

**2.1 The roll is real and it is a clock boundary.** `current_scanner_session_start_utc()`
(`strategy_engine_app.py:201`) pins the session to **04:00 ET**.
`_roll_scanner_session_if_needed()` (`:5573`) fires once per boundary, resets the scanner, and calls
`purge_non_protected_session_state()` on every bot in `self.bots`.

**2.2 The page is a session-scoped view — its clear-down is cosmetic.**
`_snapshot_matches_current_scanner_session()` (`control_plane.py:285`) keeps a snapshot only when
its `scanner_session_start_utc` marker `== session_start`. Prior-session rows stop matching at
04:00 ET and the page renders empty **with nothing cleared**. 2.1 and 2.2 pivot on the same instant,
so they have always looked like one event.

**2.3 v2 is not in `self.bots`.** `schwab_1m_v2` appears **zero times** in `strategy_engine_app.py`
(control: 10,440 lines; the single case-insensitive match is an unrelated comment).

**2.4 v2 implements none of the engine's bot interface — and that is NOT the finding.** All 29
attributes the engine calls on a bot are absent from both v2 files. ⛔ **Do not read this as "29
missing methods."** v2 is a **parallel implementation under its own names**, not a partial
migration; a name-by-name diff across a process boundary manufactures a false alarm. The behaviour
map is narrow:

| Engine behaviour | v2 counterpart | |
|---|---|---|
| protect open positions from purge | `_protected_symbols` (`bot:1325`) | exists |
| watchlist rebuild from handoff | `_apply_strategy_state_event` (`bot:1112`) | exists |
| bar / quote ingestion | `_handle_bar` (`bot:1649`), `_handle_quote` (`bot:1696`) | exists |
| session-boundary computation | `_current_scanner_session_start_utc` (`bot:172`) | exists |
| **acting on the boundary** | — | ⛔ **absent — this is D1a** |
| evicting a symbol's state | `drop_symbol` (`strategy:576`) | ⛔ dead code — D1b |

**2.5 v2 computes the boundary and never acts on it.** `_current_scanner_session_start_utc` is
referenced only at `bot:1127` and `bot:1323`, both snapshot-currency checks. `session-roll` and
`purge` appear **0 times** in v2's log (control: 7,383 lines).

**2.6 Proof the two mechanisms are distinct**, 90 ms apart:

```
08:00:00,916 UTC  strategy.log  scanner session-roll fired | prev=2026-08-04T08:00Z new=2026-08-05T08:00Z
08:00:01,006 UTC  schwab-1m-v2  watchlist updated count=0 sample= warmed=0
08:00:01,232 UTC  schwab-1m-v2  [V2-WS-SUB] cmd=UNSUBS count=5 sample=AMIX,BJDX,CDTG,XGN,ZJYL
```

v2's **watchlist** emptied because the upstream handoff emptied — not because anything purged v2.
`_symbol_states` behind it still held FUSE/HYFM/AXTL.

## 3. What was built

**Strategy** (`strategy_core/schwab_1m_v2.py`)

* `_apply_session_anchor_reset(state, anchor)` — the reset block **extracted verbatim** from
  `_update_atr_state`. Both drivers now call it, so they cannot drift. A drift here would be
  silent: some fields cleared on one path and not the other, leaving a segment that reads armed
  with no trail.
* `roll_stale_session_state(now_ms, *, is_protected) -> list[str]` — the time-driven half. Guard is
  `0 < atr_session_anchor_ms < anchor`: the symbol's newest bar belongs to an **earlier** session.
  `anchor_ms == 0` (no bar ever) is left alone rather than fabricating a session it never had.
* `cw_armed_segments()` gains **`arm_age_secs`** and **`stale_session`**. Two epoch integers make a
  33-hour arm and a 3-minute arm look identical; age does not. `stale_session` derives from the
  session anchor, **not** the entry counter, so replay cannot inflate it (unlike `capped`).

**Bot** (`services/schwab_1m_v2_bot.py`)

* `_roll_stale_session_state(positions, held)` on the existing 5s position poll — **no new task**,
  so no new liveness surface.
* ⛔ **The carve-out is wider than `_protected_symbols()`.** That helper answers a *different*
  question — "which symbols must stay on the watchlist" — and sees positions only. The reset also
  clears `resting_active`, and clearing that while a buy-stop is **working at the broker** orphans
  the order: #580's latch race, where losing it once means it never reprices again. So the sweep
  carries its own predicate (`operator-protected ∪ positions ∪ held ∪ resting_active`) and
  `_protected_symbols()` keeps its meaning untouched.
* A symbol **mid-warmup is skipped, not protected** — warmup replays historical bars whose anchors
  are legitimately older and the bar-driven path owns it; rolling underneath would reset the trail
  mid-series. A **warmed, watchlisted, silent** symbol (halted/illiquid) **does** roll.

**Flag:** `strategy_schwab_1m_v2_session_time_roll_enabled`, default **False** → the sweep returns
immediately and the bar-driven path is untouched (byte-identical).

## 4. ⭐ The 04:00 ET session slice (generalises well beyond this change)

The ATR trail is **rebuilt from scratch at 04:00 ET daily**, matching the validated session-sliced
backtest. TOS computes the same indicator **continuously across sessions**. Same tape, same
ATRPeriod=5 / ATRFactor=3.5 / Wilders — **different answer**, because the indicator is
path-dependent.

| Symbol | unsliced multi-day run | live 04:00-ET slice | bot's actual arm |
|---|---|---|---|
| GTE | flip 07:00 ET | **BUY 09:01 ET** | `[V2-CW-ARM]` 09:02:02 ✅ |
| BJDX | flip 07:30 ET | **BUY 08:49 ET** | `[V2-CW-ARM]` 08:50:02 ✅ |

**Live and the backtest agree; the chart is the outlier.** This likely explains a whole class of
"I saw a setup and we didn't take it" reports. Whether daily re-seeding is right for **pre-market**
is a strategy question on the parked list — flagged, not built.

## 5. Acceptance

⚠️ **The trap:** the deploy restarts v2, which clears FUSE/HYFM/AXTL **regardless of whether this
code works**. A clean `cw_armed_segments` after the deploy proves **nothing**. Hence:

| | Test | Result |
|---|---|---|
| 1 | Stale armed symbol, **no bar arriving**, crossed over the boundary → `cw_armed=False` | ✅ `test_a_stale_armed_symbol_rolls_with_NO_bar_arriving` |
| 2 | Open position survives | ✅ `test_a_symbol_with_an_open_position_survives_the_roll` |
| 3 | **Working resting order survives (#580)** | ✅ `test_a_working_RESTING_ORDER_survives_the_roll` |
| 4 | Mid-warmup skipped; warmed-but-silent rolls | ✅ 2 tests |
| 5 | Flag OFF byte-identical | ✅ `test_flag_off_is_byte_identical` |
| 6 | Both drivers produce the same reset, field by field | ✅ `test_both_drivers_produce_the_same_reset` |
| 7 | Bar-driven path unchanged by the extraction | ✅ `test_bar_driven_reset_still_clears_at_the_anchor` |

**Mutations (green is not evidence until a deliberate break turns it red):**

```
MUTATION A  remove the time trigger      -> 3 RED (incl. the stale-symbol test)   reverted
MUTATION B  remove the protection check  -> 4 RED (position, resting, warmup, operator)  reverted
```

**Regression:** 255 passed (`-k "schwab_1m_v2 or cw or atr or oracle or exit_logic"`), 30 passed
(`tests/backtest`, includes the ATR-oracle parity pin), ruff clean.

## 6. Rejected alternatives

* **Control-plane signal to trigger v2's roll** — cross-process dependency on the entry path plus a
  new silent-failure mode (signal not delivered ⇒ no purge, no error). v2 computes the boundary
  locally; self-firing has no such mode.
* **Registering v2 in `strategy_engine.bots`** — reverses a deliberate isolation for one method.
* **Reusing `_protected_symbols()` for the sweep** — it does not cover `resting_active` (§3).
* **Fixing the restart cadence** — there is no scheduled restart and never was: 117 starts since
  2026-05-22, bursts of 6–11 per afternoon, gaps of 4–6 days; `Restart=always` is crash-recovery
  only, `NRestarts=0`, `Result=success`, no timer, no cron. ⛔ A scheduled restart would **restore
  an accidental mask over a latent defect.**
* **D3 intra-session max-age** — deferred. Needs the distribution of how long *legitimate* live
  segments stay armed, over ≥20 sessions. An invented threshold either cancels valid setups or
  never fires (the vol-floor and Ship-1-floor lesson).

## 7. What this design cannot see

* **Whether `dangerous=false` is ever meaningful.** It derives from the retired
  `cw_entries_this_flip`; not fixed here. Until it is, **the P1.3 boot-hold release is
  unvalidated.** Separate item.
* **GTE's warmup gap.** 1,147 bars fetched, `[V2-REST-WARMED]` complete, zero out-of-order drops —
  and no ingestion evidence. Its *outcome* is now fully explained by §4, but the bar-accounting is
  not. **Open.**
* **The composition checker's blind spot.** `/home/trader/entry_fix_watch/check.py` builds every
  segment from a `[V2-CW-ARM]` line and drops replay arms by design; the string `reconstructed`
  never appears in it. The composition cap is therefore **unvalidated on reconstructed segments**,
  not narrowly validated. Separate item.
* **D1b.** `_symbol_states` still grows without bound for de-watchlisted symbols. Resource
  concern only — their state is now neutralised at each boundary.

## 8. Numbers and claims that were wrong first

Recorded so the next reader does not re-derive them.

| Claim | Status |
|---|---|
| `min_bars = 135` blocks GTE | **wrong** — an ATR carve-out sits below it, and the constant belongs to a strategy v2 does not run |
| The ATR trail was under-seeded, so GTE never flipped | **wrong** — same flip on the full 1,212-bar warmup and the 300-bar deque tail |
| The volume floor blocked the flip bar | **wrong** — GTE 76,069 and BJDX 214,530, both far above 10,000 |
| Warmup history was fetched and discarded | **wrong for the in-memory path** — warmup bars are deliberately not persisted (`PERSIST_BAR_AGE_LIMIT_SECONDS = 300`) |
| Deploy cadence was the daily cleanup | **wrong** — fails against full data seen after a Monday deploy, and against the clear-down persisting through 06-24→06-30 and 07-01→07-07 |
| FUSE `3/2` is a live cap breach | **wrong** — `capped=true, dangerous=false`; the counter is a label, and the 3 came from replay increments that emitted no order |
| "The engine tells every bot to roll its day" | **true, and inapplicable** — `self.bots` does not contain v2 |
| **BJDX's 07:30 flip was swallowed by a stale arm (D2)** | ⛔ **wrong — the flip does not exist.** Produced by an **unsliced** oracle run; live slices at 04:00 ET. BJDX armed at 08:50:02 on the real 08:49 flip |
| **Acceptance test "07:30 flip → assert a fresh arm"** | ⛔ **unsatisfiable** — written off the unsliced output without asking which series produced it. **Same session-slice trap, one step downstream.** Always state the slice when quoting an oracle result |
| "Zero SEED-CAP / zero arms / zero warmup lines" | **false zeros** — queried `journalctl -u schwab-1m-v2`, a unit that does not exist |
