# schwab_1m_v2 — LIVE BEHAVIOR SPEC (canonical, backtest-replay foundation)

> **Status:** CANONICAL / SOURCE-OF-TRUTH for the DEPLOYED v2 system, as of the deploy below.
> Built by reading the **deployed code + resolved live config** on the VPS, not the design docs.
> **This doc SUPERSEDES the scattered design docs for backtest-replay purposes.** Where a design
> doc disagrees with live, live wins and the disagreement is logged in the **Discrepancy Report**
> (§9) — that section is the highest-value output for anyone building the replay engine.
>
> **Deployed commit:** `3475968a2ee4608073d522df64045e44523d6659` (= `origin/main` HEAD)
> — `feat(schwab-v2): pre-market entry window opens 07:00 (was 07:30) — both modes (#538)`, 2026-07-24.
> **Account:** `live:schwab_1m_v2` (Schwab, isolated). Mirror target: `live:orb` (Webull, shared with ORB).
> **All times ET.** All config values below are **resolved from the live `Settings()`** on the box
> (env `/etc/project-mai-tai/project-mai-tai.env` merged over `settings.py` defaults), not defaults.
>
> **Provenance markers:** [READ] = read directly from deployed code/config. [INFER] = inferred from
> absence-of-path / docstring / call-site, not a direct read. Anything the operator must confirm is
> collected in §10.
>
> Superseded / reconciled docs: `schwab-1m-v2-entry-criteria.md` (retired MACD/VWAP entry — NOT live),
> `premarket-eod-exit-design.md`, `cw-v2-intrabar-rules-design.md`, `v2-resting-flip-entry-design.md`,
> `webull-mirror-on-fill-design.md`, `dual-broker-v2-design.md`, `oco-bracket-design.md`,
> `v2-overnight-flatten-design.md`, `v2-eh-exit-routing-fix-design.md`. See §9 for the line-level diffs.

---

## 1. The one thing to internalize first: which entry engine is live

The live entry is the **CW-v2 intrabar ATR-flip** engine. It is NOT the MACD-Cross / VWAP-Breakout
engine described in `schwab-1m-v2-entry-criteria.md` (that doc is retired — see §9). The selector:

- `strategy_schwab_1m_v2_confirmed_window_enabled = True` AND `strategy_schwab_1m_v2_cw_v2_enabled = True`
  ⇒ `_cw_v2_enabled` is True ⇒ `on_quote` routes to the CW-v2 intrabar path and **`_cw_entry`
  (bar-close entry) is a no-op**; the MACD/VWAP Path-1/Path-2 bar-close logic does not drive entries.
  [READ: `strategy_core/schwab_1m_v2.py` `_cw_v2_enabled` gate + `on_quote` routing + `_cw_entry` no-op]

Two entry MODES run inside CW-v2, both live and coexisting (§4/§5):
1. **RESTING** (primary): a resting **buy-STOP-LIMIT** at the ATR line, filling at the cross.
2. **REACTIVE** (fallback): wait-3-bars-then-break-the-high, intrabar MARKET (RTH) / marketable-LIMIT (EH).

The **ATR line** both modes key off = the UT-Bot-style ATR trailing stop:
`trail = close − factor × ATR(period, Wilder's)`, with **period 5, factor 3.5** [READ: `_atr_period=5`,
`_atr_factor=3.5`]. A **BUY flip** = price crossing above the short trail; the **flip level** (rule-7 line)
= the short trail value at the flip bar, captured before it is overwritten.

---

## 2. Universe / gating  (§ maps to task item 1)

| Behavior | Live rule | Gate / flag (live value) | Source |
|---|---|---|---|
| Symbol selection | scanner-confirmed names: `top_confirmed ∪ all_confirmed ∪ watchlist`, upcased/deduped, take first **25**, plus currently-held (protected) symbols; then subtract hard-protected (CYN/CELZ) and Schwab-ineligible names | `max_watchlist_size=25`; always-on | [READ] bot `_extract_confirmed_symbols`, `_apply_strategy_state_event` |
| Snapshot freshness | only scanner snapshots with `produced_at ≥ today's 04:00-ET session start` are honored | always-on | [READ] bot `_strategy_state_event_is_current` |
| Schwab-ineligible eviction | names Schwab refused to open today ("must be placed with a broker") are subtracted; DB-backed, cached ≤60s, empty in paper | always-on | [READ] bot `_schwab_ineligible_symbols` |
| **Entry window** | **07:00–16:00 ET**, weekday, non-holiday, half-open `[start,end)`, minute-granular. Enforced at the emit chokepoint for every `intent_type=="open"` (drops with `[V2-ENTRY-WINDOW-BLOCK]`). Applies to **both modes**. | `entry_window_start=07:00`, `end=16:00`; always-on | [READ] bot `_within_entry_window` → `is_fillable_et_session` |
| Resting-place window | `_resting_in_window` = **09:30–16:00 ET** (wall-clock). Opens to **07:00** only if `eh_resting_entry_enabled` (OFF live). NOTE this is a *separate* window from the emit-chokepoint one; the resting place/cancel drafts bypass the chokepoint gate. | `eh_resting_entry_enabled=False` → 09:30 start | [READ] strategy `_resting_in_window` |
| **09:30–10:00 ORB skip** | reactive intrabar breaks in `09:30 ≤ t < 10:00` are suppressed (setup stays armed, `[V2-CW-ORB-BLOCK]`). **REACTIVE ONLY** — the resting mode has no ORB skip (its window simply starts at 09:30). | always-on (in reactive path) | [READ] strategy `_cw_in_orb_window`, checked only in `_cw_v2_quote` |
| **10k volume floor** | `bar.volume ≤ 10000 → no signal` on the ATR-A/B bar-close, CW-v1 break, and hold-confirm paths. **NOT applied in the live `_cw_v2_quote` reactive intrabar path, nor in the resting path.** So under CW-v2 the 10k floor does not gate the live intrabar/resting entries. | `atr_flip_vol_floor=10000` | [READ] strategy vol-floor checks |
| Master strategy switch | `confirmed_window_enabled` + `cw_v2_enabled` both ON = the CW-v2 regime (§1). Turning `cw_v2_enabled` off reverts to bar-close CW; turning `confirmed_window_enabled` off reverts to the old ATR A/B + MACD/VWAP. | both **True** | [READ] |

---

## 3. Entry — REACTIVE  (§ maps to task item 2)

Mode flag: `strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled = True` (live). [READ]

**Wait-3 / break-the-high (exact counts)** — `_cw_v2_track` (bar path) + `_cw_v2_quote` (quote path):
- On a **BUY flip**: arm; `cw_bars_waited=0`; `cw_trigger = flip-bar high`; `cw_segment_high = flip-bar high`;
  `cw_flip_level = the short trail at the flip`; `cw_entries_this_flip = 0`.
- On each **new bar** while armed and `cw_bars_waited < 2`: `cw_bars_waited += 1`;
  `cw_trigger = max(cw_trigger, bar.high)`. ⇒ trigger = **max HIGH of the flip bar + next 2 bars**
  (the flip/spike bar is **INCLUDED**).
- **Fire** (intrabar, in `on_quote`) requires ALL of: `cw_armed` AND `cw_bars_waited ≥ 2` (watch phase)
  AND `position_qty == 0` AND `cw_entries_this_flip < max_per_flip` AND `NOT cw_v2_emit_claimed`
  AND `NOT in_orb_window(now)` AND (reactive enabled AND `NOT resting_active`) AND
  **rule 6** `px > trig` AND **rule 7** `px > flip_level AND cw_bar_low_so_far > flip_level`
  (the whole forming bar is above the flip line). `trig` = `cw_trigger` (1st entry) or `cw_segment_high` (reclaim). [READ]

**Order type — MARKET (RTH) vs marketable-LIMIT (EH):** the reactive draft carries **no `order_type`**
(defaults to MARKET). EH conversion is done downstream in the bot's `_apply_extended_hours_routing`, not
in the strategy. [READ]

**`_apply_extended_hours_routing`** (bot; **ALWAYS-ON**, restored dc11d5a 2026-06-23) — runs on every
`open` draft just before emit:
- RTH (`extended_hours_session(now) is None`) → **untouched → MARKET/NORMAL**. [READ]
- EH → look up the symbol's last quote; buy → use the **ask**; `limit_price = _format_limit_price(ask)`;
  merge `order_routing_metadata(price, side, now)` = `session=AM/PM` + `order_type=limit` +
  `limit_price=ask` [INFER on the exact tag strings — `order_routing_metadata`/`_format_limit_price` are
  imported helpers not in the two read files]. **No ask in EH → skip the entry** (returns False, warns).
- Gated only by RTH-vs-EH; **not** by `oms_v2_eh_entry_enabled`.

**P-B1 OMS cap/abandon flag** `oms_v2_eh_entry_enabled = **False**` (live). This OMS-side flag would add a
marketable buffer + max-cross **cap** (buffer `0.3%`, max-cross `1.0%`, quote-max-age `2000ms`) so a thin-EH
fill can't chase past the signal, abandoning if the ask is past the cap. **OFF ⇒ that cap/abandon is not
applied** — an EH reactive entry routes as the plain ask-priced marketable LIMIT from
`_apply_extended_hours_routing` above, with no OMS cross-cap. [READ config; INFER on the OFF-path behavior]

**EH live-bar guard (#528 mirror):** in EH only, the reactive fire requires the driving bar's timestamp be
within `reactive_entry_max_bar_age_secs = 180s` of now, else suppress (`[V2-CW-EH-STALE-BAR]`) — blocks a
warmup-replayed trigger firing pre-market on a stale bar. **RTH is byte-identical (guard skipped).** [READ]

**Per-flip cap:** `max_entries_per_flip = 2 if reclaim_enabled else 1`. Live
`strategy_schwab_1m_v2_cw_v2_reclaim_enabled = **False**` ⇒ **cap = 1 (ONE entry per BUY-flip segment)**.
`cw_v2_reclaim_gap_bars = 1` exists but is **inert** while reclaim is off. [READ]

---

## 4. Entry — RESTING  (§ maps to task item 3)

Mode flag: `strategy_schwab_1m_v2_cw_v2_resting_entry_enabled = True` (live). Managed by
`_cw_v2_resting_track` every new bar. [READ]

**Order:** a resting **buy-STOP-LIMIT** OTOCO at the ATR line:
`stop_price = line (= ATR short trail)`, `limit_price = line × (1 + band%/100)`.
`band% = resting_entry_band_pct = **0.5**` ⇒ `limit = trail × 1.005`. On fill the +2%/−5% OCO is armed
atomically (never naked — the OTOCO). [READ]

**N=3 established-short gate:** first placement requires `state_age ≥ resting_entry_min_short_bars`, i.e.
the ATR must have been **short for ≥3 consecutive bars** (`state_age` from the ATR signal). Placement branch
also requires `st=="short" AND trail>0 AND NOT resting_active`. `min_short_bars = **3**`. [READ]

**Live-bar gate (#528):** at placement, the driving bar must be within
`resting_entry_max_bar_age_secs = **180s**` of now, else skip (never rest off a warmup-replayed/stale bar). [READ]

**STOP≤ASK guard (RTH broker-stop only):** before placing a broker stop, if a fresh ask exists and
`trail ≤ ask`, skip (a buy-stop must sit above the ask); re-arm when the trail is back above market.
Fail-open when no fresh quote. Skipped in EH-software-rest mode. [READ]

**Reprice / STABLE-REST:** while already `resting_active` and short, re-place **only** when the trail has
moved `≥ reprice_pct` from the resting level: `abs(trail − resting_level)/resting_level ≥ reprice_frac`,
`reprice_pct = **0.5%** → frac 0.005`. On a qualifying move it queues a **cancel** only; the next bar
(now not `resting_active`) re-places at the new line. **At most one draft per bar** (never cancel+place
the same bar) ⇒ never two live buy orders. Sub-0.5% wiggles leave the order untouched. [READ]

**HOLD-THROUGH-FLIP:** on the up-flip (`st=="long"`, which is the fill) do NOT cancel; record
`resting_flip_ms` to start a settle grace. Cancelling would race the broker's own fill. [READ]

**SILENCE-ON-FILL:** within `resting_entry_flip_grace_secs = **30s**` of the flip, emit nothing (no orders
into fill-settle lag). On grace expiry still flat → cancel `reason="flip_no_fill"`. If `position_qty != 0`
first (fill confirmed), the top branch clears all resting state. Window close / EOD / segment-invalidation
also cancel. On restart the resting order is rehydrated from the broker (no duplicate). [READ]

**EH software-emulated cross** `strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled = **False**` (live).
When ON it would: open `_resting_in_window` start to **07:00**; place **no broker order** in EH (arm the
level in memory, `[V2-RESTING-EH-ARM]`); skip the STOP≤ASK guard in EH; and run `_eh_resting_cross_check`
which, on an EH quote crossing the armed level, emits a **marketable EH-LIMIT buy** (cap `level×(1+band)`,
`eh_resting_entry_band_pct=0.5`, `quote_max_age=2000ms`) exactly once per cross. **OFF ⇒ all of this is inert;
the resting entry is RTH broker-stop only.** [READ]

### 4a. DECIDED / LOCKED resting rules — do NOT treat as tunable in the backtest
The operator has **settled** these; the replay engine should hard-code them, not sweep them:
- **N = 3 established-short gate** (`resting_entry_min_short_bars = 3`) — **LOCKED** (operator 2026-07-24:
  keep N=3, add **no** further selectivity — no skip-if-ripping, no skip-if-whipsawing).
- **Reprice band = 0.5%** (`resting_entry_reprice_pct = 0.5`) — **LOCKED** (operator 2026-07-24: "good with
  0.5%", no further reprice tuning).

These two are decided policy, not open knobs. The entry **band** (0.5%, `resting_entry_band_pct`) remains a
setting but is at its endorsed 9-day-study value.

---

## 5. Coexistence — resting-primary + reactive-fallback  (§ maps to task item 4)

Both modes are **ON** live (this is the coexistence mode the `v2-resting-flip-entry-design.md` deferred as
"a later mode"; it is now live). [READ config]

- **Reactive stands down while a resting order is live:** `_cw_v2_quote` returns early when
  `not reactive_enabled OR state.resting_active`. So reactive fires **only in the gaps** where no resting
  order is working — e.g. before the ≥3-short-bar gate is met, during a reprice cancel/replace, or when the
  STOP≤ASK guard skipped a placement. [READ]
- **One position per symbol:** strategy requires `position_qty == 0` + a `cw_v2_emit_claimed` dedup between
  emit and fill; resting-track bails if `position_qty != 0`. Service-side, a 5s position poll counts both
  open `virtual_positions.qty>0` and in-flight open `trade_intents` as "in position", closing the
  emit→row gap. [READ]
- **N entries per flip:** cap = **1** live (reclaim off). Reset to 0 on each BUY flip and at the 04:00-ET
  session anchor. Boot-hold suppresses ALL CW-v2 entries until `_cw_boot_hold_check` confirms zero
  reconstructed-uncapped armed segments (`cw_armed_segment_safety_enabled = True`). [READ]

---

## 6. Exit  (§ maps to task item 5)

**⭐ The single most important exit fact for the backtest: there are TWO exit geometries, chosen by whether
the position opened in RTH or EH.**

Master gate: `oms_v2_exit_management_enabled = **True**`. The software eval short-circuits (returns, does
nothing) whenever `_native_oco_stand_down_active` is True for the symbol — that is what hands the exit to
the broker OCO. [READ]

### 6a. RTH-opened position → NATIVE OCO (static +2% / −5%)
On a v2 **buy-open intent submitted in RTH**, `_apply_v2_oco_bracket_entry` mutates the intent metadata so
the Schwab adapter places a **TRIGGER→OCO** combo (broker calls it OTOCO) instead of a single leg:
- **parent** = the entry (MARKET / LIMIT / STOP_LIMIT per the mode).
- **child OCO** = `SELL LIMIT @ target` + `SELL STOP @ protect`.
- **Anchor:** `entry_ref = metadata["entry_price"] or metadata["reference_price"]` — the **CW-computed
  break/reference price**, NOT the live ask and NOT the realized Schwab fill.
  `target = entry_ref × (1 + 2%) = entry_ref × 1.02`; `protect = entry_ref × (1 − 5%) = entry_ref × 0.95`.
  Rounded to the Schwab tick rule (>$1 → 2dp, ≤$1 → 4dp). [READ]
- Gates (all live-True): `oms_v2_emit_native_oco_bracket_enabled` + `schwab_native_bracket_enabled` +
  `oms_native_oco_stand_down_enabled`. **RTH-ONLY**: emitted only when `_is_regular_market_session()`; outside
  RTH it logs `[V2-OCO-EMIT] SKIPPED (outside regular hours)` and places a plain single-leg entry. [READ]
- **The OCO is STATIC** — target +2% and stop −5% are fixed at entry; there is **no trailing-stop raise** on
  the OCO stop leg (`_ratchet_trailing_stop` is ORB-only), and while the OCO is armed the software ladder is
  fully stood down, so **the bar-close ATR-flip exit does NOT fire** for an OCO'd position. An RTH position
  therefore exits at **exactly +2% (target) or −5% (stop)**, broker-arbitrated one-cancels-other. [READ]

### 6b. EH-opened (or otherwise un-OCO'd) position → SOFTWARE CW LADDER (floor-ride +2% / −5% / flip)
When there is no armed native OCO (pre-market/after-hours open, or the stand-down fails open),
`_evaluate_v2_managed_exit` runs the shared `cw_exit_decision` every quote tick. With the **live** params
`target_pct=2, stop_pct=5, floor_pct=2, floor_enabled=True`:
- **Not yet armed:** `bid ≥ entry×1.02` → **"arm"** (lock a floor at `entry×1.02`, keep riding — does NOT
  close at +2%); `bid ≤ entry×0.95` → **"stop"** (close); bar-close ATR flip pending → **"flip"** (close at
  bid); else hold.
- **Armed (floor locked at entry×1.02):** `bid ≤ floor` → **"floor"** (close); flip pending → close; else
  ride. [READ `exit_logic/cw_exit.py`]
- ⇒ the software ladder **rides past +2%** and exits when price falls back to the +2% floor, hits −5%, or a
  bar-close ATR flip fires. Precedence target/arm > hard-stop > flip.
- **This is a DIFFERENT exit shape from the RTH OCO** (which hard-closes at +2%). Because `floor_pct ==
  target_pct == 2%`, the floor sits at +2%; if the operator ever sets `floor_pct < target_pct` the ride-band
  widens. **The legacy scale/partial ladder is NOT active** (that path runs only if `confirmed_window_enabled`
  is off). [READ]

**EH exit routing (#390):** `_emit_v2_managed_sell` builds the single exit SELL:
- RTH → `order_type=market`, session NORMAL.
- EH (`_extended_hours_session()` not None, bid>0) → LIMIT + `session=AM|PM` + `extended_hours=true`:
  a **scale** exit prices at the bid (zero buffer); a **close** (hard-stop/floor/CW-full-close) prices at a
  **buffered marketable LIMIT** `_panic_limit_price(bid, buffer)` with
  `buffer = oms_v2_exit_eh_protective_limit_buffer_pct = **0.5%**`. [READ]

### 6c. 16:00 EOD OCO→ladder transition — `oms_v2_eod_oco_transition_enabled = **False**` (live, INERT)
Flag is **OFF**, so the transition is inert (byte-identical). Documented behavior **if ON** (hour/min 16:00):
at ≥16:00 ET it marks each still-open v2 position transitioned so `_native_oco_stand_down_active` returns
False for it (software +2%/−5% ladder resumes as EH-LIMITs post-16:00); it does **not** cancel the broker
legs or liquidate (decision A = KEEP MANAGING) — the RTH OCO legs (session=NORMAL/DAY) simply lapse at the
close, and the 19:55 flatten is the backstop. **Because it is OFF live, an RTH position whose OCO didn't fill
by 16:00 rides with dead RTH-only OCO legs until the 19:55 flatten** (the operator-identified 16:00→19:55
dead-OCO window still exists in the deployed system). [READ flag; behavior-if-ON is code-described]

### 6d. 19:55 overnight flatten — `oms_v2_overnight_flatten_enabled = **True**` (live, ON)
At **19:55 ET** (weekday), for every OMS-managed v2 position (`_managed_v2_symbols`, OMS-owned only — manual
holdings invisible), emit a **full-qty close** via `_emit_v2_exit_on_loop` (`reason=V2_OVERNIGHT_FLATTEN`).
It flows through `_emit_v2_managed_sell` so in AH it is a **LIMIT+session** (EH-fillable) order, not market.
**Retry-until-filled, NO per-day claim:** dedup only via `snapshot.dedup_active`; an expired unfilled limit
re-emits next 5s pass until fill or the 20:00 fillable-gate close. No-bid → LOUD error + retry. [READ]

### 6e. Pre-market-opened position across the 09:30 open (§ task item 5, last clause)
A position opened in EH has **no native OCO** (6a is RTH-only) → it is software-CW-ladder-managed (6b) and
stays that way **continuously across 09:30**. There is **NO path that converts it to a native OCO at the
open** — the OCO emitter is reached only on the entry intent, never on a periodic sweep, and a held position
generates no buy-open at 09:30. The #390 routing switches the ladder's exit from EH-LIMIT (pre-09:30) to
MARKET (post-09:30) with no gap. [READ the RTH-only entry gate; INFER the "no re-emit at open" from absence
of any re-emit path]

---

## 7. Both brokers  (§ maps to task item 6)

**Schwab = primary.** Order shapes [READ `broker_adapters/schwab.py`]:
- Single leg = `orderStrategyType:"SINGLE"`, `session` verbatim from `metadata["session"]` (default NORMAL,
  upper-cased — the adapter is a **pass-through**, the AM/PM choice is made upstream), `duration` DAY (or
  GOOD_TILL_CANCEL). MARKET = no price; LIMIT = `price`; STOP = `stopPrice`; STOP_LIMIT = both.
- Native bracket (gated `schwab_native_bracket_enabled=True` + `metadata["bracket"]∈{1,true,yes}`):
  `orderStrategyType:"TRIGGER"` parent (entry, default `bracket_entry_type="STOP"`) → `childOrderStrategies`
  = one `"OCO"` node → `[SELL LIMIT @ target, SELL STOP @ protect]`. Exit legs rounded >$1→2dp / ≤$1→4dp.
  Missing target/stop metadata → `RuntimeError` (never a half-built naked bracket). `preview_bracket_order`
  validates without placing.

**Webull = mirror-on-fill** to `live:orb` (`webull_mirror_enabled=True`, `webull_account_name='live:orb'`):
- Fires on the **confirmed Schwab FILL** (not on submit) — queued during order-sync when
  `mirror_enabled ∧ strategy==schwab_1m_v2 ∧ primary account ∧ side buy ∧ open`, fired after the sync
  session; idempotent (exactly one mirror per real fill). [READ]
- **Exit anchor:** prefers the **live ask** from `_latest_quotes_by_symbol` (Polygon/our NBBO — Webull has no
  EH market-data entitlement, so Webull is priced **off OUR feed**) if fresh (≤ quote_max_age); else falls
  back to the **Schwab fill price**. `target = anchor×1.02`, `protect = anchor×0.95`. [READ]
- **RTH (default):** **MARKET master + native-OCO combo** = MASTER MARKET buy + STOP_PROFIT (SELL LIMIT @
  target) + STOP_LOSS (SELL STOP @ protect), one `place_order` combo (`webull_native_bracket_enabled=True`,
  `webull_native_stop_order_type_map_enabled=True` maps STOP→STOP_LOSS). Log tag "MARKET+OCO". [READ]
- **EH mirror:** `webull_mirror_eh_enabled = **False**` (live) ⇒ **byte-identical to the RTH combo path**
  (an EH-open mirror would reject on Webull — MARKET/STOP 417 in EH). If ON it would build a single-leg
  marketable EH-LIMIT master (no OCO, priced off our fresh ask, buffered, capped vs the Schwab fill, ABANDON
  if no fresh ask / ask past cap) and let the software EH-LIMIT CW ladder manage the exit. [READ]
- **Collision guard:** if `live:orb` already has the symbol armed / managed / held (ORB owns it), the mirror
  is SKIPPED. Any Webull failure is swallowed and never affects the Schwab leg. [READ]
- **Webull EH order expression:** via the SDK boolean `set_extended_hours_trading(True)`, granted only for a
  LIMIT-family `order_type`; a MARKET/STOP in EH submits RTH-only with a warning. coid ≤ 40 chars, reused id
  417s (`TRADE_PLACE_ORDER_REPEAT`); combo legs get `M`/`T`/`S` suffixes. [READ `broker_adapters/webull.py`]

**Webull position-sync throttle + backoff (#537)** [READ `broker_adapters/webull.py`]:
- `list_account_positions` **throttles**: a cached success `< webull_positions_throttle_secs = 10s` old is
  returned without hitting Webull.
- On HTTP **429** / TOO_MANY_REQUESTS / RATE_LIMIT: **exponential backoff** `base=5s → ×2 → cap 60s`
  (`backoff_base=5.0`, `backoff_max=60.0`); during backoff serve last cached snapshot, or **raise
  `WebullPositionsUnavailable`** if none — **never** surface an empty/flat account (that would clear
  protective stops). Non-rate-limit errors → return `[]`. OMS reconcile interval
  `oms_broker_sync_interval_seconds = 15s`.

---

## 8. Config VALUES — the master reference table  (§ maps to task item 7)

All values **resolved from the live `Settings()`** on the deployed box (env over defaults).

### Universe / gating / mode
| Setting | Live value | Meaning |
|---|---|---|
| `strategy_schwab_1m_v2_enabled` | `True` | v2 bot on |
| `strategy_schwab_1m_v2_confirmed_window_enabled` | `True` | CW owns the ATR entry |
| `strategy_schwab_1m_v2_cw_v2_enabled` | `True` | CW-v2 intrabar regime (with the above) |
| `strategy_schwab_1m_v2_atr_only_mode` | `True` | ATR-only |
| `strategy_schwab_1m_v2_atr_flip_enabled` | `True` | ATR flip engine on |
| `strategy_schwab_1m_v2_go_live_enabled` | `True` | live orders (not paper) |
| `strategy_schwab_1m_v2_broker_provider` | `schwab` | primary broker |
| `strategy_schwab_1m_v2_max_watchlist_size` | `25` | top-N confirmed names |
| `strategy_schwab_1m_v2_atr_flip_vol_floor` | `10000` | 10k bar-vol floor (not applied in the live intrabar/resting paths — §2) |
| `strategy_schwab_1m_v2_atr_flip_use_max_state_age` | `False` | fresh-flip qualifier off |
| entry window | `07:00`–`16:00` ET | `entry_window_start_hour=7/min=0`, `end_hour=16/min=0` |
| ORB skip | `09:30`–`10:00` | reactive only |
| `oms_fillable_session_start/end_hour_et` | `7` / `20` | OMS can fill 07:00–20:00 ET |

### ATR / sizing
| Setting | Live value | Meaning |
|---|---|---|
| `strategy_schwab_1m_v2_atr_flip_period` | `5` | ATR length (Wilder's) |
| `strategy_schwab_1m_v2_atr_flip_factor` | `3.5` | trail = close − 3.5×ATR |
| `strategy_schwab_1m_v2_atr_flip_quantity` | `2` | **shares per entry (live qty)** |
| `strategy_schwab_1m_v2_default_quantity` | `10` | not used by the CW/ATR entry path |
| warmup `min_bars` | `135` | code-derived (26+9+100) |

### Reactive entry
| Setting | Live value | Meaning |
|---|---|---|
| `..._cw_v2_reactive_entry_enabled` | `True` | reactive mode on |
| `..._cw_v2_reactive_entry_max_bar_age_secs` | `180.0` | EH live-bar guard |
| `..._cw_v2_reclaim_enabled` | `False` | ⇒ **1 entry per flip** |
| `..._cw_v2_reclaim_gap_bars` | `1` | inert (reclaim off) |

### Resting entry  (LOCKED items flagged)
| Setting | Live value | Meaning |
|---|---|---|
| `..._cw_v2_resting_entry_enabled` | `True` | resting mode on (primary) |
| `..._cw_v2_resting_entry_band_pct` | `0.5` | limit = line × 1.005 |
| `..._cw_v2_resting_entry_reprice_pct` | `0.5` | **LOCKED** — re-place on ≥0.5% trail move |
| `..._cw_v2_resting_entry_min_short_bars` | `3` | **LOCKED N=3** established-short gate |
| `..._cw_v2_resting_entry_max_bar_age_secs` | `180.0` | live-bar gate (#528) |
| `..._cw_v2_resting_entry_flip_grace_secs` | `30.0` | silence-on-fill grace |
| `..._cw_v2_eh_resting_entry_enabled` | `False` | EH software-cross off |
| `oms_v2_eh_resting_entry_band_pct` | `0.5` | (EH-resting, inert) |
| `oms_v2_eh_resting_entry_quote_max_age_ms` | `2000` | (EH-resting, inert) |

### EH reactive routing
| Setting | Live value | Meaning |
|---|---|---|
| `_apply_extended_hours_routing` | always-on | EH → ask-priced marketable LIMIT (dc11d5a) |
| `oms_v2_eh_entry_enabled` | `False` | P-B1 cap/abandon **off** |
| `oms_v2_eh_entry_limit_buffer_pct` | `0.3` | (inert while above off) |
| `oms_v2_eh_entry_max_cross_pct` | `1.0` | (inert) |
| `oms_v2_eh_entry_quote_max_age_ms` | `2000` | (inert) |

### Exit
| Setting | Live value | Meaning |
|---|---|---|
| `oms_v2_exit_management_enabled` | `True` | software eval runs (when not stood down) |
| `oms_v2_cw_target_pct` | `2.0` | +2% target |
| `oms_v2_cw_hard_stop_pct` | `5.0` | −5% stop |
| `oms_v2_cw_floor_pct` | `2.0` | floor lock level |
| `oms_v2_cw_floor_exit_enabled` | `True` | **software ladder rides past +2%, floor at +2%** |
| `oms_v2_exit_eh_protective_limit_buffer_pct` | `0.5` | EH close buffered-limit |
| `oms_v2_exit_quote_max_age_ms` | `5000` *(default — not env-set; confirm)* | stale-quote guard |

### Native OCO
| Setting | Live value | Meaning |
|---|---|---|
| `oms_native_oco_stand_down_enabled` | `True` | ladder defers to armed OCO |
| `oms_v2_emit_native_oco_bracket_enabled` | `True` | v2 emits OCO on RTH entry |
| `schwab_native_bracket_enabled` | `True` | adapter builds the combo |
| `oms_native_oco_resolve_flat_reconcile_enabled` | `True` | resolve-by-fill phantom close (#514) |
| `oms_native_oco_resolve_grace_seconds` | `90` | resolution grace |
| `oms_native_oco_confirmation_max_age_seconds` | `30` | stand-down confirmation TTL |

### EOD / overnight
| Setting | Live value | Meaning |
|---|---|---|
| `oms_v2_eod_oco_transition_enabled` | `False` | **16:00 transition INERT** |
| `oms_v2_eod_oco_transition_hour/minute_et` | `16` / `0` | (if enabled) |
| `oms_v2_overnight_flatten_enabled` | `True` | **19:55 flatten ON** |
| `oms_v2_overnight_flatten_hour/minute_et` | `19` / `55` | flatten time |

### Webull mirror
| Setting | Live value | Meaning |
|---|---|---|
| `strategy_schwab_1m_v2_webull_mirror_enabled` | `True` | mirror-on-fill on |
| `strategy_schwab_1m_v2_webull_account_name` | `live:orb` | shared Webull account |
| `strategy_schwab_1m_v2_webull_mirror_eh_enabled` | `False` | EH mirror off (RTH combo only) |
| `webull_native_bracket_enabled` | `True` | Webull v3 combo path |
| `webull_native_stop_order_type_map_enabled` | `True` | STOP→STOP_LOSS map |
| `webull_positions_throttle_secs` | `10.0` | position-read coalesce |
| `webull_positions_backoff_base_secs` | `5.0` | 429 backoff start |
| `webull_positions_backoff_max_secs` | `60.0` | 429 backoff cap |
| `oms_broker_sync_interval_seconds` | `15` | reconcile / mirror-fire cadence |
| `oms_intent_max_age_seconds` | `30` | intent staleness (trigger orders exempt, #527) |

---

## 9. DISCREPANCY REPORT — design docs vs LIVE  (the highest-value section)

Each row: the design doc says X; the deployed code/config does Y. **Y is authoritative.** These are exactly
the places a replay built from the docs would be WRONG.

### D1 — `schwab-1m-v2-entry-criteria.md` — WHOLE DOC RETIRED (describes an entry engine that is NOT live)
- The doc documents **MACD-Cross (Path 1) / VWAP-Breakout (Path 2)** bar-close entries with 7 base gates.
  **LIVE the entry is CW-v2 intrabar ATR-flip** (§1); with `cw_v2_enabled=True`, `_cw_entry` is a no-op and
  Path-1/Path-2 do not drive entries. → **Do not model this doc's entry at all.**
- Line 52/111 `volume_threshold = 5000` (abs-vol gate). **LIVE ATR vol floor = 10000** (and it isn't even
  applied on the live intrabar/resting paths — §2).
- Line 60/134/157 `default_quantity = 100`. **LIVE entry qty = `atr_flip_quantity = 2`** (`default_quantity`
  is `10` and unused by this path).
- The whole "two entry paths / 7 base gates / stochastic ceiling / cooldown 5" model is **superseded**.

### D2 — `premarket-eod-exit-design.md` — STALE WINDOW + INERT/ON flag states
- Title + line 3, 23, 40–41: entry window **"07:30–16:00"**, `_resting_in_window` **"07:30–16:00"**, and
  "start → 07:30 by P-B1". **LIVE = 07:00–16:00 for both modes** (PR #538, the deployed HEAD; resting window
  start is 09:30 because `eh_resting_entry_enabled` is OFF). → the doc's 07:30 is wrong.
- §R3 / line 59–68: the **16:00 EOD OCO→ladder transition** is presented as decided/"LOCKED (KEEP MANAGING)".
  **LIVE `oms_v2_eod_oco_transition_enabled = False` — the transition is INERT.** So the 16:00→19:55 dead-OCO
  window the doc says R3 closes **still exists** in the deployed system (only the 19:55 flatten backstops it).
- §R2b / line 114–146: Webull **mirror-EH parity** — **LIVE `webull_mirror_eh_enabled = False`** (built,
  flag-off). EH mirror is byte-identical to the RTH MARKET+OCO combo (would reject in EH).
- Line 30/32: P-B1 reactive EH **cap/abandon** (`oms_v2_eh_entry_enabled`) and the EH live-bar guard —
  **LIVE `oms_v2_eh_entry_enabled = False`** (the OMS cross-cap is OFF; the EH live-bar guard in the strategy
  IS on). The reactive EH entry routes as the plain ask-priced marketable LIMIT with no OMS cross-cap.

### D3 — `v2-resting-flip-entry-design.md` — STALE flag defaults + missing the N=3 gate
- Line 68–76 flag table: `resting_entry_enabled` **default False**, "both ON … a later mode … NOT in the
  first build", "reactive default = today". **LIVE BOTH modes are ON** (`resting=True`, `reactive=True`) —
  the coexistence mode is deployed (§5). A replay assuming resting-only or reactive-only is wrong.
- The doc predates and does **not mention the N=3 established-short gate** (`min_short_bars=3`) — a LIVE,
  LOCKED placement gate (§4a). The replay must include it.
- The doc predates the **reprice band** (`reprice_pct=0.5`, LOCKED) and the **live-bar gate** (#528) — both
  live. The doc's lifecycle is "replace-on-ratchet every bar"; **live re-places only on a ≥0.5% move**.

### D4 — `cw-v2-intrabar-rules-design.md` — reclaim cap is now 1, not 2
- Line 15/28 rule 9: "**max 2 entries per BUY-flip segment** (first + one reclaim), no cooldown".
  **LIVE `cw_v2_reclaim_enabled = False` ⇒ cap = 1 entry per flip** (memory: reclaims win 38% vs 58%, turned
  off). `reclaim_gap_bars=1` is inert. A replay allowing 2 entries/flip over-trades.
- Everything else in this doc (3-bar-incl-flip trigger, intrabar break, rule-7 above-line, ORB skip 09:30–10:00
  reactive-only) **matches live** — this is the most accurate of the entry docs, except the reclaim cap.

### D5 — `dual-broker-v2-design.md` — mirror architecture SUPERSEDED (fan-out-at-submit → mirror-on-FILL)
- §2/§3: "**FAN-OUT** — a single v2 open intent submitted to BOTH accounts **simultaneously at submit**".
  **LIVE the mirror fires on the confirmed Schwab FILL, not at submit** (superseded by
  `webull-mirror-on-fill-design.md` / PR #531 — the trigger was deliberately relocated off the submit path).
  A replay that mirrors at signal-submit time mistimes the Webull leg (esp. for the resting entry, which sits
  until the cross).
- §5 account = `live:orb` and the collision guard **match live**. The A/B-telemetry framing is aspirational
  reporting, not execution behavior.

### D6 — `oco-bracket-design.md` — anchor detail + entry-leg default
- Structure (TRIGGER→OCO, +2% LIMIT / −5% STOP, RTH-only, stand-down defer) **matches live**.
- Line 47–51 shows the parent as a **buy-STOP** at the break level (the ORB-style ticket). **LIVE the v2 OCO
  parent type mirrors the intent** (`bracket_entry_type` = MARKET for reactive, STOP_LIMIT for resting) — the
  Schwab adapter's bracket *default* is STOP but v2 overrides it per mode.
- Anchor: the doc implies target/stop off "entry"; **LIVE anchor = `entry_price/reference_price` (the CW
  break/reference price), not the fill** (§6a). Small but load-bearing for exact target/stop levels.
- The "trailing ratchet — still live OMS work" (line 123): **NOT implemented for the v2 OCO** — the OCO is
  static +2%/−5%, no stop-raise (§6a).

### D7 — `v2-overnight-flatten-design.md` — stale window context (mechanism matches)
- Line 8/113: "v2's entry window runs to **16:30**". **LIVE entry window ends 16:00** (#532). The 19:55
  flatten mechanism itself (retry-until-filled, no per-day claim, LIMIT+session, `_managed_v2_symbols`)
  **matches live and is ON.**

### D8 — Webull mirror exit anchor — doc "decision" vs live
- `webull-mirror-on-fill-design.md` §3b "Decision 2 — exit anchor: the WEBULL fill", then an impl-note to use
  the live ask at submit. **LIVE anchors the mirror exits off the fresh live ask from OUR feed
  (`_latest_quotes_by_symbol`), falling back to the Schwab fill price** — not the Webull fill (Webull has no
  EH data entitlement). Reconciled: the live behavior follows the impl-note (ask), not the headline
  ("Webull fill"). RTH master = MARKET + OCO combo, matching Decision 1.

---

## 10. Items needing operator confirmation / marked-inferred

1. **[INFER] `_apply_extended_hours_routing` tag strings** — the exact `session=AM/PM` token and
   `_format_limit_price` rounding live in imported helpers (`order_routing_metadata`, `_format_limit_price`,
   `is_fillable_et_session`, `extended_hours_session`) not in the two read strategy/bot files. Behavior above
   is from docstrings + call sites. Confirm the AM/PM boundary and any limit rounding if the replay needs
   exact EH fill prices.
2. **[INFER] pre-market position never OCO-converts at 09:30** — established from the *absence* of any
   OCO re-emit path + the RTH-only entry gate (read directly). No positive code path was found that would
   convert it; confirm there is no other sweep.
3. **`oms_v2_exit_quote_max_age_ms = 5000`** is the settings default and was **not** env-overridden — confirm
   the live value if the replay models the stale-quote exit guard.
4. **EH-open exit geometry ride-band** — with `floor_pct == target_pct == 2%`, the software ladder's floor
   sits exactly at +2%. Confirm this is intended (a floor below the target would create a wider ride band).
5. **Reactive EH entry with `oms_v2_eh_entry_enabled = False`** — confirmed the OMS cross-cap is off; the
   [INFER] is only on whether any residual EH-entry abandon logic runs with the flag off (code path suggests
   not — it routes the plain ask-priced marketable LIMIT).
6. The **09:30–16:00 resting window vs 07:00–16:00 reactive window** asymmetry is real (resting starts 09:30
   because EH-resting is off; reactive can route EH via `_apply_extended_hours_routing` from 07:00). Confirm
   this asymmetry is intended for the backtest (resting cannot fire pre-market today; reactive can).
</content>
</invoke>
