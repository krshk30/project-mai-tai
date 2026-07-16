# Webull native-stop-guard order-type fix — design (REVIEW-READY, after-close attended deploy)

> **Status:** REVIEW-READY spec. Investigation complete + evidence-pinned 2026-06-26; **ground-truthed against
> the installed Webull SDK + live OMS/adapter source 2026-06-29.** Code change NOT written, NOT deployed.
> Deploy is **after-close, attended, flag-gated** per live-money discipline. Until deployed, **ORB has no
> working broker-resident stop — the in-memory trail is the sole safety net AND the sole RTH stop trigger** (see Risk).

## TL;DR

ORB's OMS arms a **native broker-resident stop** on every fill. On Webull that order is **rejected every trade**
(`ILLEGAL_PARAMETER … correct order type`, http 417) because the adapter sends the literal `order_type="STOP"`,
which Webull's OpenAPI does not accept — Webull's stop enum is **`STOP_LOSS`** (market-on-trigger) /
**`STOP_LOSS_LIMIT`** (limit-on-trigger). Fix is **adapter-only** (map the order-type token + price-gates),
**flag-gated**, recommended target **`STOP_LOSS` (market-on-trigger)**.

**⚠️ This is NOT a pure "add a backup net" change.** Enabling it flips ORB's *RTH stop-exit trigger* from the
in-memory trail (panic-buffered LIMIT at bid) to the **broker-resident `STOP_LOSS` market order** — because the
in-memory trigger is coded to **stand down when a broker stop is active** (`oms/service.py:1889`). That is the
correct, intended design (it survives an OMS restart), but it changes exit fill character on thin small-caps from
limit-at-bid to **market-on-trigger** (more slippage, guaranteed fill). Surfaced as an explicit decision below.

---

## 1. Parameter shape — VERIFIED against the installed SDK (not assumed)

Source of truth: `…/site-packages/webull/trade/trade/order_operation.py` + `…/trade/common/order_type.py`
(read on the VPS venv 2026-06-29).

**`OrderType` enum** (`order_type.py`): `MARKET=(1)`, `LIMIT=(2)`, **`STOP_LOSS=(3)`**, **`STOP_LOSS_LIMIT=(4)`**,
`TRAILING_STOP_LOSS=(5)`, `ENHANCED_LIMIT=(6)`, `AT_AUCTION=(7)`, `AT_AUCTION_LIMIT=(8)`, …

**Wire token format = the underscored enum NAME.** `EasyEnum.__str__` returns `self.name` and `from_string`
matches on `item.name` → the API expects `"STOP_LOSS"` / `"STOP_LOSS_LIMIT"` (underscores), **not** the label
`"STOP LOSS"` and **not** the numeric code. This is consistent with `"LIMIT"`/`"MARKET"` already working today.

**Required params per type** (`place_order(... order_type, ..., limit_price=None, stop_price=None, trailing_type=None, trailing_stop_step=None)`):

| order_type | stop_price | limit_price | notes |
|---|---|---|---|
| `LIMIT` | — | **required** | already works |
| `MARKET` | — | — | already works; `extended_hours_trading` must be `false` |
| **`STOP_LOSS`** | **required** | — | market-on-trigger; **no limit_price** |
| **`STOP_LOSS_LIMIT`** | **required** | **required** | limit-on-trigger; needs BOTH |
| `TRAILING_STOP_LOSS` | — | — | needs `trailing_type` + `trailing_stop_step` (future enhancement) |

**Does the adapter HAVE the values to pass?** — YES for `STOP_LOSS`, NO for `STOP_LOSS_LIMIT`:
- The OMS arm builder `_arm_or_rearm_native_stop_guard` (`oms/service.py:914-919`) puts into `metadata`:
  `order_type="STOP"`, `time_in_force="day"`, **`stop_price=<formatted>`**, `native_stop_guard="true"`,
  `stop_loss_pct`. **There is NO `limit_price`.**
- The adapter reads `stop_price` via `_meta_price(request, "stop_price")` (`webull.py:156`) → present. ✅
- **`STOP_LOSS_LIMIT` is therefore NOT reachable without an OMS change** (would submit with `limit_price=None`
  → another 417). This is an independent reason to choose `STOP_LOSS`.

**Tick-rounding (the #374 lesson) is already covered.** `stop_price` is passed through `_round_to_tick`
(`webull.py:158` / `407`): $0.01 grid for px≥$1, $0.0001 below $1. So the stop trigger snaps to Webull's grid —
no off-grid 417. (Mapping the token does not bypass this; keep `set_stop_price(str(self._round_to_tick(stop_price)))`.)

**`extended_hours_trading` must stay `false` for `STOP_LOSS`.** A `STOP_LOSS` is market-on-trigger and the SDK
states market orders can only be `extended_hours=false`. The arm builder sets no `session`/`extended_hours` →
the adapter defaults it `false` (`webull.py:161-163`). Additionally the guard only arms during the regular
session (`_arm_or_rearm…` early-returns if `not _is_regular_market_session()`, `service.py:884`). Consistent;
**add an assert** so a future change can't set `extended_hours=true` on a `STOP_LOSS`.

---

## 2. DECISION — `STOP_LOSS` (market-on-trigger) vs `STOP_LOSS_LIMIT` (limit-on-trigger)

**Recommendation: `STOP_LOSS` (market-on-trigger).** Make this an explicit, operator-visible decision.

| | `STOP_LOSS` (market) | `STOP_LOSS_LIMIT` (limit) |
|---|---|---|
| On trigger | sells at market | sells at a resting limit |
| Fill guarantee | **guaranteed fill** | can **skip on a gap-through** → still holding |
| Slippage on thin small-cap | **real** (wide spread → bad print, à la a CDT-style gap) | none, but only if it fills |
| Adapter change needed | token + stop_price gate (have it) | **+ a `limit_price` the OMS does not emit** |
| Restart-survivor backup | ✅ | ✅ but the skip-risk defeats the point |

**Why market wins for this role:** the native stop exists to protect a **restart-while-holding** and to be the
exchange-speed safety net. On a violent thin-microcap collapse a limit stop that gaps through leaves us **naked
with no protection** — i.e. a stop that doesn't fill isn't a stop. The whole reason this order exists is the
worst case; a market-on-trigger is the only variant that actually covers it. The slippage cost is the premium
we pay for guaranteed exit, and ORB's exits are already modeled with gap-through-optimistic fills, so this only
makes the live behavior *more* conservative than backtest, not less.

**If we ever want `STOP_LOSS_LIMIT`** (e.g. to cap slippage on a known-liquid name): it requires the OMS to emit
a `limit_price` in the stop metadata (e.g. `stop_price × (1 − panic_buffer)`), which is an OMS change + a new
fill-semantics decision. **Out of scope** for this fix; tracked below.

---

## 3. COEXISTENCE with the in-memory trail — CORRECTED MODEL (verified in source)

The earlier "the trail fires the exit, so it must cancel the resting broker stop" framing is **only half right.**
Verified mechanism:

**(a) Stop-vs-stop: prevented by the in-memory side STANDING DOWN, not by cancelling.**
`_trigger_hard_stop` (`oms/service.py:1889-1903`):
```python
if _is_regular_market_session() and await self._has_active_native_stop_guard_order(...):
    stop.last_trigger_attempt_at = utcnow()
    return   # defer to the broker-resident stop; do NOT submit an in-memory close
```
- **Today** the broker stop is always rejected → `_has_active_native_stop_guard_order` → False → the in-memory
  trail always fires the exit (a panic-buffered LIMIT, `service.py:1958-1965`). This is why ORB exits work now.
- **After the fix** the `STOP_LOSS` rests and is "active" → the in-memory trigger **defers** → the **broker stop
  market order is what executes the RTH exit.** The in-memory trail keeps **ratcheting** the level and remains
  the **fallback** (fires only if the broker stop is somehow not active — after-hours, or a brief window after a
  restart before re-arm). No double-sell because exactly one side ever submits.

**(b) Managed/profit exits (scale, floor, EOD, any non-stop close): cancel-then-sell, already wired.**
The intent handler (`service.py:453-462`) calls `_cancel_native_stop_guard_before_sell` before any
`close`/`scale` sell that is **not itself** the native stop. So a profit-taking or floor exit cancels the
resting `STOP_LOSS` first, then sells → no double-sell / no leftover short. (`_arm_or_rearm` likewise
cancels-then-replaces the resting stop on every ratchet — `service.py:889-899`.)

**(c) Ratchet churn (note, not a blocker).** Each upward ratchet does cancel-existing → place-new at the higher
level via `_arm_or_rearm`. With a live broker stop this becomes real cancel/replace traffic at Webull during a
run-up (currently it's place→reject, so it's been invisible). Watch the after-close test for cancel/replace
latency and any rate-limit; if chatty, add a min-delta or min-interval before re-arming (follow-up, not in this fix).

**Net coexistence contract the fix must preserve:**
1. Broker `STOP_LOSS` rests at `stop_price` once a fill arms it (RTH only).
2. Every ratchet cancel/replaces it to the new level.
3. On a stop breach, broker fires; in-memory defers (no double-sell).
4. On a managed/profit exit, OMS cancels the resting stop, then sells.
5. If the broker stop is inactive (after-hours / post-restart pre-re-arm), the in-memory trail fires (fallback).

---

## 4. The fix (adapter-only, flag-gated)

`broker_adapters/webull.py` — translate the broker-neutral token to Webull's enum **in one place** so the two
price-gates and `set_order_type` stay consistent.

- **`_order_type()` (`webull.py:386`)** — after upper-casing, map: `STOP → STOP_LOSS`, `STOP_LIMIT →
  STOP_LOSS_LIMIT`; `LIMIT`/`MARKET` unchanged; unknown tokens fall through verbatim (default-safe).
- **limit-price gate (`webull.py:154`)** — widen set to `{LIMIT, STOP_LOSS_LIMIT, ENHANCED_LIMIT, AT_AUCTION_LIMIT}`.
- **stop-price gate (`webull.py:157`)** — change set to `{STOP_LOSS, STOP_LOSS_LIMIT}`.
- Keep `set_stop_price(str(self._round_to_tick(stop_price)))` — tick-rounding intact.
- **Assert**: if mapped type is `STOP_LOSS`, force `extended_hours_trading=false`.

Because both gates and `set_order_type` already call `self._order_type(request)`, mapping inside that one helper
propagates everywhere. **OMS / ORB / strategy / other adapters (schwab, alpaca, simulated) untouched** — they
accept the neutral `"STOP"`, so per-venue mapping at the adapter boundary is the correct seam.

**Flag.** `MAI_TAI_WEBULL_NATIVE_STOP_ORDER_TYPE_MAP_ENABLED` (default **false**).
- **false** → adapter sends `"STOP"` verbatim (today's behavior: 417-rejected, in-memory trail is sole net+trigger).
- **true** → adapter maps to `STOP_LOSS` (broker stop rests; in-memory trail becomes ratchet+fallback).

The flag matters because flipping it **changes the RTH exit trigger** (§3) — it is not a cosmetic correctness
patch. Default-off ships the code inert (byte-identical wire behavior), enable is attended after-close, rollback
= flag false + OMS restart.

---

## 5. Verification plan (attended, after close) — the test is "does it REST at Webull"

1. **Unit** (mirror existing `webull.py` adapter tests): `_order_type` maps `STOP→STOP_LOSS`,
   `STOP_LIMIT→STOP_LOSS_LIMIT`; `stop_price` set (tick-rounded) for the mapped stop types; `limit_price`
   untouched for plain stops; `extended_hours_trading=false` asserted for `STOP_LOSS`; flag-off path still emits `"STOP"`.
2. **Live ACCEPTANCE — the real validator (far-from-market, like the 06-24 plumbing test).** With the flag ON,
   place a `STOP_LOSS` sell on a small held test lot, **stop far below market so it cannot trigger**:
   - one **>$1** name and one **<$1** name (exercise both tick grids),
   - confirm Webull returns a **working order, not a 417**,
   - confirm `find_open_native_stop_guard_order` **sees it RESTING** (the order is actually at the broker — not
     just that our code submitted it),
   - confirm `_has_active_native_stop_guard_order` now returns **True** (so the in-memory defer will engage),
   - then confirm `_cancel_native_stop_guard_before_sell` **cancels it cleanly** (working → cancelled).
   *Code-submitted ≠ resting. The acceptance + the resting + the cancel are three separate confirmations.*
3. **🚦 RESTART-WHILE-HOLDING — HARD PASS/FAIL DEPLOY GATE (not optional).** This scenario is the **entire reason
   the fix exists**; "the stop rests" is necessary but **not sufficient**. With a live `STOP_LOSS` resting on a held
   test lot, **restart the OMS while holding** and require ALL of:
   - **(a)** the broker stop is **still working at Webull** across the restart (query Webull directly, not our state),
   - **(b)** the OMS **re-discovers** it on boot and does **not** place a duplicate (`_rearm_native_stop_from_registry`,
     `service.py:1010` / the boot reconcile at `service.py:1514-1520`),
   - **(c)** after re-discovery the position is **still protected** (one — and only one — working stop at the right level).
   **If any of (a)/(b)/(c) fails, the deploy does NOT proceed** — flag stays off, investigate first. A passing "stop
   rests" (step 2) with a failing restart-survival is a FAIL, because the unprotected restart-while-holding window is
   precisely what we are fixing.
4. **Next ORB window (live behavior change).** Confirm a real fill arms an **accepted** `STOP_LOSS` (no 417)
   and that on a trail breach the **broker stop** executes the exit while the in-memory trigger **defers**
   (look for the trail's deferral, not an in-memory `[HARD-STOP TRIGGERED]` submit). Compare the realized exit
   slippage vs the prior limit-at-bid character (§2 tradeoff in practice).

---

## 6. Risk / operating posture until deployed

- **ORB's only stop is the in-memory trail inside the OMS process** — it is both the net AND the RTH trigger
  today. Works in steady state (proven: IVF + SDOT 06-26 both exited correctly), but **no broker-resident backup.**
- **"Don't restart OMS while ORB holds" is load-bearing.** If ORB holds and the OMS looks unstable: **flatten ORB
  first, then restart — never restart-while-holding.** A restart drops the in-memory trail and there is no broker
  stop to catch the position. (Currently inert — ORB is flat — but keep as standing policy.)
- **Deploy**: after-close, attended, fleet-flat pre-flight. Adapter change is OMS-process-only → choreography =
  account-flat → restart OMS (no strategy-stop dance needed). Default-off flag; enable attended; rollback = flag
  false + OMS restart.

## 7. Out of scope (track separately)

- **`STOP_LOSS_LIMIT` variant** — needs the OMS to emit a `limit_price` in the stop metadata + a slippage-cap
  decision. Only if a future need to bound stop slippage on liquid names arises.
- **Native `TRAILING_STOP_LOSS`** — Webull supports `trailing_type` + `trailing_stop_step`; a broker-resident
  *trailing* stop could match ORB's 3% trail at exchange speed (stronger than a static `STOP_LOSS`, and would
  remove the ratchet cancel/replace churn). Follow-up enhancement; the immediate fix is the static `STOP_LOSS`.
- **Ratchet cancel/replace throttle** — add a min-delta/min-interval before re-arming if the after-close test
  shows chatty cancel/replace traffic (§3c).
- **`NO_SUCH_TICKER` (SHPH 2026-06-26)** — Webull `buy LIMIT` rejected, symbol/`instrument_id` resolution gap.
  Decide lookup-bug-vs-non-listing; if non-listing, evict like the Schwab-ineligible path. Harmless (no fill).

---

### Evidence appendix (2026-06-26 live, `live:orb`→webull, qty 5)

`broker_orders`, both real ORB trades — identical pattern: `buy LIMIT` filled → `sell STOP` **rejected 417**
(`ILLEGAL_PARAMETER … correct order type`) → `sell LIMIT` filled (in-memory trail exit). Positions closed flat;
the **rejected leg is the broker-resident backup stop**, not the exit. No naked position, but the broker net is missing.

| time (ET) | sym | side | order_type | status |
|---|---|---|---|---|
| 9:31:28 | IVF | buy | LIMIT | filled (entry) |
| 9:31:29 | IVF | sell | STOP | **rejected 417** |
| 9:31:38 | IVF | sell | LIMIT | filled (trail exit) |
| 9:41:01 | SDOT | buy | LIMIT | filled (entry) |
| 9:41:15 | SDOT | sell | STOP | **rejected 417** |
| 9:41:20 | SDOT | sell | LIMIT | filled (trail exit) |
