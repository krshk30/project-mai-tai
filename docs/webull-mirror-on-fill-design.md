# Design: v2 → Webull mirror, on-fill (not on-submit) — market/limit master + native OCO

**Status:** DESIGN-FIRST (2026-07-23). No code yet. Implement + test 2026-07-24.
**Author context:** operator-directed after the 07-23 live run. "Fix the Webull first… design first,
then implement. Tomorrow we can test." Do NOT disable the ATR/resting entry — bots keep running.

Related: [`oco-bracket-design.md`](oco-bracket-design.md),
[`oco-step1-runbook-webull.md`](oco-step1-runbook-webull.md),
memory `project-mai-tai-resting-entry-out-of-window-bug`.

---

## 1. The problem (root-caused 07-23, live)

The v2→Webull mirror REJECTS every resting entry. Two independent defects:

**Defect A — verbatim order-type copy.** `_maybe_mirror_v2_open` (service.py:1093-1094) makes a
*faithful* copy of the primary Schwab request metadata, including `bracket_entry_type="STOP_LIMIT"`
and `order_type="stop_limit"`. Webull's combo builder correctly refuses a buy-STOP master
(webull.py:598, "Fork A" — a buy-STOP master 417s: `invalid order_type, value: STOP_LOSS`). We are
handing Webull a shape it structurally cannot accept.

**Defect B — wrong trigger MOMENT (the important one).** The mirror fires inside
`process_trade_intent` — i.e. **when v2 PLACES the order**, not when it FILLS. For the old reactive
market entry, place ≈ fill (the cross already happened), so on-submit was fine. For the RESTING
entry the Schwab order just *sits* until the up-cross. Mirroring at placement with a Webull market
buy would enter Webull **immediately, before the cross** — early and wrong. Even if we fixed
Defect A alone, a submit-time market mirror would front-run the signal.

## 2. What the broker actually supports (operator's TurboTrader screenshots, 07-23)

Webull's TurboTrader ladder attaches a bracket to **Buy @MKT / Buy @ASK** — a market or
marketable-limit entry — plus **Take-Profit (limit)** + **Stop-Loss (stop)**. There is no "resting
buy-stop that carries a bracket." This CONFIRMS Fork A from the broker-UI side, and it is exactly
the shape our combo builder already emits (MASTER LIMIT/MARKET + STOP_PROFIT + STOP_LOSS), validated
in STEP-1 (preview PASSED 07-22; Phase-3 combo Stage A/B/C PASSED on PAVS).

**Key conclusion (non-obvious):** because Webull cannot rest a stop-ENTRY *with* an atomic bracket,
the ONLY way to get an atomic OCO on Webull is a market/limit master. And the only way to place that
master at the correct MOMENT (the cross) is to trigger it off the Schwab **fill**. So mirror-on-fill
is not a convenience or a shortcut — it is the single structurally-sound option Webull allows. This
also answers "should we even mirror?": yes, on-fill; a native-Webull *resting* entry with an atomic
bracket is impossible on the OpenAPI (would require splitting entry from bracket = a naked window,
violating OTOCO principle #4). Mirror-on-fill keeps the bracket atomic.

## 3. The fix

Move the mirror trigger from **placement** to **fill**, and collapse the master to market/limit.

### 3a. Trigger relocation
- Remove the call from `process_trade_intent` (service.py:966).
- Fire the mirror from the **async fill path**. A resting order's fill is detected by
  `sync_broker_orders` (service.py:2651), which calls `_apply_managed_position_after_fill`
  (service.py:1571) and logs `[OMS-V2-MANAGED-OPEN]`. That log line is the exact fill moment
  (it fired for SKYQ at 15:45). `_apply_managed_position_after_fill` is **sync**, `submit_order` is
  **async**, so the mirror cannot live inside it — fire it from the async caller *after* the hook
  returns, guarded on: flag on + `strategy_code=="schwab_1m_v2"` + primary account + `side=="buy"` +
  `intent_type=="open"` + a *newly created* managed row (not an idempotent re-sync).
- Both `_apply_managed_position_after_fill` callers exist: `sync_broker_orders` (2769, the resting-
  fill path) and `_record_order_reports` (4360, immediate reports). A resting fill comes through the
  former. Design the trigger so it fires once per real open regardless of which path saw the fill
  first (see 3c idempotency).

### 3b. Metadata transform for the Webull leg
From the confirmed fill (`price` = actual Schwab fill, e.g. 5.73), build a Webull combo request:
- `order_type` / `bracket_entry_type` → **MARKET** (simplest; enters at the ask) OR **LIMIT** at the
  live ask (`bracket_entry_type=LIMIT`, `limit_price=ask`) for slippage control. Decision below.
- DROP `stop_price` (the resting trigger — irrelevant, the cross happened).
- KEEP the two OCO exits: `bracket_target_price`, `bracket_stop_price`. Recompute from the **Webull**
  fill? No — anchor them to the **Schwab fill price** (same geometry both brokers) OR to the Webull
  fill for a true independent leg. Decision below.
- `bracket="true"`, `native_oco_bracket="true"`.

**Decision 1 — master type: MARKET** (operator, 2026-07-23). Guaranteed clean bake-off fill on a
cheap liquid tight-spread name; changeable to marketable-LIMIT later if slippage proves material.

**Decision 2 — exit anchor: the WEBULL fill** (operator, 2026-07-23). Exits must be sane relative to
what Webull actually paid, else a Webull entry above the Schwab fill could place a target below its
own cost. Compute target/stop from the Webull master fill via `cw_target_pct` / `cw_stop_pct`.
IMPLEMENTATION NOTE: the combo arms all three legs atomically, so we cannot read the Webull master
fill *before* arming the exits. Resolve by anchoring the combo's exits to the **live ask at submit**
(the expected market-fill price) rather than a post-fill read — a MARKET master fills at/near the
ask, so target/stop geometry is correct to within the spread. Log both the assumed-ask anchor and
the realized Webull fill so any drift is measurable (and we can switch to a post-fill re-arm later if
the drift matters).

### 3c. Idempotency & safety (critical — `sync_broker_orders` runs every ~15s)
- The managed-row create is already idempotent (existing-row check, service.py:1599). The mirror
  MUST be equally idempotent: reuse the existing collision guard (armed_here / managed_here /
  held_qty on the Webull account, service.py:1058-1074) so a re-sync never double-places.
- Fire the mirror ONLY on the transition to a *new* managed row (the create actually happened), not
  on every sync that re-observes an already-open position.
- Keep the whole mirror fail-safe-wrapped (own session, swallow all Webull errors) — a Webull
  failure must never unwind the committed Schwab leg. (Unchanged from today.)
- Shared `live:orb` account: the collision guard already prevents fighting ORB for the same symbol.

## 4. Latency artifact (acknowledge, don't fix)
Webull enters up to one reconcile interval (~15s) after Schwab fills — the mirror waits for
`sync_broker_orders` to observe the Schwab fill. For a bake-off this is an acceptable, *measurable*
artifact (arguably realistic for a follow-broker). If it proves too laggy, a later optimization is
to fire off the fill-event stream instead of the reconcile poll. Log the Schwab-fill→Webull-submit
delta so we can quantify it. Do NOT silently cap or hide it.

## 5. Test plan (2026-07-24, attended, RTH)
1. Unit: mirror metadata transform (STOP_LIMIT→MARKET, stop_price dropped, exits present); trigger
   fires once per new managed row, never on re-sync (mutation test: force a double-sync → one place).
2. Off-hours: `scripts/webull_otoco_preview.py` shape unchanged (MARKET master accepted).
3. Attended live qty-1/2 on a cheap liquid name NOT on ORB's watchlist: let v2 rest+fill on Schwab,
   verify the Webull leg fires on the fill (not before), the combo is accepted, and the OCO arms
   (get_order_open shows MASTER filled + STOP_PROFIT/STOP_LOSS working). Confirm one-cancels-other on
   resolution. Verify no double-place across ~4 reconcile cycles.
4. Confirm the Schwab leg is byte-identical unaffected with the mirror flag on vs off.

## 6. Rollback
`MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_MIRROR_ENABLED=false` + restart → fully dormant, byte-identical
Schwab path. No schema changes. The relocation itself is guarded by the same flag.

## 7. Out of scope
- Native-Webull *resting* entry (impossible atomically — see §2).
- Replace-throttle per-1min (separate, queued after this).
- Webull OMS read-side (`fetch_armed_native_oco_symbols` / `fetch_oco_resolved_by_fill_symbols` for
  Webull) — still deferred; the mirror's own stand-down/resolve on Webull is a follow-on once we have
  the combo read shapes captured from the live qty-1 run.
