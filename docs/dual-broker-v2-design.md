# Dual-broker v2 — design doc (v0, design-first, for review)

**Status:** BUILD approved flag-off (2026-07-10). **ENABLE is a separate gate** keyed on forward data
(confirmed-window mean **positive** over the stopping-rule window), NOT on the build date. Target:
code-complete + validated (byte-identical-off proven, qty-1 harness ready) by end of next week.

**Why build now despite the backtest verdict:** the broker-aware backtest showed the confirmed-window rule is
net-negative at real latency (BASE −0.65% mean on the Webull-routed confirmed set). That kills the *enable*, not
the *build*. Building flag-off costs only dev time + zero fleet risk; if the forward data (or an entry fix) turns
the edge positive, the plumbing is ready to flip. Build-ready ≠ enabled.

---

## 1. What already exists (scope-reducing findings)

- **`oms_managed_positions.broker_account_name`** (varchar 128, indexed) already tags each managed row with its
  broker account, and the exit path already routes by it: `adapter = self.broker_adapter._adapter_for_account(
  broker_account_name)` (`oms/service.py:3854`), backed by a `RoutingBrokerAdapter` (`_build_broker_adapter`,
  2735) that maps provider→adapter (`_build_provider_adapter`: simulated/alpaca/schwab/**webull** at 2765).
  → **A v2 leg written with `broker_account_name = <v2-webull-acct>` already has its exits routed to Webull.**
  This is the load-bearing infra and it's already built. **Likely NO new exit-read column is needed.**
- **#326 Schwab-ineligible cache** (`SCHWAB_INELIGIBLE_REASON_SUBSTRINGS = ("must be placed with a broker",)`,
  `record_schwab_ineligible_entry` on reject 3162-3163, `get_schwab_ineligible_entry`/`_has_cached_...` 3885-3893)
  already learns which symbols Schwab rejects, per-account, cached. → **This IS the learn-and-direct-route primitive.**
- **The OMS already MULTIPLEXES exit strategies by strategy, not by broker.** Two independent registries dispatched
  per-leg: `_armed_hard_stops` (keyed `(strategy, account, symbol)`) = ORB trail/native-stop; `_managed_v2_symbols`
  → `_evaluate_v2_managed_exit` = v2 CW ladder. The tick handler runs the exit for whichever registry the leg is in
  (2335/2342), which follows the placing STRATEGY, independent of the broker adapter. → **v2's CW ladder and ORB's
  trail coexist on the same Webull account, each on its own leg. No 1:1 exit↔broker requirement** (enables §3's
  account-sharing).

## 2. Routing design — Schwab-first + learn-and-direct-route (one position, one broker per leg)

On a v2 open intent, at OMS `_evaluate_risk`/submit, when `WEBULL_FALLBACK_ENABLED`:
1. If the symbol is **already cached Schwab-ineligible** (`_has_cached_schwab_ineligible_symbol`) → route **directly
   to the v2-Webull account** (skip the wasted Schwab reject round-trip). *[learn-and-direct-route]*
2. Else route to Schwab as today. On a **Schwab reject with the ineligible reason**, the existing code already
   records the ineligible entry (3162) → **re-submit the same order to the v2-Webull account** (the fallback),
   and every subsequent order for that symbol that day takes path (1). So the round-trip is paid **once per name
   per day**, not per order.
3. The resulting fill writes a managed row with `broker_account_name = <v2-webull-acct>` → exits auto-route to
   Webull via the existing `_adapter_for_account`. **One position, one broker per leg. No double-posting.**

Flag OFF = the fallback branch is never taken; every v2 order routes to Schwab exactly as today (byte-identical).

## 3. Webull account — SHARE `live:orb` (default), separate account as fallback

**Revised 2026-07-10 (operator + code verification):** the OMS **multiplexes exit strategies by strategy, not by
broker** (see §1) — a v2 leg tagged `(strategy_code=schwab_1m_v2, broker_account_name=live:orb)` gets the v2 CW
ladder while ORB legs on the same account get the trail, both executing via the Webull adapter. So **reuse the
existing ORB Webull account `live:orb`** (operator has no separate account, and provisioning one is unnecessary
for the exit logic). **Enable-time residuals to verify (smaller than a separate-account rewrite):**
- (i) **Unique `(broker_account_name, symbol)` open-row constraint** (`uq_oms_managed_positions_open_symbol`):
  ORB + v2 can't both hold the SAME symbol open on `live:orb` at once. Rare (different names); handle the edge
  (e.g. v2 skips a symbol ORB already holds on the shared account).
- (ii) **Reconciliation attribution** — the reconciler must tag each leg on `live:orb` to the right strategy.
- (iii) **ORB's 2-entry cap** (`_ENTRY_ATTEMPT_CAP`, orb_app) is ORB-strategy-internal → v2 legs should NOT
  consume it; confirm during the build.
Fallback: if (i)-(iii) prove messy at enable, provision a dedicated `live:v2_webull` account then. Not now.

## 4. Schema — reassessed (the #5 concern)

Reusing `broker_account_name` means **the exit-read row is unchanged** → the #5 "broker column touches the live
exit path" risk is largely dissolved. **VERIFY during build (gates whether ANY schema touch is needed):**
- (a) Does anything assume a v2 managed row's account == `live:schwab_1m_v2` (hardcoded), rather than reading
  `broker_account_name`? Grep the exit engine + reconciler + F2 rehydrate.
- (b) **F2 rehydrate** (`oms_armed_stops` + managed-row boot rehydrate, #394): does it carry/rehydrate
  `broker_account_name` for a v2-Webull leg, and re-arm the stop on the *correct* broker? For ORB (Webull) the
  native stop path already exists; confirm it composes with a v2-owned Webull leg.
- **IF (a) or (b) forces any new/changed persisted field** → that IS a live-exit-path schema touch and MUST earn
  its merge with a **#404-style rehydrate/survival test**: arm → persist → restart OMS → rehydrate → assert
  byte-identical exit behaviour with the field present-but-unused (flag off). If no schema change → no such gate
  needed, but we still prove byte-identical-off behaviourally.

## 5. Exits on Webull — the load-bearing risk

The CW ladder + hard stop must execute on the Webull adapter for a Webull-held leg. Reuse ORB's proven Webull
fixes: **#386** STOP→STOP_LOSS mapping, **#375** fill polling by client_order_id, **#374** 4-dec rounding,
limit+session for extended hours. **RISK:** Webull's exit path is proven for ORB's *single* stop-exit, NOT for
v2's *multi-leg CW ladder* (partial +2%, floor, 2% trail, −5% hard stop, bar-close flip) — partial fills,
cancel-on-other-leg, EH sessions. This is where the qty-1 test focuses and where surprises will surface.

## 6. Reconciliation / protected symbols / capital — per-account

- Scoping invariant already per-broker (OMS only touches positions it placed, per account) — a v2-Webull leg is a
  v2-owned position on the v2-Webull account; reconciler runs per-account. Verify the reconciler enumerates the
  new account.
- Protected symbols apply per-account (the v2-Webull account has no manual holdings initially).
- Capital/buying-power: the v2-Webull account is a separate pool; sizing unchanged (per-order qty), just executes
  on whichever broker. No cross-account netting.

## 7. Flag + merge gate

- `MAI_TAI_..._WEBULL_FALLBACK_ENABLED` default **False** → byte-identical off (no fallback branch, no new routing).
- **Merge gate:** byte-identical-off proven — behaviourally (all v2 orders → Schwab, unchanged) AND, if any
  persisted field changed, the #404-style rehydrate/survival test.
- Genuine-green CI, no admin. Attended deploy flag-off (fleet-flat).

## 8. qty-1 Webull plumbing test — gates the ENABLE, not the merge

On the shared `live:orb` Webull account: qty-1 v2-style entry → CW exit ladder → confirm each leg fills/cancels
cleanly (the ORB go-live 4-bug shakeout, expect surprises). **Do NOT enable until this passes.** Merge flag-off
first; the plumbing test + the after-hours live smoke + the forward-data gate all precede full RTH enable (§10).

## 9. Sequence

1. **Design doc** (this) → review.
2. **Routing + fallback PR** (flag-off): the fallback branch + learn-and-direct-route reusing #326 cache;
   `broker_account_name` set to the v2-Webull account on fallback fills. Byte-identical-off proven.
3. **Schema/rehydrate PR** *only if* §4 (a)/(b) forces it — with the #404-style test.
4. **Webull v2-exit adapter PR** (flag-off): the CW ladder on the Webull adapter, reusing #386/#375/#374/EH.
5. **qty-1 harness** ready (script, like ORB's).
6. **Enable** (separate, later): ops provisions the v2-Webull account → qty-1 test passes → forward data clears
   the stopping rule (**mean positive** over the window) → attended flag flip.

## 10. Enable gate (explicit, decoupled from build) — STAGED (operator plan 2026-07-10)

Flag flips through **staged live validation**, gated on forward data:
1. **qty-1 plumbing test** (§8) on `live:orb` (Webull): one buy / one sell / one stop — confirm the v2 CW-ladder
   path fills, stops, and cancels cleanly on Webull (both-broker sanity). Gates the enable, not the merge.
2. **Flag ON → after-hours live smoke:** send ONE real ATR/CW trade through Webull in a **slow / extended-hours
   market** (controlled, low-liquidity window) and watch the full entry→CW-exit lifecycle work on real money.
3. **Then RTH:** only after the after-hours smoke is clean, allow the flag on during regular hours.
4. **Standing enable condition:** the confirmed-window forward data must show **mean positive** over the
   stopping-rule window (real-latency fills, not idealized) — else the flag stays off. The current broker-aware
   backtest says this is net-negative today, so absent an entry-edge change the plumbing simply waits, built and
   ready. That is the intended, accepted outcome — build-ready ≠ enabled.

## 11. Open questions for review
- Webull account name/credentials (ops) — confirm the account handle.
- Confirm §4 (a)/(b) via grep before committing to "no schema change."
- Canary qty during the forward window (operator wrote qty 10; currently qty 2 — live-money decision, held for GO).
