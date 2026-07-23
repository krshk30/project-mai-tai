# Design — v2 resting flip-entry (buy-stop-limit at the ATR cross → OTOCO)

> **Status:** DESIGN-FIRST, flag-OFF. Additive second entry mode for Schwab v2. Do NOT change the
> live entry until an attended qty-2 flag-flip (PR 3). All times ET.

## Goal

Add a **second, opt-in entry mode** to Schwab v2: instead of the reactive wait-3-bar MARKET buy,
place a **resting buy-STOP-LIMIT** at the ATR trail line + a small band, which fills **at the cross**
and auto-arms the existing **+2% / −5% OCO** (one native OTOCO). The current entry is untouched and
stays the default; the new one is flag-selected and fully reversible.

## Why (the R&D basis — all real Schwab bars + Polygon quotes, honest fills)

- Entering **at the ATR cross** (vs waiting 3 bars) is the first v2 entry variant that isn't clearly
  dead: **9 trading days, in-window, 228 trades, 72% win, median +2.00%, mean +0.27%.**
- **Slippage is the crux.** A market stop fills on the fast spike (>5% slip on the majority of fills →
  net negative). A **buy-stop-LIMIT band** fills on the pullback *into* the band instead: **0.5% band →
  92% fill, 73% win, mean +0.33%, +0.16% avg slip** — beats the market stop on every metric.
- The band + "skip if it gaps past" is the endorsed textbook answer (IBD don't-chase, Dukascopy
  cancel-in-band). Both brokers support it natively (Schwab `STOP_LIMIT`, Webull `STOP_LOSS_LIMIT`),
  and it expresses as an OTOCO so the entry auto-arms the bracket. See
  [`../memory`](.) note `project_mai_tai_flip_entry_stoplimit` and `oco-bracket-design.md`.
- **Validation is LIVE, not a backtest engine** (operator decision): the crux (does the stop-limit
  actually fill on the pullback? real slippage? OTOCO routing?) is exactly what only live proves. The
  9-day study is the pre-flight; qty-2 live is the validator. Live proves the MECHANICS fast; the
  marginal +0.33% edge needs a longer forward sample either way (mechanics first, edge later).

## Current entry (what STAYS, untouched)

Reactive MARKET buy, unchanged (map: `strategy_core/schwab_1m_v2.py`, `oms/service.py`):
1. `_cw_v2_track` arms on the ATR `flip=="BUY"`, captures `cw_flip_level` (the short trail crossed =
   the rule-7 line), waits 3 bars → `cw_trigger` = the 3-bar high.
2. `_cw_v2_quote` fires intrabar when `px > cw_trigger` + rule 7 → returns a `TradeIntentDraft` (buy /
   open, **no `order_type`** → defaults to MARKET).
3. OMS `_apply_v2_oco_bracket_entry` attaches the bracket (`bracket_entry_type="MARKET"`) → OrderRequest
   (`order_type="market"`) → adapter `submit_order` → OTOCO with a MARKET parent.

## New entry (the resting flip-entry)

The ATR state machine already tracks short/long + the trail every bar. When the resting mode is ON:

- **Place:** while **SHORT**, **inside a scanner CONFIRM window**, **flat**, and **no resting order
  already live** → place a resting buy-stop-LIMIT **OTOCO** at
  **`[stop = ATR short-trail (the line), limit = line × (1 + band%)]`**, with the +2%/−5% OCO attached
  (target/stop priced off the line).
- **Replace (ratchet):** each bar, if the ATR short-trail moved (it ratchets **down**), **replace** the
  resting order to the new trail — so the stop tracks the line down and catches the cross cleanly.
  *(This is the ORB resting-entry lifecycle mirrored — ORB tracks the running-high UP; v2 tracks the
  short-trail DOWN. See `orb-resting-stopbuy-entry-design.md`.)*
- **Fill:** when price crosses the trail (the BUY flip), the resting order fills **at the cross within
  the band**, or **does not fill if it gaps past the band** (the "skip the spike" feature). On fill the
  OCO exit is already armed → never naked.
- **Cancel:** on CONFIRM-window close, EOD, or segment invalidation. On restart, **rehydrate** any
  resting order from the broker (never place a duplicate).

### Chosen defaults (operator, 2026-07-22)
- **Replace-on-ratchet: YES** (the +0.33% depends on the clean fill at the *current* trail).
- **Band: 0.5%**, but a **tunable setting** (`..._resting_entry_band_pct`) so it changes without code.
- **Gate: the same scanner CONFIRM window** as today (only rest while short *inside* a window).

## The two flags (independent — operator's call)

Decoupled so all four combos are reachable; the reactive default = today, byte-identical:

| Flag | Default | Meaning |
|---|---|---|
| `strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled` | **True** | the current wait-3 MARKET entry |
| `strategy_schwab_1m_v2_cw_v2_resting_entry_enabled` | **False** | the new resting buy-stop-limit entry |

- **Reactive ON / resting OFF** = today (default, byte-identical).
- **Reactive OFF / resting ON** = the new entry (the **first live test — resting-only**).
- **Both ON** = resting primary + reactive fallback — a **later** mode; needs **order-level dedup**
  (only one entry *order* live per name, so a fast tick can't fill both → double/oversell). NOT in the
  first build.
- **Both OFF** = a clean pause (no entries).

Reversible kill = flag off + restart **while flat** (cancel any resting order first). Nothing is
deleted; the build is purely additive.

## Build pieces & phasing

| PR | Piece | Scope | Risk |
|---|---|---|---|
| **1** | **STOP_LIMIT-master OTOCO** — new `bracket_entry_type="STOP_LIMIT"` (both `stopPrice`+`price`) in `schwab.py::_build_bracket_payload` + a Schwab **preview** validation (STEP-1-style). Adapter-only; inert (nothing emits it yet). | small | low — no live behaviour change |
| **2** | **Resting-entry emit + OMS lifecycle** — strategy emits place/replace/cancel intents tracking the short-trail; OMS resting-entry lifecycle (record order id, replace-on-ratchet with replace-race safety, cancel-on-window/EOD, restart-rehydrate); `_apply_v2_oco_bracket_entry` STOP_LIMIT branch; the two flags. | large | medium — new live-money order path (flag-OFF inert) |
| **3** | **Attended qty-2 live** — flip resting-only ON, watch real fills/slippage/OTOCO routing. | — | attended, revertable |

## Gate before any live use (like the STOP/LIMIT masters were, 07-21)

A **STOP_LIMIT-master OTOCO must be broker-preview-validated at Schwab** before PR 3. The MARKET-parent
OTOCO was never preview-checked; do not assume STOP_LIMIT works — PR 1 includes the preview.

## Edge cases (reused from the ORB resting-entry design)

- **Replace race:** never two live buy orders — cancel-confirm→place, or native Replace; a fill during
  replace must not double-fill.
- **Gap-through no-fill:** price gaps past the limit band → no fill → we skip (intended).
- **Never-naked:** the OTOCO arms the exit atomically at fill (already proven live on Schwab v2).
- **Restart-while-resting:** rehydrate the resting order from the broker on boot; never place a
  duplicate; cancel a stale one if the setup no longer holds.
- **Window/segment end:** cancel the resting order at CONFIRM-window close / EOD.

## Scope

**Schwab v2 first** (STOP_LIMIT master supported + preview-validated). **Webull STOP_LIMIT-master is
untested** (a buy-STOP master already 417s; STOP_LOSS_LIMIT as a buy master needs its own preview) →
separate, later, its own STEP-1.

## Open risks / to-confirm

- Marginal edge (+0.33%): the live test proves mechanics fast, edge slowly. Set expectations.
- The OCO children are priced off the line at placement (fill is within +band of the line); Schwab
  validates children post-trigger, so this is accept-safe, but confirm in the preview.
- The band (0.5%) is from 9 days of one regime; keep it a setting and re-measure live.
