# schwab_1m_v2 — paper → live credential transition scoping

> **STATUS: SCOPING / READ-ONLY (2026-06-16).** This document describes EXACTLY what the
> eventual paper→live conversion of `schwab_1m_v2` (v2) involves so go-live is a clean,
> attended, staged step. **Nothing in this doc has been flipped, changed, or deployed.** All
> file:line references and setting names were verified against the working tree at authoring
> time. Target config: ATR-ONLY, qty 10, staged.

---

## 1. Summary

v2 is today **structurally paper** — it *cannot* route an order to the real Schwab account, by
two independent, deliberate layers added in P1 Phase 1 (PR #276):

1. **Provider default = `"simulated"`** (`settings.py:168`). v2's orders route to
   `SimulatedBrokerAdapter`, never `SchwabBrokerAdapter`. Routing is decided at the *provider*
   level (`RoutingBrokerAdapter._adapter_for_account`, `routing.py:42-44`), **before** any
   account-hash lookup.
2. **Hash-side guard** (`schwab.py:51-52`): `configured_schwab_accounts` explicitly refuses to
   register `paper:schwab_1m_v2`, so even if the provider were flipped back to `"schwab"`, v2's
   account can never bind a real Schwab hash → `submit_order` would reject "missing Schwab
   account hash".

Going live means **deliberately defeating both layers** plus wiring a credential/hash, in an
attended ceremony. The ATR-only entry shape is achievable via existing flags **with one
exception that needs code** (see §4). The single most important non-credential precondition is
the **04:00 ET watchlist-staleness race** (GO-LIVE BLOCKER #4), which must be fixed first.

The transition is mostly **config (reversible)**; the one **irreversible** thing is a real fill
at the broker.

---

## 2. Current isolation — what it is, with file:line

### 2.1 Layer 1 — provider default routes v2 to the simulated sink
- `settings.py:163` — `strategy_schwab_1m_v2_account_name: str = "paper:schwab_1m_v2"`
- `settings.py:168` — `strategy_schwab_1m_v2_broker_provider: str | None = "simulated"`  ← **was `"schwab"`; the P1 flip**
- `settings.py:611-614` — `provider_for_strategy("schwab_1m_v2")` returns this override.
- `settings.py:629-630` — `provider_for_account("paper:schwab_1m_v2")` → `provider_for_strategy("schwab_1m_v2")` → `"simulated"`.
- `routing.py:42-44` — `RoutingBrokerAdapter._adapter_for_account` picks the adapter purely by `provider_by_account[account]`. With `"simulated"`, v2 orders go to `SimulatedBrokerAdapter`; the Schwab account map is **never consulted** for v2.
- `runtime_registry.py:239-250` — `configured_broker_account_registrations` builds the OMS `provider_by_account` map from `provider_for_account(...)`. v2 registers as provider `"simulated"`.

### 2.2 Layer 2 — hash-guard refuses to bind a real hash to v2
- `schwab.py:31-56` — `configured_schwab_accounts(settings)`; inside `add(...)`:
  - `schwab.py:51-52`:
    ```python
    if account_name == settings.strategy_schwab_1m_v2_account_name:
        return
    ```
  - This is **v2-scoped only** (NOT all `paper:` accounts — broadening that is "Phase 2", deliberately deferred because the retired bots' position-sync still triggers the shared token refresh; see `docs/p1-v2-deliberate-paper-routing-design.md`). The retired bots `paper:macd_30s` / `paper:schwab_1m` stay registered.
- Proven by `tests/unit/test_p1_v2_paper_routing.py`:
  - `test_v2_routes_to_simulated_provider_by_default` (provider layer)
  - `test_configured_schwab_accounts_refuses_v2_but_keeps_retired_bots` (hash-guard layer; even with `schwab_account_hash="REALHASH-2EE5A4"` set, v2 is absent from the map)
  - `test_v2_inert_on_sim_even_if_a_real_hash_entry_would_exist` (both layers together → inert)

### 2.3 Display-honesty (cosmetic only — not a guard)
- `settings.py:635-642` — `display_account_name`: if `provider == "schwab"` and the account starts with `paper:`, it is shown as `live:<bot>`. This is **display only** and does not affect routing.

### Does renaming to `live:` alone flip it? **No.**
Renaming `paper:schwab_1m_v2` → `live:schwab_1m_v2` only changes `strategy_schwab_1m_v2_account_name`. It does **not**:
- change the provider (still `"simulated"` from `settings.py:168`) → orders still go to sim; **and**
- the hash-guard at `schwab.py:51` keys off `settings.strategy_schwab_1m_v2_account_name`, so the guard **moves with the rename** — it would now refuse `live:schwab_1m_v2`. The guard only refuses to *register a hash*; it does not gate routing.

So a rename alone leaves v2 fully simulated and unable to bind a hash. **All three of {provider, hash-guard, hash wiring} must change together** (see §3).

---

## 3. What changes to go live — config keys, account strings, files

Three coordinated changes are required (any one alone is insufficient / inert):

| # | Change | Key / location | Paper value (now) | Go-live value |
|---|--------|----------------|-------------------|---------------|
| A | **Account rename** `paper:`→`live:` | `strategy_schwab_1m_v2_account_name` (`settings.py:163`) | `paper:schwab_1m_v2` | `live:schwab_1m_v2` (matches the existing `live:polygon_30s` convention) |
| B | **Provider** → real Schwab | `strategy_schwab_1m_v2_broker_provider` (`settings.py:168`, read at `settings.py:611-614`) | `simulated` | `schwab` |
| C | **Relax the v2 hash-guard** | `schwab.py:51-52` | refuses `strategy_schwab_1m_v2_account_name` | guard must no longer match the (now `live:`) account so it can register a hash — **CODE change** |
| D | **Wire the credential/hash** | `schwab_account_hash` (shared) `settings.py:383`, OR a v2-specific hash field | unset/inert for v2 | the real account hash, resolved by `configured_schwab_accounts.add(...)` at `schwab.py:53` |

Detail on each:

- **A — Account rename.** `strategy_schwab_1m_v2_account_name` flows everywhere via `provider_for_account` (`settings.py:629`) and `configured_broker_account_registrations` (`runtime_registry.py:241-249`). Changing it renames v2's broker account end-to-end.

- **B — Provider.** Set `strategy_schwab_1m_v2_broker_provider="schwab"`. Then `provider_for_account(live:schwab_1m_v2)` returns `"schwab"` → `RoutingBrokerAdapter` sends v2 orders to `SchwabBrokerAdapter`. Set it as the explicit running env value (self-documenting), not relying on a default.

- **C — Hash-guard.** The guard at `schwab.py:51-52` keys on `settings.strategy_schwab_1m_v2_account_name`. Because it follows the *account name*, after the rename it would refuse `live:schwab_1m_v2`. To let a hash bind, the guard must be **changed in code** (e.g. only refuse while the name is still `paper:`-prefixed, or remove the v2-scoped clause as part of the deliberate go-live). **This is the single line that turns structural-paper into can-be-live and must be edited as a reviewed code change, not an env flip.** (`schwab.py:37-52` comment explicitly frames this as the deliberate go-live opt-in.)

- **D — Credential / hash wiring.** `configured_schwab_accounts.add(account_name, account_hash)` resolves the hash from the per-account argument or falls back to the shared `schwab_account_hash` (`schwab.py:53`). Today the v2 `add(...)` returns early (guard C) before reaching this line. With the guard relaxed, v2's account would resolve to `schwab_account_hash` (`settings.py:383`) — the **same shared real hash** the momentum bots use. Decision for the operator: reuse the shared hash, or add a dedicated `schwab_<v2>_account_hash` field (pattern exists: `schwab_macd_30s_account_hash` `settings.py:384`, `schwab_schwab_1m_account_hash` `settings.py:385`) and wire it in `add(...)`. The OAuth token itself (access/refresh) is shared and owned by the P0 dedicated refresher — no new token wiring needed; only the **account hash** is the new credential surface.

**Net:** A+B+D are config/env; **C is a code edit.** Going live is therefore NOT config-only — it requires a reviewed code change to the hash-guard, by design.

---

## 4. ATR-only config for go-live (setting → value)

Goal state: **ATR-flip ON + fresh-flip qualifier ON (ceiling 5) + MACD/VWAP (Path 1/Path 2) OFF + OMS exits ON.**

| Setting | File:line | Go-live value | Notes |
|---------|-----------|---------------|-------|
| `strategy_schwab_1m_v2_enabled` | `settings.py:159` | `true` | the bot itself / kill-switch |
| `strategy_schwab_1m_v2_atr_flip_enabled` | `settings.py:236` | `true` | ATR-Flip path ON (read at `schwab_1m_v2.py:330`) |
| `strategy_schwab_1m_v2_atr_flip_variant` | `settings.py:237` | `B` (default) | Variant B = intrabar touch of resting trail (validated); A = confirmed-flip-at-close |
| `strategy_schwab_1m_v2_atr_flip_use_max_state_age` | `settings.py:249` | `true` | fresh-flip qualifier ON (read at `schwab_1m_v2.py:348`; gate at `schwab_1m_v2.py:610-613`) |
| `strategy_schwab_1m_v2_atr_flip_max_state_age` | `settings.py:250` | `5` (ceiling; default already 5) | screen flips with `state_age >= 5` |
| `strategy_schwab_1m_v2_atr_flip_quantity` | `settings.py:238` | `10` | live-paper size (read at `schwab_1m_v2.py`; ATR emit uses `self._atr_qty`) |
| `strategy_schwab_1m_v2_atr_flip_vol_floor` | `settings.py:239` | `5000` (default) | the ONLY ATR filter (`schwab_1m_v2.py:602`) |
| `strategy_schwab_1m_v2_atr_flip_period` | `settings.py:240` | `5` (default) | ATRPeriod parity |
| `strategy_schwab_1m_v2_atr_flip_factor` | `settings.py:241` | `3.5` (default) | ATRFactor parity |
| `oms_v2_exit_management_enabled` | `settings.py:438` | `true` | **OMS exits ON** (read at `oms/service.py:931, 984, 1041`) |
| `strategy_schwab_1m_v2_gateway_register_enabled` | `settings.py:198` | `true` (recommended) | v2 watchlist → OMS quote/trade cache; exits need quote coverage. (`_sync_gateway_subscription` registers when this OR the exit flag is on, but enabling explicitly is the documented pre-check.) |

### Can Path 1 (MACD Cross) / Path 2 (VWAP Breakout) be turned OFF via config? **NO — needs code.**

This is a **hard finding**, verified:
- The MACD/VWAP path enablement lives entirely in the frozen dataclass `SchwabV2Config` (`schwab_1m_v2.py:64-122`). The gates (`require_vwap_filter`, `require_macd_strength`, `require_uptrend`, `require_green_bar`, `require_rel_volume`, etc.) are **dataclass defaults**, NOT read from `Settings`/env.
- `SchwabV2Strategy(self.settings)` is constructed with **no config arg** (`services/schwab_1m_v2_bot.py:192`), so `self.cfg = config or SchwabV2Config()` (`schwab_1m_v2.py:311`) uses the hardcoded defaults. **There is no env/Settings override path for the SchwabV2Config fields.**
- There is **no `path_1_enabled` / `path_2_enabled` / `macd_enabled` flag** anywhere.
- In `_evaluate_completed_bar`, Paths 1/2 fire whenever their conditions hold (`schwab_1m_v2.py:796-809`), and **ATR is only reached when neither Path 1 nor Path 2 fired** (`schwab_1m_v2.py:976-981`, explicit precedence MACD > VWAP > ATR). So with paths still active, a MACD/VWAP signal would **fire a real order ahead of ATR** at go-live — exactly what "ATR-only" must prevent.

**Therefore "ATR-only" requires a CODE change** to disable Paths 1/2 (e.g. short-circuit `path_macd`/`path_vwap` to `False`, or add a `SchwabV2Config`/Settings toggle). Do this design-first, with a real-emit test, per the v2 entry-criteria discipline. **Do not assume a flag exists.**

---

## 5. Staged go-live plan (qty 10)

### Kill-switch — fastest way to halt v2
- **Primary:** set `strategy_schwab_1m_v2_enabled=false` (`settings.py:159`) and restart `project-mai-tai-schwab-1m-v2.service`. The bot stops emitting all intents. (Restart confirmed as the only service touched for v2 deploys — handoff `:530`.)
- **Order-side belt-and-suspenders:** set `strategy_schwab_1m_v2_broker_provider="simulated"` (revert §3-B) → any intent that still emits routes to sim, not Schwab. Requires OMS restart to rebuild the routing adapter (`_build_broker_adapter`, `oms/service.py:1891`).
- **Note:** halting v2 stops *new entries*. **Open live positions are NOT auto-flattened** by disabling the bot. The OMS exit ladder (if `oms_v2_exit_management_enabled` is on) continues to manage exits only while the OMS runs; otherwise positions must be closed manually at the broker.

### Reversible vs irreversible

| Action | Reversible? | How / cost to undo |
|--------|-------------|--------------------|
| `strategy_schwab_1m_v2_enabled` true→false | ✅ Reversible | env flip + v2 service restart |
| `strategy_schwab_1m_v2_broker_provider` schwab→simulated | ✅ Reversible | env flip + OMS restart (rebuilds routing) |
| Account rename `live:`→`paper:` | ✅ Reversible | env flip + restart |
| ATR/exit flags | ✅ Reversible | env flips + restart |
| Hash-guard code relaxation | ✅ Reversible | revert the code change + redeploy |
| Submitting an order that the broker accepts | ⚠️ Partly | can attempt cancel only while WORKING/unfilled |
| **A real FILL at Schwab** | 🔴 **IRREVERSIBLE** | a real position at real cost; only undoable by a second real (closing) trade with its own slippage/P&L. This is the only truly one-way step. |

**The reversible/irreversible boundary is the broker fill.** Everything up to "order routed to SchwabBrokerAdapter" is config/restart-reversible; the moment Schwab fills, real money has moved.

### Staged sequence (smallest blast radius)
1. Land the **04:00 race fix** (§7) and the **ATR-only path-disable code** (§4) — both PR + review + after-close deploy. Re-prove v2 still paper.
2. Keep qty at **10** (`strategy_schwab_1m_v2_atr_flip_quantity`). Penny-stock notionals are tiny but real.
3. **Attended, after-close, account-flat** ceremony (per high-stakes deploy discipline): preflight requires **zero open positions** (`deploy_preflight.py:97-103` fails on any open virtual or broker positions).
4. Apply §3 changes (A rename, B provider=schwab, C guard relaxation deployed, D hash wired), restart `project-mai-tai-schwab-1m-v2.service` + OMS.
5. Verdict at RTH (or the bug-manifestation window), eyes on. Kill-switch ready (above).

---

## 6. Safety story with isolation removed

### Protections that REMAIN once v2 can route real orders
- **Exit ladder (OMS):** with `oms_v2_exit_management_enabled=true`, v2 positions get the OMS exit ladder — scale-outs, peak-ratchet floor, −1.5% hard stop, MACD/stoch tier exits (ref `docs/oms-exit-logic-reference.md`). **This is the primary real-money protection** and MUST be ON before go-live. (Quote-staleness guard: `oms_v2_exit_quote_max_age_ms`, `settings.py:443`; hard stop runs on any fresh quote, not RTH-gated.)
- **Quote coverage:** `strategy_schwab_1m_v2_gateway_register_enabled` ensures v2's symbols are in the OMS quote/trade cache so exits can act (`settings.py:198`).
- **Risk gate (OMS `_evaluate_risk`, `oms/service.py:1935-1948`):** rejects protected symbols (`protected_symbol_set`), non-positive quantity, bad intent-type/side. **That is the entire risk gate.**
- **Per-segment / cooldown entry guards:** ATR one-entry-per-short-segment + flat-and-no-cooldown gates (`schwab_1m_v2.py:971-981`) limit re-entry churn.
- **Account-flat-at-restart preflight** (`deploy_preflight.py:97-103`) for the managed deploy path.
- **Fixed small size:** qty 10.

### NEW risks that appear
- 🔴 **No position/notional/exposure cap.** `_evaluate_risk` has **no max-position, max-notional, max-open-positions, or buying-power check** (verified `oms/service.py:1935-1948`; no such keys in `settings.py`). Blast radius is bounded only by qty 10 × number of symbols v2 can be flat-and-eligible on simultaneously. There is no cap on *concurrent* live positions.
- 🔴 **Sim P&L was idealized.** Paper-sim fills are instant/full at `reference_price`, no slippage/partials/rejects (`simulated.py:41-88`; sells `min(held, qty)`, never short, `:136`). Real Schwab fills on **illiquid penny stocks** will diverge materially (slippage, partials, rejects). The paper track record does NOT predict live execution.
- 🔴 **04:00 watchlist-staleness race** (precondition, §7) — with v2 live this could enter *yesterday's* symbols at the session boundary.
- ⚠️ **Shared OAuth token / shared real hash.** v2 would trade the same real account as the momentum bots (unless a dedicated hash is wired, §3-D). Reconciliation and position attribution across bots on one account hash needs operator confirmation.
- ⚠️ **Open positions survive a bot-disable.** Killing the bot stops entries but not open positions; relies on OMS exits or manual close.

---

## 7. Preconditions (must hold before the credential transition)

1. 🔴 **04:00 ET watchlist-staleness race fixed (GO-LIVE BLOCKER #4).** Diagnosed 2026-06-16 (handoff `docs/session-handoff-global.md:38-73`). At the 04:00 ET boundary, bot watchlists carry yesterday's symbols because lifecycle retention re-promotes them (~04:00:00.965) just before the scanner reset (`set_watchlist([])`, ~04:00:00.969). **While paper this is harmless; with v2 credentialed it could enter yesterday's symbols.** Do NOT re-investigate here — this scoping doc treats the fix as a hard precondition. (Candidate fix per handoff: order reset before trade-signal generation / repopulate at 03:55 if 04:00 data isn't required.)
2. 🔴 **ATR-only path-disable code landed** (§4) — Paths 1/2 cannot be turned off by config; a MACD/VWAP signal would otherwise fire a real order ahead of ATR.
3. 🔴 **Hash-guard relaxation reviewed** (§3-C) — the deliberate code edit that defeats Layer 2; design-first + reviewed.
4. ✅ **Token-refresh SPOF resolved** (P0, #274) — access-token freshness owned by the control-service refresher; no new token wiring at go-live (only the account hash). A dead *refresh* token still needs human re-auth (surfaced loudly) — operator awareness item, not a code gap.
5. **OMS exits verified live** (`oms_v2_exit_management_enabled` + gateway registration proven covering v2's watchlist) BEFORE the first real entry — a live position with no exit is the worst case.
6. **Attended, after-close, account-flat** ceremony per high-stakes deploy discipline.

---

## 8. Open questions for the operator

1. **Shared hash vs dedicated hash?** Reuse `schwab_account_hash` (shared with momentum bots, §3-D) or add a dedicated `schwab_<v2>_account_hash` for isolated attribution/reconciliation? (Pattern exists in `settings.py:384-385`.)
2. **Path-disable mechanism:** add a real `SchwabV2Config`/Settings toggle (`atr_only` / `path_macd_enabled` / `path_vwap_enabled`) so ATR-only is config-driven and reversible without a redeploy, vs a hard code short-circuit? Recommend a toggle for reversibility.
3. **Concurrent-position / notional cap:** add an OMS exposure limit for live v2 (none exists, §6)? At qty 10 across a 25-symbol watchlist, max concurrent exposure is bounded but uncapped.
4. **Kill-switch SLA:** is `enabled=false` + v2-service restart (entries stop, open positions managed by OMS exits) acceptable, or is a hard "flatten-all" needed in the live runbook?
5. **Hash-guard scope after go-live:** keep the guard refusing only while `paper:`-prefixed (so the rename is the gate), or remove the v2 clause entirely? The former keeps "can't be live while named paper:" enforcement (matches the design's Phase-2 intent).
6. **Variant A vs B** at go-live (default B = validated touch entry) — confirm.

---

## Appendix — key files
- `src/project_mai_tai/settings.py` — all flags; provider/display logic `:597-642`
- `src/project_mai_tai/broker_adapters/schwab.py` — `configured_schwab_accounts` + v2 hash-guard `:31-56`
- `src/project_mai_tai/broker_adapters/routing.py` — provider-level routing `:13-58`
- `src/project_mai_tai/broker_adapters/simulated.py` — idealized fill model `:41-136`
- `src/project_mai_tai/runtime_registry.py` — account registrations `:239-250`
- `src/project_mai_tai/oms/service.py` — risk gate `:1935-1948`, adapter build `:1891-1908`, exit flag reads `:931/984/1041`
- `src/project_mai_tai/strategy_core/schwab_1m_v2.py` — `SchwabV2Config` `:64-122`, ATR emit `:586-656`, path precedence `:796-981`
- `src/project_mai_tai/services/schwab_1m_v2_bot.py` — strategy construction `:192`
- `src/project_mai_tai/deploy_preflight.py` — flat-at-deploy preflight `:97-103`
- `tests/unit/test_p1_v2_paper_routing.py` — both isolation layers, proven
- `docs/p1-v2-deliberate-paper-routing-design.md` — the P1 design + go-live ceremony framing
- `docs/session-handoff-global.md:38-73` — 04:00 watchlist-staleness race (BLOCKER #4)
