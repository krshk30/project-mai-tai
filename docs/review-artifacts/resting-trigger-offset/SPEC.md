# SPEC — resting entry trigger OFFSET above the ATR flip line (operator 2026-09-22: "go with 1%")

**Written by `claude-1` 2026-09-22 ~18:40 ET; amended 2026-09-23 after `codex-2`'s review (F1–F4 below). For `codex-2` to build and review. Nothing built yet.**

## What the operator asked, and what the code actually does today

Operator's picture: *"we have a 0.5% buffer, we are buying 0.5% above the flip; make it 1% so a false flip never fills."*

Code (`schwab_1m_v2.py:4187`, `:4887`; `settings.py:556`): the resting entry is a **buy-stop-limit with the STOP AT the
flip line** and `limit = line × (1 + resting_entry_band_pct)`, band 0.5%. **The 0.5% is a slippage cap, not an offset.**
We buy at the line ([[resting fill vs trigger +0 bps]] memory). Raising the band to 1.0% would do the OPPOSITE of the
operator's intent: same trigger, worse permitted fills.

⛔ **Labelling error in my replay, corrected here:** the rows I called "+1.0% band" and "+1.5% band" modelled the trigger at
`fill × 1.005` and `fill × 1.010` — i.e. a trigger **0.5% and 1.0% ABOVE the line**. So:

| replay label | what it really is | month total (Schwab v2, 158 trips, 08-24→09-22) |
|---|---|---|
| actual | trigger at the line, limit +0.5% | **−50.8%** |
| "+1.0% band" | **trigger 0.5% above the line** | **−24.4%** (30 losers avoided −93.6; 0 winners lost; 83 winners each ~0.5% worse; 2 targets no longer print) |
| "+1.5% band" | trigger 1.0% above the line | −28.5% (42 losers avoided; 5 targets lost; median trade +0.9 vs +1.5) |

**The number that worked is a 0.5% offset. A full 1.0% offset is worse.** The operator's "1%" maps to the first row
(his mental "0.5 → 1.0"); the spec implements a 0.5% offset and says so plainly.

## The change

New setting, **default 0 = byte-identical to today**:

```
strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct: float = 0.0
```

Semantics, both places the line is used (`_place_resting_entry` ~4187 and the reactive cap ~4887):

```
trigger = line × (1 + offset_pct/100)          # stop price of the buy-stop-limit
limit   = trigger × (1 + band_pct/100)         # the existing slippage cap, unchanged, now relative to the trigger
```

- The **Webull mirror** rests at the same trigger (it mirrors the Schwab resting order; PA1's 8% distance check is
  measured from the market, unaffected).
- Reprice cadence (`stable-rest`, 1.0% moves of the trail) unchanged — it moves the LINE; the offset rides on it.
- The reactive-break cap (`:4887`) uses the same trigger so a quote-path entry cannot fire below the offset.
- Logs: see F4 below — `line=`, `trigger=`, `offset_pct=` on every placement, reprice, EH and mirror marker.
- Deploy value for the operator's ruling: **`0.5`**, set in the env file, one flag on its own day (never with LC1's first
  day or with C).

## Frozen after `codex-2`'s review of the first draft (2026-09-23) — three blocking details

**F1 — the stop-vs-ask admission guard must use the computed TRIGGER, not the raw line.** Production
(`[V2-STOP-ASK-PRICE-CHECK]`, ~:4834) checks *line vs ask* before placement, for BOTH the first rest and the reclaim rest.
With an offset, line 9.98 / ask 10.00 / trigger 10.0299 is a valid stop (above the ask) that today's guard would refuse.
Both guards take the trigger. **Boundary test:** line below the ask, trigger above it ⇒ admitted; line and trigger both
below ⇒ refused (the control).

**F2 — `state.resting_level` is the REPRICE baseline and must stay the LINE.** Production reprices when the next raw
line moves ≥ `_resting_reprice_frac` (1.0%) from `state.resting_level` (~:4581 first, ~:4770 reclaim). If the trigger
were stored there, a true 1.0% line move would read as ~0.5% and reprice would wait for ~1.5%. Therefore: keep
`state.resting_level = line` (unchanged), add `state.resting_trigger = trigger` for the order, and the reactive-cap /
mirror-cross comparisons (~:4884, ~:5199, ~:5244) use `resting_trigger`. **Pin the boundaries** for first AND reclaim:
line move 0.99% ⇒ no reprice; 1.00% ⇒ reprice; offset 0 ⇒ identical to today.

**F3 — metadata semantics once line ≠ trigger** (the tape must keep the underlying line to grade this change):

| key | value | why |
|---|---|---|
| `cw_flip_level` | the ORIGINAL ATR line | unchanged meaning; the grading key |
| `stop_price` | the offset trigger | what rests at the broker |
| `limit_price` | `trigger × (1 + band)` | the cap, now relative to the trigger |
| `reference_price` | the trigger | slippage is measured from what we asked for |
| `entry_price` | the trigger | the exit ladder's ±% anchors follow the fill anyway |
| `resting_offset_pct` (new) | e.g. `"0.5"` | alongside `resting_band_pct` |

Same keys on the first rest (~:4226), the reclaim rest (~:4291), the reactive entry (~:4930) and the Webull mirror
intent; the mirror's stop = the same trigger.

**F4 — markers, by their real names.** `[V2-RESTING-PLACE]` (not "PLACED"), `[V2-RESTING-CANCEL] reason=reprice`,
`[V2-RESTING-EH-ARM]` / `[V2-RESTING-EH-CROSS]` / `[V2-RESTING-EH-DISARM]`, `[V2-WEBULL-RESTING-PLACE]` /
`[V2-WEBULL-RESTING-CANCEL]`, `[V2-FANOUT-RTH-RESTING]`, `[V2-STOP-ASK-PRICE-CHECK]`: each prints `line=`, `trigger=`
and `offset_pct=` next to the existing `band_pct=`.

## What it does NOT change (say so in the PR)
- Exits: target +5%, hard stop −5% from the FILL, confirmation exit — all unchanged. The 5% → 3% target is the operator's
  NEXT question, measured separately.
- Retries on the same name (RETRY1) and the last-30-min downtrend (TREND1) — suspended by the operator; not in this.
- 09-21's −25% (dropped Webull exits, fixed by #1028) is untouched by this: the offset avoids false flips, not bad exits.

## Tests codex-2 should write (behavioural, on the real strategy path)
1. offset 0 ⇒ stop and limit byte-identical to today's (the control; the fixture must match production config —
   `band_pct=0.5`, `offset_pct=0`).
2. offset 0.5 ⇒ stop = line×1.005, limit = line×1.005×1.005; the mirror intent carries the same stop.
3. A flip whose price never rises 0.5% above the line after the cross ⇒ NO fill (today's TOPS 09:50 / QNME / DCOY ×2 /
   JAGX #1 / RAIN #2 shape — the six avoided trades of 09-22).
4. A flip that rises 0.6% ⇒ fills at ≤ line×1.005×1.005 (the winners' shape; JAGX 14:53, RAIN 15:21).
5. Reprice moves the line ⇒ the trigger moves with it, offset intact.
6. Mutation: offset applied to the limit only (the wrong reading of the ask) ⇒ tests 2–3 RED.
7. F1 boundary: line < ask < trigger ⇒ admitted; line and trigger < ask... (i.e. trigger below the ask) ⇒ refused. First AND reclaim.
8. F2 boundaries: line move 0.99% ⇒ no reprice, 1.00% ⇒ reprice, with offset 0.5 — first AND reclaim; offset 0 identical to today.
9. F3: the placed intent's metadata carries `cw_flip_level == line`, `stop_price == trigger`, `limit_price == trigger×(1+band)`,
   `resting_offset_pct`; the mirror intent's stop equals the Schwab trigger.

## Evidence and its limits
- Replay = trigger at `fill × (1 + offset)`, "fills" if a later 1-min HIGH reached it inside the actual trip's life, exit
  unchanged except a +5% target re-checked from the higher entry. Directional, not a full backtest: it cannot see trades
  that would have existed only under the offset, nor the reprice chain's interaction.
- 19 sessions: 9 better, 10 slightly worse (the winners' haircut on days with no false flip). The gain is concentrated:
  09-22 (−27 → −13), 09-15 (−10 → −3), 08-26 (−15 → −9).
- Pass mark after deploy: over 5 sessions, count "flip crossed, offset never reached, price then fell" (avoided losers)
  vs "offset reached, then +5% target missed by < 0.5%" (the haircut cost), by name. UNEXERCISED until then.
