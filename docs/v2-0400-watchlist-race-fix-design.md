# Design: 04:00 ET Session-Boundary Watchlist Race Fix

Status: DESIGN ONLY (no production code changed, no VPS touched). For operator review before any PR.
Date: 2026-06-16
Author: Claude (research/design agent)
Scope file: `src/project_mai_tai/services/strategy_engine_app.py` (+ `strategy_core/runner.py`, `strategy_core/time_utils.py`)

---

## 1. Summary

At the 04:00 ET scanner session boundary, bot watchlists end up STALE = yesterday's symbols
re-promoted onto bot lifecycle state, surviving alongside today's fresh scanner picks. The
operator-traced root cause is an **ordering race inside the `snapshot_batch` stream handler**:
`set_broker_blocked_symbols_by_strategy(...)` (which calls `_resync_bot_watchlists_from_current_confirmed`)
runs BEFORE `process_snapshot_batch(...)` (which calls `_roll_scanner_session_if_needed`). The
resync re-promotes yesterday's still-present handoff symbols into bot `lifecycle_states`
milliseconds before the scanner roll, and the roll's only watchlist touch for those bots is
`set_watchlist([])`, which — under lifecycle retention — does NOT purge already-promoted symbols.

I VERIFIED the operator's mechanism in code and it holds, with **one important refinement** (see
§2.4): the scanner roll *does* invoke each bot's `_roll_day_if_needed()`, which *does* hard-clear
lifecycle — but that hard-clear is keyed on the bot's `_active_day` flag, and that flag can be
advanced to "today" by an **asynchronous trade-tick / live-bar that arrives a few hundred ms before
the snapshot batch**. Once `_active_day` is already today, the roll's `_roll_day_if_needed()` is a
**no-op**, so the scanner roll's `set_watchlist([])` is the only thing left to purge — and it can't.
This refinement matters because it explains why the existing isolated unit test
(`test_session_roll_clears_bot_desired_watchlist_symbols_and_lifecycle`, passes today) does NOT catch
the live bug: the test never fires a pre-roll tick.

The fix has three agreed targets: (a) reorder so the scanner roll runs before any
broker-blocked/manual-stop resync, (b) add a hard-purge of non-protected bot lifecycle/watchlist
symbols at session-roll with a load-bearing carve-out for open positions / pending exits, and (c)
move the session reset to ~03:55 ET. (c) carries a due-diligence caveat: the FRESH watchlist
repopulation depends on live market data that only flows from ~04:00 ET, so 03:55 must "clear stale,
repopulate-on-data" not "clear-and-rebuild-now".

This touches **shared** watchlist/lifecycle state across **all** momentum bots, not just v2. v2
(`schwab_1m_v2`) only mirrors the published watchlist (commit 463e424) and is not a member of
`self.bots`; fixing the shared state fixes v2's symptom too.

---

## 2. Verified root cause (with file:line)

### 2.1 The two calls and their order in the live handler

`_handle_stream_message(...)`, `snapshot_batch` branch:

- `strategy_engine_app.py:6445` — `self.state.set_broker_blocked_symbols_by_strategy(self._load_schwab_ineligible_symbols_by_strategy())`
- `strategy_engine_app.py:6448` — `summary = self.state.process_snapshot_batch(...)`

The broker-blocked call runs FIRST, the snapshot batch (which contains the roll) runs SECOND.
Confirmed: the operator's "resync before roll within the same batch" is accurate at the handler level.

### 2.2 The resync inside `set_broker_blocked_symbols_by_strategy`

`set_broker_blocked_symbols_by_strategy` — `strategy_engine_app.py:5449`
- `:5461` stores `_broker_blocked_symbols_by_strategy`
- `:5462-5465` pushes blocked sets into each bot
- `:5466` — `self._resync_bot_watchlists_from_current_confirmed()`  ← the re-promotion trigger

`_resync_bot_watchlists_from_current_confirmed` — `strategy_engine_app.py:4768`
- `:4776` — `bot.set_watchlist(self._watchlist_for_bot(code, self._bot_handoff_symbols_for_bot(code)))`

The watchlist source is `_bot_handoff_symbols_for_bot(code)` → `bot_handoff_symbols_by_strategy[code]`
(`strategy_engine_app.py:5031-5033`). At ~04:00 in the pre-roll instant, `bot_handoff_symbols_by_strategy`
still holds **yesterday's** symbols (it is only cleared inside the roll, see §2.3). So this resync calls
`set_watchlist(yesterday's symbols)` and re-promotes them into `lifecycle_states`.

### 2.3 The scanner roll, inside `process_snapshot_batch`

`process_snapshot_batch` — `strategy_engine_app.py:4243`; first line `:4250` — `self._roll_scanner_session_if_needed()`.

`_roll_scanner_session_if_needed` — `strategy_engine_app.py:5404`
- `:5406-5407` guard: fires only when `current_scanner_session_start_utc(now) != self._active_scanner_session_start`
- `:5414-5439` clears scanner-side state: `confirmed_scanner.reset()`, `current_confirmed=[]` (`:5419`),
  `bot_handoff_symbols_by_strategy = {code: set()...}` (`:5425`), manual-stop reset (`:5438-5439`), etc.
- `:5441-5444` — for each bot: `roll_day = getattr(bot, "_roll_day_if_needed", None); roll_day()`
- `:5446` — `self._resync_bot_watchlists_from_current_confirmed()` (now reads EMPTY `current_confirmed`/handoff)

So the roll's *intended* cleanup for the bots is the per-bot `_roll_day_if_needed()` at `:5441`.

`StrategyBotRuntime._roll_day_if_needed` — `strategy_engine_app.py:1768`
- `:1770-1771` guard: `if session_day_eastern_str(self.now_provider()) == self._active_day: return False`
- `:1785` — `self.lifecycle_states.clear()`
- `:1786` — `self.watchlist.clear()`
- `:1787` — `self._desired_watchlist_symbols.clear()`
- `:1772` — `self.positions.reset()` (NOTE: `PositionTracker.reset()` at `position_tracker.py:302`
  only resets daily-PnL/streak/pause counters — it does NOT drop open positions; the position
  store survives the roll. Verified.)

### 2.4 Why `set_watchlist([])` does not purge — "lifecycle retention" and the `_active_day` no-op

`StrategyBotRuntime.set_watchlist` — `strategy_engine_app.py:347`
- `:354-359` if lifecycle policy DISABLED: `self.watchlist = set(desired); lifecycle_states.clear()` → hard reset.
- `:360-365` if lifecycle policy ENABLED (production): it iterates `desired_symbols` to *promote*, never
  removes, then calls `_sync_watchlist_from_lifecycle()`. With `desired=[]`, the loop adds nothing and
  removes nothing.

`_sync_watchlist_from_lifecycle` — `strategy_engine_app.py:3515`
- `:3516` seeds from `_desired_watchlist_symbols`
- `:3517-3521` re-adds every `lifecycle_states` symbol whose state `keeps_feed()`
- `:3522-3525` re-adds pending_open / pending_close / pending_scale / open-position symbols

This is "lifecycle retention": the watchlist is reconstructed from `lifecycle_states` + positions/pendings,
**not** from the `set_watchlist` argument. So `set_watchlist([])` cannot evict an already-promoted
`lifecycle_states` entry. Confirmed.

**The refinement (divergence from operator's description — flagged honestly):**
In isolation (no pre-roll tick), the scanner roll's `_roll_day_if_needed()` at `:1785-1787` DOES clear
`lifecycle_states`/`watchlist`/`_desired_watchlist_symbols`, so the post-`:5446` resync from an empty
`current_confirmed` leaves the bot clean. The existing test
`tests/unit/test_strategy_engine_service.py:1098` proves exactly this and passes.

For the bug to manifest live, `_roll_day_if_needed()` at `:5441` must be a **no-op** at the moment the
scanner roll runs. That happens when the bot's `_active_day` has ALREADY been advanced to today by an
earlier call in the same wall-clock window. `_roll_day_if_needed()` is called at the top of the
tick/bar/price handlers: `strategy_engine_app.py:641, 645, 1129, 1245, 1518, 1583, 1696, 1745`. Trade
ticks / live bars stream asynchronously and can arrive a few hundred ms before the periodic snapshot
batch. The operator's traced timestamps (resync ~04:00:00.965, reset ~04:00:00.969) are consistent
with a tick having crossed 04:00 first (advancing `_active_day`), so by the time the snapshot-batch
handler runs:

1. `:6445` resync re-promotes yesterday's handoff into `lifecycle_states` (handoff not yet cleared).
2. `:6448`→`:4250` roll fires; `:5441` `_roll_day_if_needed()` is a **no-op** (`_active_day` already today)
   → lifecycle NOT re-cleared.
3. `:5446` resync calls `set_watchlist([])` (empty `current_confirmed`/handoff) → cannot purge the
   re-promoted symbols (lifecycle retention) → **stale symbols persist**.

There is direct corroboration of this exact class of `_active_day` cleanup gap in a code comment at
`strategy_engine_app.py:9170-9178` (the 2026-05-18 evening-restart leak: "the next 4 AM ET roll then
failed to fully clean up because bot `_desired_watchlist_symbols` wasn't cleared by
`_roll_day_if_needed`").

**Net:** operator's mechanism is correct; the precise trigger for *why* the roll's own clear fails is
the `_active_day` no-op caused by a pre-roll async tick. Any fix must make the purge robust to
`_roll_day_if_needed()` being a no-op (i.e. not rely solely on it), which (b) addresses.

### 2.5 v2 mirror confirmation

`_watchlist_for_bot` — `strategy_engine_app.py:5374`. `schwab_1m_v2` is not in `self.bots`
(`:3901-4094`; bots = macd_1m, schwab_1m, tos, runner, macd_30s, polygon_30s, macd_30s_probe/reclaim/retest).
v2 runs as its own service mirroring the published watchlist (per commit 463e424 / memory notes), so it
shows the same stale set purely as a downstream mirror; fixing the shared producer state fixes v2.

---

## 3. State-mutation-path audit (all watchlist / lifecycle / promotion mutators)

This audit is a hard requirement (a prior two-axis regression a one-grep audit would have caught). Every
path that mutates bot watchlist / lifecycle / handoff / position-feed state:

| # | Path (file:line) | What it mutates | Add / Promote / Purge / Retain / Reset | Collision risk with fix |
|---|---|---|---|---|
| 1 | `set_watchlist` `:347` | `_desired_watchlist_symbols`, promotes into `lifecycle_states`, rebuilds `watchlist` via `_sync_watchlist_from_lifecycle` | Add/Promote (never removes when lifecycle enabled) | CORE of bug; `[]` cannot purge. Fix (b) must purge around it, not through it. |
| 2 | `discard_watchlist_symbols` `:368` | removes from `_desired_watchlist_symbols`, prewarm, and `lifecycle_states` for UNPROTECTED symbols (`_symbol_requires_feed`) | Purge (protected carve-out already present) | REUSE as the purge primitive for (b). |
| 3 | `set_prewarm_symbols` `:395` | `prewarm_symbols` | Add/Reset | low |
| 4 | `set_entry_blocked_symbols` `:403` | `entry_blocked_symbols` (derives from lifecycle) | Retain | low |
| 5 | `set_manual_stop_symbols` `:410` | drops manual-stopped from desired/prewarm/lifecycle | Purge (manual-stop only) | overlapping evictor; keep semantics |
| 6 | `set_broker_blocked_symbols` `:432` | drops broker-blocked from desired/prewarm/lifecycle | Purge (broker-block only) | overlapping evictor; note "Open positions preserved by `_sync_watchlist_from_lifecycle`'s positions clause" |
| 7 | `_sync_watchlist_from_lifecycle` `:3515` | rebuilds `watchlist` from desired + lifecycle.keeps_feed + pendings + positions | Retain/Rebuild | The retention engine; (b)'s carve-out must match its positions/pending clauses (`:3522-3525`). |
| 8 | `_prune_runtime_state` `:3468` | prunes indicators/quotes/builders to `keep` = watchlist+pendings+positions+prewarm | Purge (runtime, not lifecycle) | (b) must call this after purge so builders/quotes follow |
| 9 | `_roll_day_if_needed` (bot) `:1768` | clears lifecycle/watchlist/desired/etc.; `positions.reset()` (counters only) | Reset (day-keyed, can no-op) | UNRELIABLE at 04:00 due to `_active_day` no-op (§2.4) |
| 10 | `_roll_day_if_needed` (runner) `runner.py:492` | resets daily PnL/entered/closed only; does NOT touch watchlist | Reset (counters) | runner watchlist re-set every batch via `update_candidates` |
| 11 | `_roll_scanner_session_if_needed` `:5404` | scanner-wide reset incl. `bot_handoff_symbols_by_strategy={code:set()}` (`:5425`), then per-bot roll_day (`:5441`), then resync (`:5446`) | Reset + Re-promote | The fix's primary edit site (a)+(b) |
| 12 | `_resync_bot_watchlists_from_current_confirmed` `:4768` | per-bot `set_watchlist(handoff)`, manual-stop, entry-block, refresh_lifecycle | Add/Promote | The re-promotion vector; called from 5 sites (see below) |
| 13 | `set_broker_blocked_symbols_by_strategy` `:5449` | per-bot broker-block + resync (`:5466`) | Purge + Re-promote | The pre-roll caller (`:6445`) in the race |
| 14 | `apply_global_manual_stop_symbols` `:4702` | manual stops + discard handoff + resync (`:4715`) | Purge + Re-promote | also calls resync; reset path calls it with `set()` at `:5438` |
| 15 | `apply_manual_stop_symbols` `:4717` | per-strategy manual stops (no resync) | Purge | reset path calls with `{}` at `:5439` |
| 16 | `apply_manual_stop_update` `:4729` | live manual-stop add/remove + resync (`:4748`/`:4761`) | Purge/Restore + Re-promote | another resync caller |
| 17 | `_purge_faded_symbols_from_bot_watchlists` `:4947` | `discard_watchlist_symbols` for faded confirmed, with `_symbol_requires_feed` carve-out (`:4960-4968`) | Purge (protected) | EXACT precedent for (b)'s protected purge — model (b) on this |
| 18 | `_record_bot_handoff_symbols` `:4915` / `_discard_bot_handoff_symbols` `:4934` | mutate `bot_handoff_symbols_by_strategy` (feeds resync's set_watchlist) | Add/Purge | handoff is the resync source; roll clears it at `:5425` |
| 19 | `seed_confirmed_candidates` `:4654` / `restore_confirmed_runtime_view` `:4658` | restore path repopulates confirmed/handoff on restart | Add/Promote | mid-session restart vector (see §6) |
| 20 | `process_snapshot_batch` per-bot loop `:4356-4378` | per-batch `set_watchlist` + `refresh_lifecycle` (steady-state) | Add/Promote/Retain | the normal steady-state mutator; runs AFTER the roll in the same batch |

Resync callers (all re-promote from handoff/current_confirmed): `:5466`, `:5446`, `:4715`, `:4748`,
`:4761`. The reorder/purge fix must be consistent with all five so none re-introduces stale state.

---

## 4. Fix design

### (a) Move scanner session-roll BEFORE any broker-blocked / manual-stop resync

**Problem:** `:6445` (broker-blocked resync) runs before `:6448` (which contains the roll). The pre-roll
resync re-promotes yesterday's handoff.

**Change:** in `_handle_stream_message`'s `snapshot_batch` branch (`strategy_engine_app.py:6444-6452`),
run the scanner roll **before** the broker-blocked refresh. Concretely, hoist the roll out of
`process_snapshot_batch` into an explicit call at the top of the branch:

```
# proposed order in the snapshot_batch branch (design sketch, not final code):
self._preload_manual_stop_state()
self.state._roll_scanner_session_if_needed()          # NEW: roll first
self.state.set_broker_blocked_symbols_by_strategy(...) # now resyncs from CLEAN, post-roll state
summary = self.state.process_snapshot_batch(...)       # roll inside is now a no-op (already rolled)
```

- `process_snapshot_batch:4250` already calls `_roll_scanner_session_if_needed()`; it is idempotent (guarded
  at `:5406`), so calling it earlier and leaving the existing call in place is safe (second call no-ops).
  Preferred: keep the in-batch call as the canonical one and ADD the earlier explicit call, rather than
  removing the existing call (minimizes behavior change for all other callers of `process_snapshot_batch`,
  e.g. replay/tests).
- **Dependency on current order:** the broker-blocked set is loaded from
  `_load_schwab_ineligible_symbols_by_strategy()` (an external cache) and is order-independent of the roll;
  applying it after the roll is strictly better (it then filters today's fresh watchlist, not yesterday's).
- **Tradeoff:** this alone does NOT fully fix the bug, because the roll's own lifecycle clear can still
  no-op (§2.4). (a) closes the *handler-level* ordering race but must be paired with (b) for robustness.
  Doing (a) without (b) would reduce, not eliminate, the leak.

### (b) On session-roll, HARD-PURGE non-protected bot lifecycle/watchlist symbols (carve-out is load-bearing)

**Problem:** the roll relies on `_roll_day_if_needed()` which can no-op; even when it fires, a subsequent
resync re-adds. We need an explicit, unconditional purge at the roll that does not depend on `_active_day`,
and that preserves anything tied to real money.

**Change:** add a new bot method, e.g. `purge_session_watchlist()`, on `StrategyBotRuntime`, and call it
for each bot inside `_roll_scanner_session_if_needed` **after** the scanner-side clears (`:5414-5439`) and
**after/instead-of** the per-bot `_roll_day_if_needed()` loop (`:5441`), but **before** the resync (`:5446`).
Model it directly on the existing protected purge `_purge_faded_symbols_from_bot_watchlists` (`:4947`) +
`discard_watchlist_symbols` (`:368`), which already implement the exact carve-out we want.

Proposed `purge_session_watchlist()` behavior:
1. Compute `protected = { s for s in (lifecycle_states | watchlist | _desired_watchlist_symbols) if _symbol_requires_feed(s) }`.
2. Compute `removable = (lifecycle_states | _desired_watchlist_symbols | watchlist) - protected`.
3. `lifecycle_states.pop(s)` and `_desired_watchlist_symbols.discard(s)` for each `s in removable`.
4. Clear `prewarm_symbols` of `removable`.
5. `_sync_watchlist_from_lifecycle()` (rebuilds watchlist; protected symbols re-added via its
   positions/pending clauses `:3522-3525`).
6. `_prune_runtime_state()` (drops orphaned builders/quotes/indicators for purged symbols).

**Exact purge-protection rule ("protected" definition):** a symbol is PROTECTED (kept) iff
`_symbol_requires_feed(symbol)` is true (`strategy_engine_app.py:3539`), i.e. ANY of:

- **Open position:** `self.positions.has_position(symbol)` (`:3545`) — quantity-bearing managed position row.
- **Pending OPEN intent:** `symbol in self.pending_open_symbols` (`:3541`) — an OPEN order in flight.
- **Pending CLOSE intent:** `symbol in self.pending_close_symbols` (`:3541`) — an exit order in flight.
- **Pending SCALE level:** `(symbol, level) in self.pending_scale_levels` (`:3543`) — an in-flight partial scale-out.

Everything else (lifecycle-only / handoff-only / scanner-promoted with no position and no in-flight order)
is **purged**. This rule is reused verbatim from the already-shipped faded-symbol purge (`:4960-4968`),
which is the strongest argument it is correct: it is the same predicate the system already trusts to evict
a faded confirmed symbol without dropping a live position.

**Detection specifics:**
- open position → `PositionTracker.has_position` (true when qty > 0 tracked row exists).
- pending exits/opens/scales → the three in-flight sets maintained by `apply_order_status`/`apply_execution_fill`
  and the `restore_pending_*` re-hydration methods (`:471-481`).
- Managed-position rows survive `positions.reset()` (counters only, §2.3), so a position open across the
  04:00 boundary remains protected.

**Why this is the riskiest part / tradeoffs:**
- If the carve-out is too narrow → we orphan a live position's feed/exit ladder (catastrophic: a held
  symbol stops getting bars/quotes, exits never fire). Mitigated by reusing the proven `_symbol_requires_feed`.
- If too wide → stale symbols leak again (the original bug). The predicate is exact: it keeps ONLY
  position/pending-bearing symbols.
- Interaction with `_sync_watchlist_from_lifecycle` (`:3517-3525`): even after we pop a protected symbol's
  lifecycle_state, the rebuild re-adds it via the positions/pending clauses — so for a protected symbol it
  is safe to pop the lifecycle_state OR keep it; to be conservative the purge should NOT pop protected
  symbols' lifecycle_states (keep their retention metadata so exit gating/feed-retention continues unchanged).
- Runner bot: `RunnerStrategyRuntime` has no `lifecycle_states`; its watchlist is reset each batch via
  `update_candidates`. The purge call should be guarded `isinstance(bot, StrategyBotRuntime)` (mirroring
  `:4964`).

### (c) Move the session reset to ~03:55 ET (with due-diligence; see §5)

**Goal:** let the clear settle before 04:00 trading so the first 04:00 fresh-confirm lands on an empty
watchlist, removing the millisecond race window entirely.

**Change:** the session boundary is computed by `current_scanner_session_start_utc`
(`strategy_engine_app.py:229`, hard-coded `hour=4, minute=0`) and `session_day_eastern_str`
(`strategy_core/time_utils.py:23`, default `reset_hour=4, reset_minute=0`). A 03:55 reset would shift the
boundary. **Do NOT simply change these to 03:55** — see the due-diligence finding (§5): the FRESH watchlist
cannot be rebuilt at 03:55 (no live data yet). Instead:

- Introduce a separate **"stale-clear" tick at 03:55** that performs the purge (b) and clears scanner
  confirmed/handoff, WITHOUT advancing `_active_scanner_session_start` to the new session and WITHOUT
  expecting repopulation. Repopulation then happens naturally as 04:00 data arrives through the normal
  `process_snapshot_batch` confirm path.
- Keep the canonical session-start at 04:00 for "what session are we in" semantics (persistence keys,
  restore-window guards at `:9155-9168`/`:9258-9270` compare against `current_scanner_session_start_utc`;
  changing that constant would invalidate those comparisons and the persisted `scanner_session_start_utc`).
- **Tradeoff:** a separate 03:55 clear adds a second time-trigger to reason about. Simpler alternative:
  rely on (a)+(b) alone (which already eliminate the race deterministically) and treat (c) as a
  belt-and-suspenders defense-in-depth that clears the stale set ~5 min early so even a malfunctioning
  04:00 batch starts clean. Recommend shipping (a)+(b) first; (c) as a follow-up only if operator wants
  the early-clear margin.

---

## 5. 03:55 due-diligence finding — does FRESH repopulation need 04:00 data?

**Verdict: YES. The fresh watchlist repopulation depends on live market data that only flows from ~04:00 ET.
A 03:55 "clear-and-rebuild-now" would have no inputs to rebuild from; 03:55 must be "clear stale,
repopulate-when-data-arrives".**

Trace of what builds the new watchlist:
- `process_snapshot_batch` consumes `snapshots` (live `MarketSnapshot`s) + `reference_data`
  (`strategy_engine_app.py:4243`).
- It runs `apply_five_pillars(...)` (`:4270`), `top_gainers_tracker.update(...)` (`:4278`),
  `alert_engine.check_alerts(...)` (`:4285`), then `confirmed_scanner.process_alerts(...)` (`:4313`).
- Confirmation gates require real volume/price: `MomentumConfirmedScanner._check_common_filters`
  (`strategy_core/momentum_confirmed.py:414-429`) rejects on `volume < confirmed_min_volume` and on
  volume/float turnover ratio — i.e. it needs **actual traded volume**.
- The watchlist published to bots is then `bot.active_symbols()` aggregated (`:4380-4386`).

At 03:55 ET the pre-market session has not opened the scanner window; incoming snapshots are sparse/zero
or absent, so `apply_five_pillars`/`process_alerts` produce no confirmations → nothing to repopulate with.
The fresh symbols only materialize once 04:00 data streams in. (The persistence/restore fallbacks
`_seed_confirmed_candidates_from_persisted_snapshot`/`_restore_watchlist_from_scanner_cycle_history`,
`:9120`/`:9227`, are explicitly guarded to refuse cross-session and stale snapshots — they will NOT
backfill a "new" 03:55 session from yesterday.)

**Handling:** implement (c) as clear-only at 03:55 (purge + scanner reset) and let the existing 04:00
data-driven confirm path repopulate. Do not move the `current_scanner_session_start_utc` constant.

---

## 6. Edge cases

1. **Weekend / holiday boundary.** The roll/`_active_day` are calendar-driven (Eastern day string). Over a
   weekend the first 04:00 roll on Monday must purge Friday's symbols. The purge (b) is unconditional on
   each roll, so it handles multi-day gaps. The restore guards (`:9162-9168`, `:9270`) already refuse to
   seed from a prior session, so no weekend backfill. Verify: a Friday open position carried over the
   weekend stays protected (position survives `positions.reset()`).
2. **Mid-session restart.** On restart the seed/restore path (`seed_confirmed_candidates:4654`,
   `restore_confirmed_runtime_view:4658`, `_restore_watchlist_from_scanner_cycle_history:9227`) repopulates
   handoff/confirmed for the *current* session only (guarded by `scanner_session_start_utc` equality and
   `strategy_seeded_snapshot_max_age_seconds` cap, `:9180-9192`). Purge (b) only runs at roll, not at
   restart, so a same-session restart correctly keeps today's symbols. Confirm the restart does not itself
   straddle 04:00 (if it does, the first post-restart batch triggers a normal roll → purge).
3. **Symbol that is BOTH yesterday's-stale AND today's-fresh.** After purge (b) the symbol is removed; if it
   re-confirms from today's data it is promoted fresh via the normal `process_snapshot_batch` path with a
   new `lifecycle_states` entry / `promoted_at` timestamp. No double-count: `set_watchlist` de-dupes
   (`_watchlist_for_bot:5395-5401`) and lifecycle keys by symbol. Net: correct (treated as today's).
4. **Open position in a symbol no longer scanned today.** Protected by `_symbol_requires_feed` (open
   position) → kept on watchlist via `_sync_watchlist_from_lifecycle` positions clause; feed/quotes/bars
   continue; exit ladder runs. This is the critical correctness case and is exactly what the carve-out
   guarantees.
5. **Cooldown / retention interaction.** A symbol in `cooldown`/`resume_probe`/`dropped` lifecycle state
   with NO position is NOT protected → purged at roll (it's stale). This is desired: cooldown is intra-session
   state and should not survive the day boundary (consistent with `_roll_day_if_needed` clearing lifecycle).
   Feed-retention `feed_retention_states` is already cleared at roll (`:5435`); the purge must also drop
   per-bot retention metadata for purged symbols (handled by popping lifecycle_states), but keep it for
   protected ones (do not pop protected lifecycle_states — see (b) note).

---

## 7. Test plan

All in `tests/unit/test_strategy_engine_service.py` (mirror existing `now_box`/`make_test_settings`
fixtures; the existing roll tests at `:1056`, `:1098`, `:2388` are the templates).

(i) **Stale symbols hard-purged at roll.** Seed `bot_handoff_symbols_by_strategy["macd_30s"]={"AEHL","GOVX"}`,
   resync (promote), then cross 04:00 and assert after the roll+purge: `lifecycle_states == {}`,
   `watchlist == set()`, `_desired_watchlist_symbols == set()`. (Extends `:1098`.)

(ii) **Open-position / pending-exit symbols PRESERVED through purge.** Give `macd_30s` an open position in
   `AEHL` (`restore_position`, `:458`) and a `pending_close` in `GOVX` (`restore_pending_close`, `:475`),
   plus a stale non-position `MOBX`. Cross 04:00; assert `AEHL` and `GOVX` remain in `watchlist` /
   protected, `MOBX` purged. Also assert a pending-scale `(SYM, level)` is preserved.

(iii) **Ordering — roll before resync (the real-handler reproduction, NOT the isolated roll).** This is the
   test the current suite is MISSING and the one that would have caught the live bug. Drive it through the
   handler path: simulate (1) a pre-roll trade tick at 04:00:00.5 that advances `_active_day` (so the roll's
   `_roll_day_if_needed` no-ops), then (2) `set_broker_blocked_symbols_by_strategy` (re-promotes yesterday),
   then (3) `process_snapshot_batch`. Assert NO stale symbols survive. Without the fix this fails; with
   (a)+(b) it passes. Also assert the broker-blocked resync (`:5466`) reads post-roll (empty) handoff.

(iv) **03:55 timing behavior** (only if (c) is implemented). At 03:55 assert: stale set cleared, watchlist
   empty, `_active_scanner_session_start` NOT advanced to the new session, and that a subsequent 04:00
   snapshot batch with live data repopulates fresh confirmations. Assert 03:55 with no data does NOT
   repopulate (proves the due-diligence handling).

Add a regression test asserting `purge_session_watchlist` is idempotent and a no-op when there is nothing
to purge (matches `_purge_faded_symbols_from_bot_watchlists` early-return at `:4954`).

---

## 8. Blast radius / rollback

- **Shared state across ALL bots.** `lifecycle_states`, `watchlist`, `bot_handoff_symbols_by_strategy`,
  `_broker_blocked_symbols_by_strategy`, and the roll/resync paths are shared by every momentum bot
  (macd_1m, schwab_1m, tos, macd_30s + probe/reclaim/retest, polygon_30s) and the runner. v2 mirrors the
  published watchlist downstream. **This is NOT a v2-only change** — any bug in the purge predicate affects
  every live-money momentum bot, including the chance of orphaning a real open position's feed. Call this
  out explicitly to the operator before merge.
- **Highest-severity failure mode:** an over-narrow carve-out drops a held position's feed/exit ladder.
  Mitigated by reusing the already-trusted `_symbol_requires_feed` predicate verbatim and by test (ii).
- **Rollback:** (a) and (b) are additive and independently revertible. (a) is a pure reorder (revert =
  remove the hoisted roll call). (b) is a new method + one call site in `_roll_scanner_session_if_needed`
  (revert = delete the call). No schema/persistence changes. (c), if shipped, is a separate time-trigger
  and revertible on its own. Recommend shipping (a)+(b) together (they are co-dependent for full fix),
  holding (c) as a follow-up.
- **Deploy discipline:** per project rules this is a live-money/shared-state change — PR + Validate
  (no direct push), attended deploy with account-flat at restart, after-close v2/strategy-engine restart,
  RTH = the verdict. Design-first (this doc) before PR.

---

## 9. Open questions for operator

1. Confirm the async-tick `_active_day` no-op (§2.4) matches your trace: did a trade-tick/live-bar for any
   symbol cross 04:00 *before* the ~04:00:00.965 snapshot batch on the day you traced? (If yes, this pins
   the trigger; if no, there may be a second no-op vector to find.)
2. Do you want (c) the 03:55 early-clear at all, given (a)+(b) already eliminate the race deterministically?
   It adds a second time-trigger for marginal margin.
3. For protected symbols at the roll, OK to KEEP their `lifecycle_states` (preserve retention/exit-gating
   metadata) rather than pop-and-rebuild? Design recommends keeping them.
4. Should the purge also run for the runner bot? It has no lifecycle and resets its watchlist each batch,
   so design currently skips it (guarded by `isinstance(bot, StrategyBotRuntime)`). Confirm acceptable.
5. Is there any non-`snapshot_batch` caller that legitimately wants yesterday's handoff to survive a roll
   (e.g. a planned overnight-hold strategy)? If so the purge needs an opt-out per strategy code.

## 10. Review verdict (2026-06-16) — both gates PASS, with 3 build directives

**Gate 1 — purge predicate `_symbol_requires_feed` is EXHAUSTIVE.** Verified it protects pending_open ∪
pending_close ∪ pending_scale ∪ `positions.has_position`. The fill handoff (pending_open → position) is
ATOMIC: `apply_execution_fill` (`:1569`) is synchronous with NO `await` between `pending_open_symbols.discard`
(`:1593`) and `positions.open_position` (`:1595`), so the async purge cannot interleave into a window where a
symbol is in neither state. Cooldown/prewarm symbols carry no position → safe to purge.
- **Cross-bot (v2, the go-live bot) is independently safe.** v2 mirrors the published list but unions its own
  `_protected_symbols()` into `selected` in one atomic expression (`schwab_1m_v2_bot.py:885-886`).
  `_protected_symbols` = strategy-view `position_qty>0` ∪ `_fetch_open_positions()` (OMS/DB view incl. in-flight),
  so a strategy-engine purge **cannot orphan a v2 position** — even post-restart (the DB view recovers positions
  v2's in-memory state lost). **DIRECTIVE: document this v2-self-protection dependency** so a future change to
  v2's mirror can't silently remove it.

**Gate 2 — reproduction test reproduces the real race.** Test (iii) drives pre-roll-tick → resync →
`process_snapshot_batch` and asserts no stale survive (fails without the fix). Test (ii) asserts preservation.
- **DIRECTIVE: MERGE preservation into the real-handler reproduction (iii).** Today the stale-purge assertion
  (iii) and the preservation assertion (ii) are split; (ii) preserves through a more isolated path. Strengthen
  (iii) to carry an open position + pending_close + pending_scale AND stale non-position symbols through the
  SAME pre-roll-tick→resync→batch sequence, asserting BOTH purge AND preservation under the exact interleaving
  that causes the bug. This proves the load-bearing carve-out survives the race, not just isolation.

**Build-correctness directive — hang the hard-purge on `_roll_scanner_session_if_needed`, NOT
`_roll_day_if_needed`.** `_roll_day_if_needed`'s `_active_day` guard is pre-advanced by ticks/fills (`:1583`
et al.) → it's the no-op that causes the bug. `_roll_scanner_session_if_needed`'s guard
`_active_scanner_session_start` is advanced ONLY inside the roll itself (`:5446`) + init (`:3807`), never by
ticks, and the roll is invoked from BOTH the snapshot batch (`:4250`) AND a periodic heartbeat (`:6123`), so it
fires reliably exactly once per boundary — even on sparse-data mornings (06-08's roll was 88s late but still
fired via the heartbeat path). The purge must live here.

**Verdict: cleared to build** — (a) reorder + (b) hard-purge are co-dependent (ship together); (c) 03:55 =
clear-stale-only (repopulation needs ~04:00 data). PR for review before deploy; deploy is a separate attended step.
