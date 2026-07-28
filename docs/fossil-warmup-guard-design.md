# Fossil-warmup guard — design (NOT built)

**Status: design only.** Bar-build changes are gated behind a design doc for good reason —
`#237`'s cold-start fix was reverted, and on 2026-07-27 I shipped a guard in this exact area
(`#552`) that blocked *every* CW-v2 arm for 68 minutes and had to be rolled back and reverted
(`#554`). Nothing here is implemented.

---

## The defect

On a newly-confirmed symbol's **first** REST warmup, Schwab can return a bar series whose
**newest bar is weeks old**. v2 ingests it and builds ATR/MACD on those prices as if current.

Measured live 2026-07-27 — all three symbols confirmed that morning, at timestamps matching
their scanner CONFIRM exactly:

| symbol | first warmup | newest-bar age | real price that day |
|---|---|---|---|
| LGHL | 08:10:23 ET | ~1457h (~60d) | ~1.20–1.43 |
| BIYA | 08:11:26 ET | ~1119h (~46d) | 2.83 → 4.12 |
| ENTX | 08:41:05 ET | ~835h (~35d) | ~3.73 |

`[V2-MACD-PROBE] BIYA close=1.17..1.23` while BIYA actually traded ~2.83. **3 of 3.** This is not
an edge case; it is what the cold-start path does when Schwab has no recent data for a name.

Schwab later served BIYA correctly (398 fresh bars, including the real `08:19 BUY close=2.8300`
the operator saw on their own chart), so **the data was never missing — only the early warmup was
fossil.**

This is the nastier sibling of the known "DRY pre-market" gotcha: not EMPTY, but **STALE** — and
stale reads as valid.

## Why it matters

A flip computed on June bars is fictional. Any entry derived from it rests at a weeks-old price
level. The `#552` incident also showed the failure is *silent*: without a guard, v2 simply arms and
rests on fossil geometry and nothing in the logs says the data is old.

---

## ⛔ The trap that must not be repeated

**`[V2-CW-ARM]` fires during WARMUP REPLAY, not only live.** The whole historical series is walked
through the strategy, so ONE log instant emits dozens of arms whose bar timestamps span weeks —
e.g. GMEX: 30+ arms all logged at `10:33:07Z`, bars from 06-02 to 07-23.

Two consequences, both of which actually happened:

1. `#552` keyed its guard on **`arm_bar_ts` age > 24h**. That is a property of replay, not of
   staleness, so it blocked essentially every arm. Zero arms were possible 07:56–09:04 ET.
2. The follow-up "base rate" query counted replay arms and concluded *"81% of arms are >1 day old
   and the bot trades fine, so old arms are normal"* — measuring the wrong quantity, and using it
   to justify the rollback for the right outcome via wrong reasoning.

**`arm_bar_ts` age is NOT a staleness signal.** Any guard keyed on it will misfire.

## The correct signal

The age of the **NEWEST bar in the warmup series** — i.e. *"is this symbol's feed current at all?"*
That is a property of the data, not of how the strategy walked it.

    newest_bar_age = now - max(bar.timestamp_ms for bar in warmup_series)

---

## Proposed shape

At the end of REST warmup for a symbol, before the series is handed to the strategy:

1. Compute `newest_bar_age`.
2. If it exceeds a threshold **relative to the session**, mark the symbol **NOT WARMED / dry**
   rather than warming it with fossil data — the same spirit as `stalled_offhours_rest_dry` in the
   v2 watchdog. Never arm, never rest an order, until a genuinely fresh bar arrives.
3. Log it loudly and once per symbol (not per bar), so a dry feed is visible without spamming.
4. Re-check on each subsequent warmup attempt; the symbol becomes eligible the moment a fresh bar
   lands (BIYA proved this self-heals within the session).

### Threshold — deliberately unresolved
It must be **session-relative**, not a flat 24h. A Monday 07:00 warmup legitimately sees Friday's
close as its newest bar; a 60-day-old bar never is. Candidate: "newer than the previous trading
session's close." **Do not pick this number without checking it against a real multi-day-closure
warmup** (the multi-day warmup case in the Schwab REST gotchas).

### Where it does NOT belong
- Not at the arm decision (`#552`'s mistake).
- Not in the streamer — this is the REST warmup path.

---

## Required before implementing

1. **Base rate on the RIGHT quantity.** How often is the newest warmup bar stale, across ~2 weeks
   and all confirmed symbols? 3/3 on one morning is a signal, not a rate.
2. **Multi-day-closure check.** Prove the threshold does not mark every symbol dry after a long
   weekend or holiday.
3. **A test that pins the THRESHOLD's value**, not just the behaviour — per the standing rule, a
   threshold without a test pinning its value is unowned.
4. **Mutation-check both directions:** a guard that never fires, and a guard that always fires.
   `#552` would have been caught by the second.
5. **Overlap audit** against `_cw_boot_hold_check`, the P1.3 seed-cap, and the warmup-complete
   path — three mechanisms already gate entries at boot and a fourth must not fight them.

## Rollback shape

Flag-gated, default OFF, and **prove the OFF path is byte-identical** before enabling. `#552`
shipped "OFF by default" but was enabled in the same change; the flag never got to prove itself.

---

## Related

`project_mai_tai_v2_fossil_warmup_series` (memory) · `docs/bar-build-invariants.md` ·
`project_mai_tai_schwab_rest_gotchas` (memory) · `#552`/`#554` (the reverted attempt)
