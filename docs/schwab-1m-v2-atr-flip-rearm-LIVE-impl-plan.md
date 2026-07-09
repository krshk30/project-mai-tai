# schwab_1m_v2 ATR re-arm — LIVE implementation plan (DESIGN-FIRST — review before touching live code)

**Status:** **IMPLEMENTED behind the flag (default OFF); byte-identical-off PROVEN; flag NOT flipped.**
Design GO'd 2026-07-09. `schwab_1m_v2.py` + `settings.py` edited, all writes gated on
`self._atr_rearm_enabled`. Proof: the **entire 1034-test suite passes** flag-off (byte-identical) + 6 new
flag-ON live tests (`test_schwab_1m_v2_atr_flip.py`) pin the guard lifecycle AND the end-to-end re-arm
(`..._real_flip_ENTERS` green; `test_shipped_bool_MISSES...` reproduces the bug on the shipped path).
**Next (NOT done, holds for you):** review the diff → **attended, quiet-window** flag flip with v2 flat,
rollback ready (`…_REARM_ENABLED=false` + restart) → re-run D3/D5 off-hours. **Do not flip the flag until
scheduled.**

Companion: the invariant + lifecycle + release/quantization rationale are in
`schwab-1m-v2-atr-flip-rearm-fix-design.md` (§4). This doc maps that onto the live code, line by line.

---

## 1. The flag + config (default OFF = dormant)

- `strategy_schwab_1m_v2_atr_flip_rearm_enabled: bool = False` → `self._atr_rearm_enabled`.
- `strategy_schwab_1m_v2_atr_flip_rearm_timeout_secs: float = 12.0` → `self._atr_rearm_timeout_secs`.

Env: `MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_FLIP_REARM_ENABLED`, `…_ATR_FLIP_REARM_TIMEOUT_SECS`.

**Every new read/write below is behind `if self._atr_rearm_enabled:`. Flag OFF → the existing
`atr_fired_in_short_seg` bool logic runs UNCHANGED → byte-identical.**

## 2. New `SymbolState` fields (inert when the flag is off)

```
atr_guard: str = "UNCLAIMED"     # "UNCLAIMED" | "PROVISIONAL" | "CLAIMED" — the pending-order lifecycle
atr_emit_ts_ms: int = 0          # wall-clock of the last PROVISIONAL emit (drives the timeout release)
```
Reset at the 04:00 session anchor alongside `atr_fired_in_short_seg` (wherever L673-anchor clears it).

## 3. The lifecycle mapped onto the live methods

The guard is **claimed only when an order is actually EMITTED (→ PROVISIONAL), promoted to CLAIMED only on
a FILL, and released to UNCLAIMED on skip / drop / SELL flip / timeout.** The ARM (a PendingHold) does NOT
claim — the existing `atr_hold_pending is not None` check already prevents concurrent arms.

| live site (current) | current behavior (bug) | flag-ON behavior (fix) |
|---|---|---|
| **on_quote touch-arm** L512-528 | gate `not atr_fired_in_short_seg`; **sets it True on ARM** (L520) | gate on `atr_guard == "UNCLAIMED"`; **do NOT claim on arm** (arm only sets the PendingHold) |
| **bar-close touch** `_update_atr_state` L717-725 | gate `not …_seg`; sets True on touch | gate on `atr_guard == "UNCLAIMED"`; claim happens at emit, not here |
| **emit (draft returned)** `_build_hold_draft` (confirm/thin) + `_maybe_atr_emit` (bar-close / backstop) | — | on returning a real draft: `atr_guard = "PROVISIONAL"; atr_emit_ts_ms = now` |
| **_resolve_hold skip / skip_gated** L543,L551 | returns None, **flag stays True** → segment spent | no claim was made → `atr_guard` stays UNCLAIMED → re-arm allowed |
| **_resolve_hold_on_bar drop_flip** L562-564 | clears pending, flag stays True | clear pending; `atr_guard = "UNCLAIMED"` → the BUY-flip backstop can fire |
| **SELL flip** `_update_atr_state` L738-740 | `…_seg = False` | also `atr_guard = "UNCLAIMED"; atr_emit_ts_ms = 0` (new short segment) |
| **BUY flip backstop** `_maybe_atr_emit` variant B L811-814 | variant B fires only on `touch` → flip emits nothing | if `flip=="BUY"` AND `atr_guard=="UNCLAIMED"` AND flat+off-cooldown → emit flip-close (`entry=cur.close`, reuse variant-A's L810 path) |
| **fill** `update_position` L413 (poll) | only arms cooldown on N→0 | on `prev==0 and qty>0`: `atr_guard = "CLAIMED"` |
| **timeout release** `update_position` L413 (poll) | — | if `atr_guard=="PROVISIONAL"` and `now_ms - atr_emit_ts_ms >= timeout_secs*1000` and `position_qty==0`: `atr_guard="UNCLAIMED"; atr_emit_ts_ms=0` |

**Release rides the position poll (5s)** — same clock the backtest quantizes to (`emit+timeout+poll`
upper bound). No order-terminal event dependency (the strategy is poll-only); the reject-signal
optimization is the separate follow-up ticket (`schwab-1m-v2-reject-signal-release.md`).

**⚠ Serialization (a subtlety the arm-no-longer-claims change exposes).** Today the legacy bool, set on
the arm (on_quote L520), also *blocks the bar-close touch* while a hold is pending — the two entry paths
are serialized by that one write. Since the flag-ON path does **not** claim on arm, the bar-close-touch
gate must instead be **`atr_guard == "UNCLAIMED" AND atr_hold_pending is None`** — the `pending is None`
clause restores exactly that serialization (an armed-but-unresolved hold blocks a bar-close touch in the
same bar). `on_bar` runs `_evaluate_completed_bar` (bar-close touch + `_maybe_atr_emit`) BEFORE
`_resolve_hold_on_bar` and returns `eval_draft or hold_draft`, so at most one draft emits per bar.
Verified by the whole existing suite passing flag-off + the flag-on live tests.

### Edge #2 (confirmed): one successful entry per short segment
`CLAIMED` persists through a scratch (a filled-then-closed position) until the next SELL flip opens a new
short segment — so no re-entry within the same short segment. **Quantify its frequency in the D3/D5
re-run** (how often a scratched position's segment would have offered a second leg) so the
second-leg-vs-churn question is decided later on its own evidence.

## 4. Centralize the guard writes

One helper `_set_atr_guard(state, to_state)` (and the emit-timestamp stamp) so every touch-point is
consistent — the #237 overlapping-path lesson. Reads stay pure (`state.atr_guard == "UNCLAIMED"`); the
timeout release is an explicit poll-driven step, never a mutate-on-read (matches the backtest's
`_release_if_expired`).

## 5. Double-entry safety (the reason the guard is 3-state, not a bool)

- A working order (PROVISIONAL) is not yet a position, so `position_qty==0` — the flat gate alone wouldn't
  stop a second emit. PROVISIONAL blocks re-emit until fill/timeout/SELL.
- **Late-fill race** (released at 12s, fill lands at 13s): `update_position` sets `position_qty>0` → the
  flat gates (on_quote L516, the `_maybe_atr_emit` caller) block any re-entry, and the next poll sets
  CLAIMED. So "err short" cannot double-fill — exactly the asymmetry we chose.

## 6. Byte-identical-off proof obligation

- Unit: with the flag OFF, feed the (a)/(b)/(c)/(d) live-equivalent bar/quote sequences and assert the
  emitted intents are identical to current `main` (characterization test on `main` first, then this
  branch). The new fields never read/write when off.
- Full `test_schwab_1m_v2_bot.py` + strategy_core suite green, flag off.
- Determinism/oracle pin unchanged (flip TIMES don't move — only whether we enter).

## 7. Backtest parity (re-pin to CORRECT)

`v2_sim._simulate_v2_rearm` already implements this lifecycle (PROVISIONAL/CLAIMED/UNCLAIMED, poll-
quantized release, flip-close backstop). After the live edit, re-pin the "v2 touch parity" golden to the
CORRECTED behavior (real flip fires), hand-verified vs the NVVE chart — never to the code's post-change
output.

## 8. Rollout / rollback

- Flag default **False**; ship the live edit dormant. Review the diff, then flip the flag on a **quiet
  window** (after-close or pre-open), **v2 flat**, **attended**, explicit GO.
- Restart v2 ONLY (not the fleet). Verify: `[V2-ATR-PROBE]`/hold logs show a rejected graze NO LONGER
  spending the segment, and a subsequent BUY flip entering; no double-entry; `atr_guard` transitions sane.
- **Rollback = env flag False + restart v2.** Instant, no code revert.
- Then re-run D3/D5 on the corrected entry (off-hours, niced — no heavy backtests during RTH).

## 9. Confirmations — ALL GO'd (2026-07-09)

1. ✅ **Release on the 5s position poll** (matches the backtest quantization; keeps the timeout off the
   latency-sensitive on_quote path where the OMS SPOF lived).
2. ✅ **Edge #2 — one successful entry per short segment** (CLAIMED persists through a scratch). Quantify
   its frequency in the D3/D5 re-run.
3. ✅ **Layer `atr_guard` alongside `atr_fired_in_short_seg`** (flag-off path byte-identical). ⚠ The
   bool's removal is filed as a **gated follow-up** (`schwab-1m-v2-guard-bool-removal.md`): two state
   fields for one concept is the same drift condition that caused this bug — transitional only, deleted
   once the flag is on and proven. `_set_atr_guard` centralizes the guard writes as the interim mitigation.

## 10. ⚠ Restart while an order is working (confirmed PRE-EXISTING, not worsened)

`atr_guard` (and `atr_emit_ts_ms`) live in `SymbolState`, **in memory** — there is **no boot rehydrate**
(verified: `SymbolState` is created fresh in `watchlist_state`; nothing in `schwab_1m_v2_bot.py`
reconstructs the guard). A restart **between an emit and its fill** loses the PROVISIONAL claim → after
boot the guard defaults `UNCLAIMED` → the strategy sees flat (a working, unfilled order is **not** a
position) → a subsequent touch/flip could emit **again into a live working order.**

**This is pre-existing and NOT worsened by the fix:** the legacy `atr_fired_in_short_seg` bool was **also**
in-memory with no rehydrate, so it too reset to "free" (`False`) on restart with the identical exposure.
Both designs lose the claim in the emit→fill gap; post-fill, `position_qty > 0` (rebuilt from the poll)
gates re-entry for both. The fix neither introduces nor widens this window — it changes *what* the
in-memory claim is (bool → guard), not its persistence.

**Family:** this is the "in-memory state lost on restart" class, adjacent to F2
([[project_mai_tai_v2_entry_warmup_gate]]). The real mitigation is a **durable working-order mirror**
(rehydrate PROVISIONAL from the OMS/intent ledger on boot, like F2's `oms_armed_stops`) — logged as a
future item, **out of scope** for this PR (it's an existing gap, not a regression). Noted explicitly so
it is not mistaken for new risk introduced by the re-arm change.

## 11. ⚠ Fill-and-exit inside one poll interval — ACCEPTED RESIDUAL (measured rare)

`_poll_atr_guard` infers a fill from an observed `prev_qty==0 → position_qty>0` transition on the 5s
poll. **A fill that opens AND fully closes within one poll interval is invisible** (`position_qty` reads
0→0): no branch matches, the guard stays PROVISIONAL, and at `emit+timeout` it **re-arms as if never
filled** — violating the one-entry invariant and silently enabling the deferred edge-#2 second leg. The
cooldown doesn't arm either (the invisible open+close never triggers the observed N→0). **This is NOT
pre-existing:** the shipped bool claims on arm and stays True through a fast scratch, so shipped never
re-enters; the releasable guard can.

**Measured (DB, 06-24..07-08, 26 ATR fills):** only **2** had a full lifetime `< 5s` (KIDZ 2s, LHAI 4s —
penny scratches); the other **24 lived ≥ 5s**. A position lasting ≥5s always contains a poll instant
(polls are 5s apart) → reliably detected; only sub-5s positions can fall entirely between two polls, and
even then it's phase-dependent (~60% miss at 2s, ~20% at 4s → **~0.8 expected actual misses across the
whole sample**). **The backtest fills immediately (no poll), so D3/D5 is NOT confounded** — this is a
live-only divergence.

**Verdict (operator's rule — rare → document + ship):** accepted residual risk for the flag ship. Bounds:
- Made **observable**: `[V2-REARM]` logs on both CLAIMED (fill) and the timeout re-arm — watch these at
  the attended flip.
- Pinned by a test (`test_rearm_KNOWN_RESIDUAL_fast_scratch_between_polls_re_arms`) so it flips visibly
  when fixed.
- **Proper fix** = consume order-terminal events (a fill of an already-closed position → CLAIMED), the
  **same capability** that fixes the 27 blind-timeout rejects — widened into
  `schwab-1m-v2-reject-signal-release.md`. Not a correctness gate for this ship; a completeness upgrade.
