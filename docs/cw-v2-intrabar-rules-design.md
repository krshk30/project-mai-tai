# CW v2 — intrabar break + rule-7 + reclaim (design-first)

**Status:** design for the operator-validated CW rule set. Flag-gated under a NEW sub-flag
`strategy_schwab_1m_v2_cw_v2_enabled` (requires `..._confirmed_window_enabled` on). When the sub-flag
is OFF, the existing CW (bar-close wait-3 break) is **byte-identical** — the new code paths are
skipped. Validated in backtest (`/home/trader/wt-atr-ab/atr_cw_v2.py`): 07-10 +$1.32/77%, combined
07-09+07-10 −$2.66/71% (much better than the shipped CW; still a forward test → deploy at qty 2).

## Rule changes vs the current live CW
| # | Rule | Current live CW | CW v2 |
|---|---|---|---|
| 5 | 3-bar trigger | max HIGH of the 3 bars AFTER the flip (flip bar excluded) | max HIGH of **flip bar + next 2 bars** (flip/spike bar INCLUDED) |
| 6 | Entry break | **bar-close** — first bar whose HIGH breaks the trigger, fill at trigger | **intrabar** — enter the instant a quote price breaks the trigger (`on_quote`) |
| 7 | Above-line filter | none | require, at the break instant, **price > flip level AND the forming bar's low-so-far > flip level**. flip level = the SHORT trail crossed at the BUY flip. |
| 9 | Reclaim | 1 entry per flip | **max 2 entries per BUY-flip segment** (first + one reclaim), **no cooldown** between them |
| — | ORB window | none | **no entries 09:30–10:00 ET** (ORB owns it) |
| exit | +2%/−5%/flip | unchanged | unchanged |
| window | confirm→drop | implicit in watchlist membership | unchanged (implicit) |

## Where the flip level comes from
`_update_atr_state` (schwab_1m_v2.py:842-847): on a BUY flip, `state.atr_trail` still holds the SHORT
trail (the level price crossed) at line 845, before line 846 overwrites it with `close - loss`. Add
`"flip_level"` to the returned signal dict = that pre-overwrite short trail. Non-BUY bars → None.

## State (new SymbolState fields, all cw-v2, inert when the sub-flag is off)
- `cw_trigger: float` — the frozen 3-bar-high trigger (flip bar + 2), accumulated on the bar path.
- `cw_flip_level: float` — the flip level (rule-7 line), captured at the BUY flip.
- `cw_entries_this_flip: int` — reclaim counter (cap 2).
- `cw_bar_low_so_far: float` — min quote price of the CURRENT forming bar (rule 7), reset each bar.
- `cw_v2_emit_claimed: bool` — dedup between an intrabar emit and its fill/close, cleared each bar.
(`cw_armed`, `cw_bars_waited` reused.)

## Bar path — `_cw_v2_track(state, atr_signal)` (unconditional, right after `_update_atr_state`)
Runs every new bar regardless of flat/cooldown/warmup so the trigger is always correct:
- reset `cw_bar_low_so_far = +inf`, `cw_v2_emit_claimed = False` (release stale intrabar claim).
- flip == "BUY": arm; `cw_bars_waited=0`; `cw_trigger = cur.high` (flip bar starts it);
  `cw_flip_level = signal["flip_level"]`; `cw_entries_this_flip = 0`.
- flip == "SELL": disarm (`cw_armed=False`) — segment over (also the flip-close EXIT path).
- armed & `cw_bars_waited < 2`: `cw_bars_waited += 1`; `cw_trigger = max(cw_trigger, cur.high)`.
  (after 2 increments the trigger = max of flip, +1, +2 = 3 bars incl the flip bar.)
- else: watch phase (trigger frozen); entry is intrabar in `on_quote`.
- `_cw_entry` (bar-close entry) returns None when the sub-flag is ON (no bar-close entry under v2).

## Quote path — CW-v2 branch in `on_quote` (when sub-flag on)
- `px` = last (fallback mid), as the hold path already computes.
- `cw_bar_low_so_far = min(cw_bar_low_so_far, px)`.
- gate: `cw_armed AND cw_bars_waited >= 2 (watch) AND position_qty == 0 AND cw_entries_this_flip < 2
  AND NOT cw_v2_emit_claimed AND NOT in_orb_window(now)`. **Cooldown is intentionally NOT gated**
  (reclaim has no cooldown; the entries cap + claim + flat gate + arm-on-flip prevent runaway).
- break + rule 7: `px > cw_trigger AND px > cw_flip_level AND cw_bar_low_so_far > cw_flip_level`.
- on fire: set `cw_v2_emit_claimed = True`, `cw_entries_this_flip += 1`, `last_entry_price = px`,
  return the CW open TradeIntentDraft (market buy, `_atr_qty`), metadata atr_variant="CW-v2".

## Reclaim correctness (trace)
emit#1 (claimed=True, entries=1) → fill (position>0, flat gate blocks) → exit target/stop (flat,
cooldown set but ignored, `cw_armed` still True since no SELL flip) → next bar clears claimed → next
break with entries=1<2 → emit#2 (entries=2) → fill/exit → entries=2 caps further. A SELL flip at any
point disarms (no reclaim) and is the flip EXIT. New BUY flip re-arms (entries=0).

## Safety
- Sub-flag OFF ⇒ `_cw_v2_track` not called, `_cw_entry` unchanged, `on_quote` CW branch not entered
  ⇒ byte-identical to shipped CW. Rollback = flag false + restart.
- No change to the exit path (+2%/−5%/flip) or the OMS.
- `cw_v2_emit_claimed` + per-bar clear + flat gate + entries-cap prevent double-entry / runaway.
- Session-anchor reset clears all cw-v2 fields (no cross-day carry).
- ORB-window skip removes the most volatile entries; confirm→drop is the watchlist window.
