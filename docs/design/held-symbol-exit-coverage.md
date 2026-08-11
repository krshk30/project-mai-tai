# Held-symbol exit coverage — design

**Status:** design → implementation, 2026-08-11. Deploy after the close.
**Problem class:** a position we hold can lose the market data its exit rules depend on.

---

## 1. The hazard, stated precisely

The v2 bot publishes its market-data subscription from the **watchlist only**, in `replace` mode:

```python
# services/schwab_1m_v2_bot.py::_sync_gateway_subscription
desired = sorted(self._watchlist)
... MarketDataSubscriptionPayload(consumer_name=SERVICE_NAME, mode="replace", symbols=desired)
```
The same list drives the REST client and the streamer:
```python
self.rest_client.set_desired_symbols(selected)
self.streamer.set_desired_symbols(selected)      # CHART_EQUITY + LEVELONE_EQUITIES
```

The OMS exit ladder is **not** watchlist-gated — `_watchlist` appears **0 times** in `oms/service.py`,
and `_evaluate_v2_managed_exit` is gated on `self._managed_v2_symbols`. But it is **quote-driven**:

```python
# oms/service.py::_handle_quote_tick_event  (the ONLY caller, line 4359)
for acct in self._v2_accounts():
    if (acct, symbol) in self._managed_v2_symbols:
        await self._evaluate_v2_managed_exit(acct, symbol)
```

⇒ **The rules are not gated on the watchlist; their INPUT is.** When the scanner drops a symbol we
still hold, subscriptions are replaced without it, quote ticks stop, `_handle_quote_tick_event` never
fires for it, and CW_TARGET / CW_FLOOR / CW_HARD_STOP / CW_FLIP all go dark at once. The position is
left with no live protection until the symbol re-joins or the process restarts.

⛔ **This is a hazard, not the FRTT post-mortem.** FRTT was on every watchlist update 09:38→10:02 ET
on 2026-08-11, quotes kept flowing, and the floor armed on a live `bid=1.5300`. FRTT was blocked by
`if snapshot.dedup_active: return` — a separate defect, fixed separately. Do not conflate them.

---

## 2. ⛔⭐⭐ EXIT-ONLY. This must NEVER enable an entry.

**The asymmetry is the design, not an oversight.**

On a held position we are *already exposed*; exiting is not a choice. **Entering** a symbol the
scanner has dropped is a fresh decision nobody asked for, and it directly contradicts the scanner's
purpose — the drop *is* the signal that the name no longer qualifies.

⇒ **Held-symbol coverage exists to CLOSE positions, never to OPEN them.**

⛔ **Do not "complete" this later by letting held symbols arm, re-enter, or take a fan-out leg.**
If a future change makes coverage symmetric, it is a defect, not a feature. The entry blocks are
enforced by `_within_entry_window` and the watchlist check on the arm path, and — per this design —
by an explicit test that fails if either is removed (§5, test 5).

---

## 3. Ownership source — union, deliberately

The coverage set is:

```
coverage = {open OmsManagedPosition rows for STRATEGY_CODE, both accounts}
           ∪ {virtual_positions.quantity > 0}
```

**Why the union and not one source.** Each layer has a known, recorded failure mode:

| source | known defect |
|---|---|
| `virtual_positions` | reads **ZERO for a position we hold** (DSY 2026-08-07) |
| `oms_managed_positions` | **phantom rows** — poll driven from open rows (#644) |

Using either alone imports that layer's defect into the exit path. The failure directions are
opposite, so the union is strictly safer than either.

⭐ **Fail-safe direction is OVER-subscribe.** A spurious subscription costs a few quotes per second.
A missing one blinds every exit rule on a live position. When the two sources disagree, subscribe.

---

## 4. The sweep: conservative, and the limit is deliberate

**We do NOT add a sweep that exits from a stale quote.** A floor or target computed from a quote
that stopped updating at de-listing is an exit priced off a market that no longer exists — a wrong
reason is worse than a missing one.

The conservative behaviour is **already implemented** and stays exactly as it is:

```python
# oms/service.py::_evaluate_v2_managed_exit
if age_ms > float(getattr(self.settings, "oms_v2_exit_quote_max_age_ms", 5000)):
    return  # stale quote — never act on a gap
```

⇒ **The subscription fix is the actual repair. The staleness guard is the backstop covering the
window before subscription catches up.** Its limit — it declines to act rather than acting on stale
data — is a deliberate choice, not an apology. A held position with no fresh quote is left alone and
surfaces through the existing unowned-position and orphan-order watches.

⛔ No new `_cw_managed_exit_sweep` is introduced. (No such function exists today; it was proposed
from a mechanism that the call sites disproved.)

---

## 5. Acceptance — five properties, each mutation-proved independently

Scenario for 1–4: symbol is **held** and has been **removed from the watchlist**.

| # | property | mutation that must turn it RED |
|---|---|---|
| 1 | profit target fires (`CW_TARGET`) | revert `_sync_gateway_subscription` to watchlist-only |
| 2 | trailing floor fires (`CW_FLOOR`) | same |
| 3 | trigger/flip exit fires (`CW_FLIP`) | same |
| 4 | 16:00 resting-cancel reaches the de-listed symbol's order | same |
| 5 | ⛔ held + de-listed symbol receiving quotes **never arms and never places an entry** | remove the entry-window / watchlist arm guard |

⛔ Fixing the one we noticed and leaving the rest is how CW_FLIP sat broken. Each test must fail on
its own mutation, with the others still passing.

### Mutation results (2026-08-11) — baseline 7 passed, each mutation reverted after

| mutation | result |
|---|---|
| M1 `_sync_gateway_subscription` → watchlist only | RED: `test_quote_feed_survives_delisting_for_a_held_symbol` (6 others green) |
| M2 `_push_desired_symbols` → watchlist only | RED: `test_bar_feed_survives_delisting_so_the_flip_exit_can_arm` |
| M3 remove the entry off-watchlist guard | RED: `test_held_delisted_symbol_can_never_enter` |
| M4 gate the window-closed cancel on a watchlist | RED: `test_resting_cancel_reaches_a_delisted_symbol` |

⚠️ **Honest limit on "independently".** CW_TARGET, CW_FLOOR and CW_HARD_STOP share **one** input —
the quote feed — so they share **one** mutation (M1). They are not independently mutable at the
subscription layer, and claiming three separate mutations would be manufacturing independence they
do not have. What distinguishes them is the rule-level test
`test_each_exit_rule_is_reachable_for_a_delisted_held_symbol`, which drives `cw_exit_decision` at
three different bids and asserts `arm` / `floor` / `stop` separately. CW_FLIP *is* independently
mutable (M2) because it is armed off the **bar** feed, not the quote feed.

**Fixtures must match production config** — `oms_v2_exit_management_enabled` defaults False, and a
fixture that leaves it False passes by never running the code (2026-08 precedent).

---

## 6. Side effects of keeping held symbols subscribed

| surface | effect |
|---|---|
| quote volume | +1 symbol per held position. Bounded: qty-2 positions, rarely >3 concurrent |
| streamer | held symbols stay on `CHART_EQUITY+LEVELONE_EQUITIES`; **desirable** — the flip exit is armed off bar closes, so bars must keep arriving |
| REST warmup gate | ✅ **no change needed — verified, not assumed.** The `_skip` predicate returns True for a held symbol two lines earlier (`positions.get(symbol,0) > 0 or held.get(symbol,0) > 0`), so the watchlist test below it is never reached for a held name |
| symbol cap | ✅ **checked on the gateway side, as required before shipping. No cap exists.** `gateway.py` merges consumers with `self._active_symbols = set().union(*self._desired_symbols_by_consumer.values())` — no truncation, no limit. ⚠️ Note `strategy_schwab_1m_v2_max_watchlist_size = 25` caps the **watchlist** (`symbols[:max_watchlist]`), and coverage is unioned **after** it — so the subscription can exceed 25 by the number of held positions (rarely >3, so ~28 worst case). That is intended: the cap bounds what we *watch for entries*, not what we *keep alive to exit*. `market_data_archive_retention_max_symbols = 50` is archive retention, not subscription, and is not approached |
| `_watch_start_ms` | unchanged — coverage must NOT stamp a watch-start, or a held symbol would look "watched" to `_cap_reconstructed_segment` |

---

## 7. Out of scope

- The `dedup_active` lockout (the actual FRTT defect) — separate change.
- Storing the broker fill price — separate, and the reason our own record of FRTT says 1.53 when the
  operator's chart shows the fill at **1.535**.
- Whether the tiered `_v2_exit_engine` should be reachable at all: today
  `if self._cw_exit_enabled: … return` dominates it, so `tier`, `floor_pct=8.5`, `floor_price` are
  written to `oms_managed_positions` but cannot act. Design conversation, not this change.
