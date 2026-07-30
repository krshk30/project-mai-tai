# v2 — "fresh flip since CONFIRMATION" + removal of the 09:30-10:00 ORB window

**Status:** design only. No code changed, nothing live. Operator decision 2026-07-30; this doc is for
review before any implementation.

---

## 1. What went wrong (2026-07-30, live money)

Three round trips, all losers, median **−4.92%** (SNDG schwab −4.93 / SNDG webull −4.92 /
APLX webull −4.74; the APLX schwab leg's exit was not captured — see §7).

| | APLX | SNDG |
|---|---|---|
| ATR flip level | 8.3376 | 4.9828 |
| flip bar | **09:16 ET** | **09:23 ET** |
| symbol joined the watchlist | **09:38 ET** | **09:34 ET** |
| break detected + suppressed | 09:42 (px 9.6553) | 09:38 (px 5.6400) |
| **bought at market** | **10:00:05 @ 10.3550** | **10:00:12 @ 6.1000** |
| distance past the flip | **+23.7%** | **+18.9%** |

Operator caught it on the TOS chart within minutes: *"it's not resting, it's not reclaim, it's been
going long for a long time, and it bought it all the way at the top."* The chart's `ATR X` marker
read ~4.98 — matching our recorded `cw_flip_level` of 4.9828 to four decimals.

**Both flips predate the moment the symbol joined the watchlist.** They were reconstructed from the
REST warmup replay that runs when the scanner promotes a new symbol mid-session.

### Ruled out — do not re-litigate
- **Bad bars: NO.** The stored NUWE 08:59 bar matched the operator's TOS chart exactly, volume
  included (294,632). Bar build and bar data are correct.
- **#590 (cooldown removal): NO.** Independently verified at `ffdf3d6^` — every reader of
  `cooldown_bars_remaining` was dead code or a log argument; no live path consulted it. A revert
  buys nothing. (Verdict holds while `ATR_ONLY_MODE=true`, confirmed set.)
- **The ORB window as ROOT cause: NO.** It is a severity multiplier, not the cause — see §3.

---

## 2. The rule (operator-stated)

> **A symbol newly confirmed by the momentum scanner must wait for an ATR flip that occurs AFTER it
> was confirmed. A symbol we have been watching all along is exempt — we saw its flips happen live.**

The discriminator is **"were we watching when the flip happened?"** — not bot-boot, not a fixed age.

## 3. Why the ORB window is NOT the root cause

`_cw_in_orb_window` (`schwab_1m_v2.py:1290-1294`, hardcoded 09:30-10:00) suppresses reactive entries
but **PAUSES the setup rather than cancelling it** — `cw_trigger` is frozen 3 bars after the flip
(`:1355-1357`) and never expires in RTH, so every armed symbol is released at one clock edge at
10:00:00 and enters on the first quote above its stale trigger.

⛔ **Removing the window alone would NOT have prevented either trade** — APLX would have bought at
~09:42 at 9.6553 instead of 10:00 at 10.3550. Better fill, same wrong trade. The window changed
*when* and *how badly*, not *whether*.

⭐ It is still worth removing: **the ORB bot has been `inactive` + `disabled` since 2026-07-23**, so
the window reserves the most volatile 30 minutes of the day for a bot that no longer trades.

## 4. Why the existing guard misses it

The mechanism already exists — `_cap_reconstructed_segment` (`schwab_1m_v2_bot.py:1450`):

> mark a RECONSTRUCTED armed segment (**`arm_bar_ts < boot`**) as USED, so v2 can only enter on flips
> AFTER boot

It uses **one global `_boot_ms`** (`:545`). v2 booted 07-28 19:05 ET; APLX's flip bar was 07-30
09:16 ET — two days after boot, so the cap did not apply, even though the flip predates the symbol
joining the watchlist by 22 minutes.

**The reference point is wrong, not the mechanism.**

### ⛔ And the age-based qualifier is DOUBLE-DEAD
`docs/v2-atr-fresh-flip-qualifier-design.md` specified an `atr_state_age < 5` screen (measured:
winners avg age 2.6, losers 16.3, over 740 entries / 7 weeks). It IS implemented
(`schwab_1m_v2.py:841`) but:
1. `MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_FLIP_USE_MAX_STATE_AGE=false` on the box, and
2. it sits in `_build_hold_draft`, reachable only via `_resolve_hold`, whose input
   (`state.atr_hold_pending`) is assigned **only** inside a block `_cw_v2_enabled` short-circuits.

⇒ Enabling the flag today would gate nothing. **This is the third guard found living in replaced
code in eight days** (liquidity floor #587, the cooldown, this). See §8.

---

## 5. Proposed change A — per-symbol watch-start

Replace the single boot reference with a per-symbol "watching since" timestamp.

- `schwab_1m_v2_bot.py:1154` already computes `new_symbols = selected - self._watchlist`. Stamp
  `self._watch_start_ms[symbol] = now_ms` for each newly added symbol there.
- Symbols present at process start get `watch_start = _boot_ms` ⇒ **existing behaviour preserved**,
  which is exactly the operator's exemption for symbols held since 07:00.
- `_cap_reconstructed_segment` compares `arm_bar_ts` against `_watch_start_ms[symbol]` instead of
  `_boot_ms`.

**Comparison must be STRICT (`arm_bar_ts > watch_start`), not `>=`.** Bar timestamps are the bar's
OPEN. A symbol joining at 09:38:30 was not watching when the 09:38 bar opened, so that bar's flip is
not observable-live. Fail-closed costs at most one legitimate first entry.

### Edge cases
| case | behaviour |
|---|---|
| symbol on the watchlist at boot | `watch_start = boot_ms` — unchanged, exempt |
| symbol promoted mid-session | `watch_start = join_ms` — pre-join flips rejected |
| symbol dropped then re-added | **watch_start RESETS.** We stopped watching, so we may have missed flips. Fail-closed. |
| 04:00 ET scanner session roll | watch_start persists while the symbol is continuously watched |
| process restart | all symbols get `boot_ms` — today's behaviour |
| warmup replay after promotion | bars still feed the ATR state (needed for warmup); any arm they create is marked USED |

⛔ Warmup bars must still be ingested — the ATR needs history to be correct. Only the ARM they
produce is rejected, never the bars themselves.

## 6. Proposed change B — remove the 09:30-10:00 ORB window

Delete the `_cw_in_orb_window` gate at `:1448`. Keep the `[V2-CW-ORB-BLOCK]` counter concept only if
it can be retargeted at something real; otherwise remove it with the gate.

⚠️ **This is a risk INCREASE, stated plainly:** v2 will trade the 09:30-10:00 open for the first
time. On 2026-07-30 the window suppressed **3** breaks (APLX, SNDG, STKH), so expect roughly that
many extra entries per day, at open volatility.

⛔ **A and B must land together.** B alone removes the delay that made this visible without fixing
the stale arm — the next one would fire immediately instead of at 10:00.

**Rollback:** operator's choice of (i) straight deletion, revert = a one-line PR, or (ii) a setting
defaulting to the NEW behaviour so it can be re-enabled without a deploy. If (ii), the default must
match production or we recreate the default-vs-env divergence that has now bitten three times
(vol floor 5000/10000, reclaim gap 0/1, this).

## 7. Out of scope here, but found the same day
- The APLX schwab exit produced **no fill row** — the native-OCO child-leg blackout. Today books as
  3 round trips when there were 4, and the missing one is a loss.
- Resting reprice re-validates **nothing** (floor / established-short / bar age / STOP<=ASK all sit
  inside `if not state.resting_active:`), and the reprice trigger is direction-blind (`abs(...)`).
- Phantom close (`SPURIOUS-no-shares-ever-held`) gates on the UNION qty, never `held_qty`; it
  re-arms `fanout_webull_claimed`, permitting a second Webull leg per flip.
- `cw_arm_bar_ts` is 0 on resting orders **by construction**; `cw_entry_n` is stamped but never
  incremented on that path.
- A −5% stop does not protect, it sizes the loss: APLX rode +0.7% → −4.2% without touching it.

## 8. Tests (mutation-verified, per the standing rule)

1. A symbol promoted mid-session whose flip bar predates the join → **no entry**; suite goes red if
   the comparison reverts to `_boot_ms`.
2. The same symbol, next flip AFTER the join → **enters normally**.
3. A symbol held since boot → **unchanged** (guards the operator's exemption).
4. Dropped-then-re-added resets watch_start → pre-re-add flip rejected.
5. Strictness: a flip on the bar in progress at join → rejected (pins `>` not `>=`).
6. ORB removal: a break at 09:45 enters at 09:45 (pins the value, not just the behaviour).
7. ⛔ **Reachability test** — assert the gate is hit from the LIVE path (`_cw_v2_quote` /
   `_cw_v2_resting_track` / fan-out), not merely that the function returns the right answer. This is
   the test that would have caught all three dead-code guards.

## 9. Deploy

Design review → PR + full suite → attended deploy with the market closed → v2-only restart →
verify at the next open that (a) a mid-session promotion does not enter on a pre-join flip, and
(b) a 09:30-10:00 break now enters at its own time.
