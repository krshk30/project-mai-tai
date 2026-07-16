# ORB resting-bracket entry (catch the original open break, never naked at fill) — design

> **Status:** DESIGN-FIRST. Do NOT build until **STEP 1 (the OTOCO validation test)** passes — an
> unattached/late stop = naked on a gapper, the exact risk this eliminates. Sequenced AFTER the
> consume-loop latency fix (PR #387) and the ORB phantom-position fix (both prerequisites).

## TL;DR
Replace ORB's current "buy back at the broken level within 1.5%" entry (a de-facto pullback/reclaim that
vertical gappers never fill) with a **resting native bracket** placed at ~09:25: a **buy-stop entry just above
the pre-open high with an attached stop-loss** (Webull `OTOCO`/`OTO` combo), so the instant the entry fills the
protective stop is **already live at the broker** — never naked, even on a same-instant 20% drop. Max **2 entries
per 09:30–10:00 window** (original break + one reclaim), then done.

## Why the current entry misses gappers (CELZ 2026-06-30, evidence-pinned)
Running-high mode confirms the break at **bar close** and fills at the **broken level**. On CELZ the break
(price crossing the 2.70 running-high) happened at **09:30:06.145** and price passed the 1.5% gap-cap bound
(2.7405) at **09:30:06.259** — a **114-millisecond** fillable window. By the 09:31:00 bar close the price was
already ~3.12, far past the level → the "buy at 2.70" can't fill → gap-cap abandons. **It's structural:** an
enter-at-the-broken-level + bar-close design is a pullback entry, and straight-up gappers don't pull back. To
catch the *original momentum* you must watch the live tick stream and fire on the cross — which is what a resting
buy-stop bracket does, with the stop attached so the chase is never naked.

## Feasibility (verified against the installed SDK 2026-06-30)
- **Q1 — Native brackets? YES, US-supported.** Webull v3 OpenAPI (`order_opration_v3.place_order(account_id,
  new_orders, client_combo_order_id)`) supports combo orders; `ComboType` = `OTO`/`OCO`/**`OTOCO`**/`STOP_LOSS_PROFIT`.
  Docstrings explicitly list **Webull US**. `OTOCO` = entry that, on fill, atomically activates an attached
  stop(+target) at the broker. **Caveat:** our adapter uses the single-leg `PlaceOrderRequest` today — the v3
  combo path is NEW adapter work — and we've only verified a standalone SELL `STOP_LOSS` (the 06-30 F test), NOT
  a combo. So "supported by SDK + US-listed" ≠ "our account accepts this exact `new_orders` shape" → STEP 1.
- **Q2 — If we instead armed the stop on fill (no native bracket): ~5s gap.** The OMS detects fills by polling
  (`oms_broker_sync_interval_seconds=5` / `oms_working_order_refresh_seconds=5`), then places the stop → a
  ~5-second unprotected window, exactly the fast-drop risk. **So the OMS-on-fill path does NOT make "never naked"
  true — only the native OTOCO does.** This is why Q1=YES is the gate for safety, not just convenience.
- **Q3 — Gap-over-the-level: clean no-fill via buy-stop-LIMIT.** A buy-stop-**limit** (trigger above the pre-open
  high, limit = the wider open-window ceiling) gives a clean no-fill if the stock opens above the ceiling — no
  chase. Maps to a **BUY-side `STOP_LOSS_LIMIT`** (`side=BUY`, `stop_price`, `limit_price`). **Caveat:** only a
  SELL `STOP_LOSS` is verified; buy-side stop-limit (and as a bracket leg) needs its own acceptance check.
- **Q4 — 2-entry cap: enforceable, but count FILLS not emits.** Replace `st.traded` (bool) with a per-symbol
  fill counter capped at 2. **Must increment on a confirmed fill** (else it inherits the phantom-position bug —
  an abandoned entry would burn a slot). Gated on the phantom fix.
- **Q5 — CELZ through it:** 09:25 place OTOCO: buy-stop-limit at ~2.71 (just above 2.70), limit ceiling = the
  WIDE open-window cap (~2.90, not 2.74), attached stop ~2.50. At 09:30:06 the entry triggers and fills ~2.75–2.90
  (catches the move), and the 2.50 stop is **live at fill** — never naked. vs today: missed entirely.

## STEP 1 (the GATE) — far-from-market OTOCO validation, before ANY real ORB use
**The controlled qty-1 OTOCO validation IS the in-market test — NOT a live ORB gapper.** It runs in
market hours (the combo/stop-arm behaviour can't be exercised after close, same as the broker-stop) but on a
deliberate far-from-market qty-1 order, because an *unvalidated* bracket on a real gapper = **naked if the stop
doesn't arm** — the exact risk this design eliminates. Build the v3 combo adapter → run THIS validation →
*then* wire it live. Do not skip the controlled step. Same discipline as the 06-30 F test, but for a bracket.
On `live:orb`, qty-1, controlled:
1. Place an **OTOCO** (or OTO): buy-stop-limit entry **far above market** (so it can't trigger) + attached
   stop-loss far below. Confirm Webull **accepts** the combo (`new_orders` shape + `client_combo_order_id`) — no
   error — and the parent **rests** as a working combo.
2. Optionally trigger one on a held qty-1 lot (buy-stop just above market) and confirm **the attached stop goes
   live at the broker the instant the entry fills** (query open orders: the stop is present + working) — this is
   the whole point (never naked).
3. Confirm **buy-side STOP_LOSS_LIMIT** acceptance (Q3) and the **cancel** of a resting combo (so an un-triggered
   bracket is cleanly cancelled at the 10:00 window close).
4. Cancel/flatten, account flat. **Only if all pass does any build proceed.** "Deploy-and-find-out-live" is
   forbidden: an unattached stop is the naked-on-a-gapper risk we are eliminating.

## Design (only after STEP 1)
- **Adapter:** add a v3-combo place path to `WebullBrokerAdapter` (`order_opration_v3.place_order` with
  `new_orders` legs + `combo_type=OTOCO`/`OTO` + `client_combo_order_id`), plus combo cancel/detail. Keep the
  existing single-leg path unchanged; gate combos behind a flag.
- **OMS:** a "place resting bracket" intent type → the adapter combo path; track the combo as one managed unit
  (parent + stop child) in `oms_managed_positions`; reconcile combo state on restart.
- **ORB:** at ~09:25 (universe frozen), for each pre-09:25 name, emit ONE resting buy-stop-limit bracket
  (entry just above the pre-open high; ceiling = a new `orb_open_window_gap_cap_pct`, wider than the 1.5%; stop =
  `orb_initial_stop_pct`). On trigger/fill → managed by the OMS trail. **2-entry cap by fill count.** Cancel any
  un-triggered bracket at the 10:00 cutoff.
- **The TRAIL is still live OMS work (unchanged by this).** The bracket makes the *initial* stop atomic-at-fill;
  the trailing ratchet still requires the OMS to **replace the bracket's stop leg** as price rises (cancel/replace,
  now broker-side, must avoid a gap during each replace). Trail keep-up depends on the OMS tick-consumer (already
  tick-by-tick, #333) — a different path from the ORB consume-loop lag (PR #387).

## Risks / open questions
1. **Combo acceptance + exact `new_orders` shape** — STEP 1 must confirm (SDK-listed ≠ account-accepted).
2. **Buy-side stop-limit** acceptance (only SELL verified).
3. **Stop-leg replacement for the trail** — cancel/replace churn on the broker bracket; the replace must not open
   a naked gap (consider OTOCO `replace_order` vs cancel-then-add).
4. **Resting order across the 09:30 cross** — a buy-stop placed pre-open: does it arm correctly through the open
   auction? extended-hours flag? STEP 1 / the first window must confirm.
5. **Wider open-window gap-cap = deliberate chase risk** — a wider ceiling will sometimes fill a spike that
   reverts (cf. the INTZ hold-confirm slippage); size the ceiling from backtest, not gut.
6. **2-entry cap depends on the phantom fix** (count fills not emits).

## Sequencing
1. **Consume-loop latency fix (PR #387)** — prerequisite; a tick-driven entry is worthless if the stream is 1:47 behind.
2. **ORB phantom-position fix** (count fills / reset on `[OMS-ABANDON-INTENT]`) — gates the 2-entry cap.
3. **STEP 1 OTOCO validation** (this doc) — the go/no-go gate.
4. Adapter combo path → OMS bracket path → ORB resting-bracket emit → attended first-window validation.
