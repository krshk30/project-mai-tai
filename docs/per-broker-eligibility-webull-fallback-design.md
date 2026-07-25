# Design: v2 dual-broker FAN-OUT + per-broker eligibility

**Status:** ✅ BUILT 2026-07-25 (Sat), flag-gated OFF (branch `feat/dual-broker-fanout`). Operator
requirement locked; operator decision on the RTH-resting Webull trigger = **Option 1** (software-detect
at `resting_level`). Validate Mon (flag-off/attended), go live Tue. Monday runs the CURRENT sequential
mirror-on-fill unchanged.

**Build notes (as-implemented, refines §4/§7):** the Webull leg is emitted as a NORMAL v2 buy-open
intent to the Webull account via a SECOND bot emitter — the OMS's existing account-agnostic
`_apply_v2_oco_bracket_entry` builds the RTH OCO off `entry_price` (the cross px, same as the Schwab
primary) and the EH builders re-price a marketable EH-LIMIT off the OMS's own ask, so NO OMS combo
builder is called from the intent path (the mirror's builder stays for the flag-off on-fill rollback).
Three cross moments: reactive + EH-resting co-queue the Webull leg inline in their existing builders;
RTH-resting adds `_fanout_rth_resting_cross` (software cross at `resting_level`, once-per-flip via
`state.fanout_webull_claimed`). Per-broker eligibility: `webull_ineligible_today` table + classifier
that vetoes 429/transient; eviction intersects the two sets; all Webull-ineligible read/write gated on
the fan-out flag (byte-identical + ORB untouched when off). 27 new tests + full suite green.
**Supersedes** the fallback approach (rejected: adds a sequential wait) and revives the parallel model from
`dual-broker-v2-design.md` (#424), now with per-broker eligibility.

---

## 1. Requirement (operator, 2026-07-24)
1. **Trade the same stock on BOTH brokers** (2× is wanted).
2. **Both orders fire at the SAME INSTANT, in parallel** — NO "submit Schwab, wait, then Webull." Seconds
   matter; no sequential round-trip.
3. **A broker's reject blocks ONLY that broker** — Schwab reject → skip Schwab's leg, keep Webull's; Webull
   reject → skip Webull's leg, keep Schwab's.
4. **Both brokers reject → drop the name from v2.**

## 2. What changes vs. what's live now
| | LIVE now (mirror-on-FILL) | THIS design (FAN-OUT) |
|---|---|---|
| Trigger | Schwab submitted first; Webull fires **after the Schwab fill** (sequential) | both legs fire **at the cross, in parallel** |
| Schwab reject | no fill → **Webull never trades it** | Webull leg fires anyway (independent) |
| Eligibility | Schwab-ineligible → **whole name evicted** from bot | per-broker: block one broker, keep the other |
| Latency | Webull leg lags by the Schwab submit→fill time | no lag — simultaneous submit |

Both are **flag-selected** — Monday = mirror-on-fill (flag off), Tuesday = fan-out (flag on).

## 3. The one unavoidable asymmetry — Webull refuses a buy-STOP
Webull structurally rejects a buy-STOP master ("Fork A", `webull-mirror-on-fill-design.md:40-44`). So the two
legs are NOT always the identical order:
- **Reactive entry** (MARKET-at-cross): both legs = MARKET. Truly symmetric fan-out.
- **Resting entry** (Schwab buy-stop-limit): the Schwab leg rests the buy-stop-limit; **the Webull leg fires a
  MARKET at the cross instead.** Still parallel, still no wait — but the Webull leg takes spike slippage the
  Schwab resting fill avoids ([[project_mai_tai_flip_entry_stoplimit]]). This is Webull's limitation, accepted.

**Unifying rule: the Webull leg is ALWAYS a MARKET-at-cross, driven by the bot's cross-detection.** The Schwab
leg keeps its native mode (reactive MARKET or resting buy-stop-limit OTOCO).

## 4. Architecture — BOT-LEVEL fan-out (two eligibility-gated legs per signal)
The bot owns cross-detection and the entry mode, so it fires both legs at the right moment with the right order
types. The OMS routes by account (exists) and manages each leg's exit per account (exists).

**At a v2 entry signal (the ATR up-cross), the bot emits up to TWO intents, in parallel:**
- **Schwab leg** → `strategy_schwab_1m_v2_account_name`, native mode (reactive MARKET, or the resting
  buy-stop-limit that was pre-placed). Emitted **only if the name is not Schwab-ineligible**.
- **Webull leg** → the Webull account (`live:orb`), a **MARKET (RTH) / marketable-EH-limit (EH) master +
  native OCO**, anchored off the OMS's own fresh ask + default qty. Emitted **only if the name is not
  Webull-ineligible**.

Both intents carry their own exit geometry so neither leg is ever naked (RTH native OCO; EH software ladder).
The legs are independent: one broker rejecting does not touch the other (they were never chained).

**Why bot-level, not OMS-level:** for the resting entry the Schwab order is placed *in advance*; fanning it out
in the OMS at placement would fire the Webull MARKET *before* the cross (wrong). The bot is where the cross
moment is known, so it is where the Webull MARKET must be triggered. The OMS stays a router + per-account exit
manager.

## 5. Per-broker eligibility (the operator's block rule)
- Keep **`schwab_ineligible_today`** (Schwab reject "must be placed with a broker" → recorded, `oms/service.py:4911-4919`).
- Add a symmetric **`webull_ineligible_today`** — written when a Webull open is rejected for a genuine
  not-tradable reason (NOT a 429/rate-limit — those must not mark a name ineligible, [[project_mai_tai_webull_mirror_429_flood]]).
- **Per-leg gate at emit:** the bot skips a broker's leg iff that broker has the name in its ineligible table
  today. So Schwab-ineligible → Webull-only leg; Webull-ineligible → Schwab-only leg; neither → both legs (2×).
- **Evict from the watchlist ONLY when BOTH tables hold the name** (`bot.py:945-947` changes from
  `-= schwab_ineligible` to `-= (schwab_ineligible ∩ webull_ineligible)`). A name keeps trading as long as ≥1
  broker accepts it.
- Discovery is still learn-by-failing (a name is only known ineligible after a real reject) — but under FAN-OUT
  that first reject is **free**: the other broker's leg fired in parallel and traded, so no discovery trade is
  lost (unlike the sequential model). This is the key payoff of fan-out over fallback.

## 6. Exits — unchanged, per-account (already multi-leg capable)
Each leg writes its own managed row keyed on `broker_account_name` (unique-open constraint is per-account, so
the same symbol on two accounts = two rows, no collision — `dual-broker-v2-design.md` §1). Each leg runs its own
CW exit independently: RTH = broker-native OCO with software-ladder stand-down; EH = software CW EH-limit ladder.
`_v2_accounts()` + the per-`(account,symbol)` eval already dispatch this. A fan-out Webull leg inherits it by
creating its managed row at fill (the mirror already does exactly this).

## 7. Reuse map (this is wiring, not new machinery)
| Need | Reuse |
|---|---|
| Build Webull MARKET + native OCO (RTH) | the combo builder inside `_mirror_v2_fill_to_webull` (`oms/service.py:1101-1110`) |
| Build Webull EH-limit master (EH) | `_build_v2_mirror_eh_master` (`:1247-1313`), already prices off the OMS's own fresh ask |
| Submit + record + managed row | the mirror's own submit path (`:1139-1231`) + `_apply_managed_position_after_fill` |
| Route intent → Webull | `broker_adapters/routing.py:66-68` (by account) |
| Per-account exit ladder | `_v2_accounts` / `_evaluate_v2_managed_exit` (`:1840-1848`, `:3311-3313`) |
| Collision guard on shared `live:orb` | the mirror's existing guard (`dual-broker-v2-design.md` §5) |
| Schwab-ineligible record + match | `oms/service.py:57`, `:4911-4919`, `:5683-5685` |
**The change:** move the Webull-entry TRIGGER from "on Schwab fill" (`:2916/:2987/:3146`) to "on the bot's cross,
in parallel," anchored off live ask + default qty instead of the Schwab fill; add the Webull-ineligible table +
the both-reject eviction; gate each leg on its broker's eligibility.

## 8. Flag & rollout
- New flag e.g. `strategy_schwab_1m_v2_dual_broker_fanout_enabled` (default **OFF**). OFF = today's mirror-on-fill
  (byte-identical). ON = fan-out. Mutually exclusive with the on-fill mirror at runtime.
- **Monday:** flag OFF — sequential mirror-on-fill runs live, unchanged.
- **Monday (validation, attended):** exercise the fan-out path flag-on in a controlled/qty-1 way (both legs fire
  in parallel, per-broker reject blocks one leg, both-reject evicts) — reuse the `v2_webull_qty1_harness` +
  the LVWR-style controlled order flow proven 07-24.
- **Tuesday:** flip the flag ON → fan-out live.

## 9. Decisions (operator 2026-07-24 — RESOLVED)
1. **Resting Webull leg = MARKET-at-cross slippage** — ACCEPTED (Webull's limit; still parallel, §3).
2. **Webull-ineligible criteria — DEFER.** Operator: "I have never seen any rejection from Webull so far — find
   out later or never." So the both-reject eviction is **mostly theoretical**: in practice Schwab rejects a
   foreign name and Webull accepts it → the name simply becomes a **Webull-only leg and keeps trading** (the
   whole point). The `webull_ineligible_today` table + classification is a **safety net, not a hot path** — build
   the table + the both-reject eviction hook, but treat any Webull reject conservatively (do NOT mark ineligible
   on 429/transient/BP; only a clear not-tradable string, captured if one ever occurs). Do not block the build on
   Webull reject strings we may never see.
3. **2× capital on every eligible name** — CONFIRMED wanted. Per-order default qty on each broker, no netting.
4. **Flag is mandatory (operator: "always design with flag in case this is horrible → go back to seq").** The
   fan-out flag toggles fan-out ↔ the current sequential mirror-on-fill; **keep the mirror-on-fill code behind
   the flag-off path as the rollback** (do NOT delete it — it IS the fallback). Flag-off must stay byte-identical
   to today so a bad Tuesday is a one-line revert (drop the flag + restart).

## 10. Risks & guards
- **Naked leg:** every Webull leg carries its OCO (RTH) or a created managed row (EH) at/before fill — reuse the
  mirror's atomic create; gate with a #404-style rehydrate/survival test (both legs re-arm on restart).
- **Double-fire on the same cross:** the fan-out must emit each broker's leg exactly once per flip — reuse the
  existing per-flip entry cap / dedup ([[feedback_mutate_the_code_pin_the_threshold]] max_entries_per_flip).
- **429 marking a name ineligible:** the Webull-ineligible write MUST exclude rate-limit/transient rejects.
- **Collision with ORB on `live:orb`:** the mirror collision guard must wrap the fan-out Webull leg too.
- **Flag-off identical:** merge gate = fan-out OFF is byte-identical to today; fan-out ON unit-tested (both legs,
  per-broker gate, both-reject evict) + the survival test.

## 11. Timeline
- **Fri (today):** this design → operator review.
- **Sat–Sun:** build — (a) per-broker eligibility table + both-reject eviction, (b) bot-level fan-out emit (two
  eligibility-gated legs), (c) Webull-leg entry reusing the mirror builders triggered at the cross, (d) tests
  (flag-off identical + fan-out unit + survival). Deploy flag-OFF to the VPS Sun.
- **Mon:** sequential runs live (flag off); attended flag-on validation (qty-1 / controlled) in-market.
- **Tue:** flip flag ON → fan-out live.

## Key file:line index
- Bot emit + account binding: `services/schwab_1m_v2_bot.py:329-333` · eviction `:945-947` · loader `:995-1035`
- Mirror-on-fill (the trigger to replace): `oms/service.py:1008-1013, :2916, :2987-3001, :3146-3149`
- Webull entry builders to reuse: `_mirror_v2_fill_to_webull` `:1024-1231`, combo `:1101-1110`; EH `_build_v2_mirror_eh_master` `:1247-1313`
- Schwab-ineligible record/match: `oms/service.py:57, :4911-4919, :5683-5685`; table `db/models.py:182-203`; store `oms/store.py:430-504`
- Per-account exits: `oms/service.py:1840-1848, :2114-2120, :3311-3313`
- Routing by account: `broker_adapters/routing.py:66-68`
- Prior context: `docs/dual-broker-v2-design.md` (the fan-out precedent, #424), `docs/webull-mirror-on-fill-design.md` (Fork A / no-buy-stop)
