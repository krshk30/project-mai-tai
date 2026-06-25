# ORB OMS-Quote-Priced Entry — Design (Piece 1 of the OMS-pricing port)

> **Status:** DESIGN-FIRST, no code. Flag-gated, default-OFF, byte-identical when off.
> Attended after-close deploy with ORB flat. **Scope = Piece 1 ONLY** (operator decision
> 2026-06-25). Pieces 2 & 3 (per-venue Webull quote book) are **PARKED** — Webull market-data
> is **not entitled** (probe 2026-06-25: `MarketData.get_snapshot` → `401 "Insufficient
> permission, please subscribe to stock quotes"`), and the per-venue OMS refactor is too much
> blast radius to take on speculatively. **v2 is NOT touched.**

## 1. Problem (evidence-pinned)

ORB on Webull ships the **bot's signal-time price** straight through as the order limit. On a
fast small-cap breakout the price is stale by the time it reaches the broker, so the order
either rests through-the-market or is cancelled by the drift guard — it does not fill.

**Live evidence — AZI, 2026-06-25:**
- ORB placed `BUY 5 AZI @ limit 1.90` (the break level), Webull order `EQIKJ7NMPS2J04MEO5JMCFL0TA`, 09:48:18 ET.
- `[OMS-ABANDON-INTENT] code=QUOTE_DRIFT_CANCEL symbol=AZI ... intent_age_s=3.5 limit=1.9000 reason=quote drift 4.0c past limit (tolerance 1.0c); ask/bid moved away`.
- Webull order detail: `status CANCELLED, filled_quantity 0`. Clean cancel, no fill, no position.

The payload carried `limit_price = reference_price = orb_intended_break_level = 1.9000` — all
identical = the bot's break level, computed ~3.5s before the cancel. The ask had moved ~4¢ away.

## 2. Current behavior (deployed trace)

**ORB intent (`services/orb_app.py::_build_open_intent`, running-high branch):**
```
"order_type": "limit",
"limit_price":  f"{entry_price:.4f}",          # entry_price = break level (signal-time)
"reference_price": f"{entry_price:.4f}",
"orb_intended_break_level": f"{entry_price:.4f}",
```

**OMS submit (`oms/service.py` ~577–584):** builds `OrderRequest(metadata=dict(event.payload.metadata),
order_type=metadata.get("order_type","market"), ...)` and calls `broker_adapter.submit_order(request)`.
**No re-pricing.**

**Webull adapter (`broker_adapters/webull.py` ~153–155):**
```
limit_price = self._meta_price(request, "limit_price", "reference_price")  # bot's value
if order_type in {"LIMIT","STOP_LIMIT"} and limit_price is not None:
    po.set_limit_price(str(self._round_to_tick(limit_price)))               # → Webull
```

So the bot's break level is the Webull limit. There is **no live-quote read anywhere in the
placement path** (unlike Schwab, which uses a market order in RTH and a live-ask limit in EH).

**Reference — the OMS already holds a live quote book** it can price from:
`oms/service.py:156 self._latest_quotes_by_symbol: dict[str, dict]` (symbol-keyed), populated by
`_handle_quote_tick_event` (:1739) from the Polygon `market-data-gateway` stream, with
`received_at` event-time stamping for staleness. The hard-stop/trail already prices off this book.

## 3. Goal

When the flag is ON, the **OMS sets ORB's entry limit from its own live (Polygon) quote at the
moment of placement** — the latest possible point, eliminating the ~2–3s bot→OMS lag — bounded so
it never chases past the break level + gap cap. When OFF, behavior is **byte-identical** to today.

Non-goals (explicitly out of scope): per-venue/Webull quote source (Pieces 2/3, parked); any
change to the exit/stop path (already live-quote-priced and shared); any v2 change.

## 4. Design

### 4.1 Flag
`MAI_TAI_ORB_OMS_QUOTE_PRICED_ENTRY_ENABLED` (settings: `orb_oms_quote_priced_entry_enabled: bool = False`).
Default OFF. When OFF, none of the code below executes → byte-identical.

### 4.2 ORB side (`orb_app.py`) — minimal, flag-aware
ORB continues to emit exactly as today **except** it always includes the inputs the OMS needs to
re-price (these are additive metadata; harmless when the flag is off because the OMS ignores them):
- `price_source: "ask"` (buy side; the side the OMS reads to price a marketable buy limit)
- `orb_gap_cap_pct: f"{self._rh_gap_cap_pct}"` (the bound; today's 1.5%)
- keep `orb_intended_break_level` (the bound base) and `reference_price`.

ORB still sets `limit_price` to the break level as today. **The OMS overrides it only when the flag
is on** (see 4.3). This keeps the ORB diff additive and the OFF path identical. (Alternative: omit
`limit_price` when the flag is on so a misconfig can't silently ship a stale price — see Open
Question Q1.)

### 4.3 OMS side (`oms/service.py`, submit path) — the re-pricing
At submission, **after** the `OrderRequest` is built and **before** `submit_order`, insert a single
guarded helper `_apply_orb_quote_priced_limit(request, event)`:

```
if not settings.orb_oms_quote_priced_entry_enabled:        return            # OFF → no-op
if event.payload.strategy_code != "orb":                   return            # ORB only
if request.intent_type != "open" or request.side != "buy": return            # entry buys only
if request.metadata.get("order_type") != "limit":          return
break_level = float(metadata["orb_intended_break_level"])
gap_cap     = float(metadata.get("orb_gap_cap_pct", 0)) / 100.0
bound       = break_level * (1.0 + gap_cap)

quote = self._latest_quotes_by_symbol.get(symbol)                            # Polygon book
fresh = quote and _is_fresh(quote["received_at"], max_age_ms=2000)           # reuse staleness rule
if not fresh or quote.get("ask") in (None, 0):
    -> ABANDON the entry: emit [OMS-ABANDON-INTENT] code=NO_FRESH_QUOTE (mirror Schwab skip);
       do NOT submit.
ask = float(quote["ask"])
if ask > bound:
    -> ABANDON: code=ASK_PAST_GAP_CAP (the move ran past the acceptable entry; don't chase).
       do NOT submit.
limit = ask                                                                  # marketable buy limit
request.metadata["limit_price"]   = f"{limit:.4f}"
request.metadata["reference_price"]= f"{limit:.4f}"
request.metadata["price_source"]  = "ask"
request.metadata["oms_quote_priced"] = "true"      # telemetry: distinguishes re-priced fills
# (adapter still applies _round_to_tick at place time)
```

Rationale for the bound semantics: ORB's own breakout gate already only emits when the fill would
be within `gap_cap` of the broken high, so by emit time the break level is within the cap. If, by
the later placement instant, the **ask** has risen past `break_level*(1+gap_cap)`, the move has run
beyond the acceptable entry → **abandon** (don't chase a runaway). When `ask ≤ bound`, place a
**marketable limit at the ask** → fills immediately at a known worst-case price. This is the AZI
case: at 09:48 the ask had moved ~4¢; a limit at the live ask would have filled instead of cancelling
(provided ask was within the 1.5% cap; if not, a deliberate clean miss rather than a chase).

### 4.4 Why this also neutralizes the drift-cancel
`_cancel_drifted_working_orders` cancels a working open order when the live quote drifts past the
limit by the 1¢ tolerance. With the limit set to the live ask at placement, there is essentially no
drift at birth, so the guard stops firing on these entries (it remains as a safety net). No change
to the guard itself.

## 5. Edge cases
- **No quote / stale quote (>2s):** abandon, `NO_FRESH_QUOTE`. (Same posture as Schwab's skip; better
  than shipping a stale limit.)
- **Locked/crossed quote (ask ≤ bid):** still price at ask; the bound check applies. (Marketable.)
- **Ask past gap cap:** abandon, `ASK_PAST_GAP_CAP`. Clean miss, no chase.
- **Partial fill then drift:** unchanged — same as today; the stop/registry path is untouched.
- **Tick rounding:** adapter's existing `_round_to_tick` still applies (the #374 fix) — re-pricing
  feeds it a raw float, identical to today's path.
- **Flag ON but metadata missing `orb_intended_break_level`:** treat as misconfig → abandon
  (`MISSING_BOUND`) rather than submit unbounded. Fail-closed.

## 6. Behavior when OFF (byte-identical proof obligation)
- `_apply_orb_quote_priced_limit` returns immediately on the flag check → the `OrderRequest` reaches
  `submit_order` unchanged.
- The additive ORB metadata (`price_source`, `orb_gap_cap_pct`) is inert (the adapter only reads
  `limit_price`/`reference_price`, which are unchanged).
- **Characterization tests must pass on the UNMODIFIED tree first**, then again after the change with
  the flag OFF — same intents, same orders, same fills (per the behavior-identical-refactor rule).

## 7. Testing
1. **Characterization (pre-change, unmodified):** record ORB open → OMS order → Webull request
   `limit_price` for a representative breakout; assert it equals the break level.
2. **Flag OFF (post-change):** identical to (1) — byte-identical.
3. **Flag ON, ask ≤ bound:** request `limit_price == ask`, `oms_quote_priced=true`.
4. **Flag ON, ask > bound:** abandoned, `ASK_PAST_GAP_CAP`, no `submit_order` call.
5. **Flag ON, no/stale quote:** abandoned, `NO_FRESH_QUOTE`.
6. **Flag ON, missing bound metadata:** abandoned, `MISSING_BOUND`.
7. Full OMS suite + ORB suite green; ruff clean.

## 8. Deploy (attended, after-close, ORB flat)
- Merge with flag **OFF** → confirm byte-identical in production (no behavior change).
- Enable `MAI_TAI_ORB_OMS_QUOTE_PRICED_ENTRY_ENABLED=true` **after close, ORB flat**, OMS restart;
  validate at the next open: a breakout → re-priced limit at live ask → fill (or a deliberate
  `ASK_PAST_GAP_CAP`/`NO_FRESH_QUOTE` abandon), `oms_quote_priced=true` on the fill.
- **Rollback:** flag OFF + OMS restart → byte-identical to today.
- Do **not** restart OMS while ORB holds (restart-while-holding still untested).

## 9. Open questions (need operator call)
- **Q1 — ORB metadata when flag ON:** keep emitting `limit_price=break_level` (OMS overrides) for a
  minimal/additive diff, OR omit it so a flag/strategy misconfig can never ship a stale price?
  (Recommend: keep it additive for now; the OMS guard + fail-closed abandons cover the risk.)
- **Q2 — Ask past gap cap:** **abandon** (recommended — no chase) vs. rest a non-marketable limit at
  the bound (may fill on a pullback, but reintroduces a drift/cancel surface).
- **Q3 — Cross buffer:** price exactly at ask, or ask + 1 tick to improve fill odds on a moving book?
  (Recommend: exactly at ask first; revisit if fills still slip.)

## 10. Explicitly parked / not in this PR
- **Pieces 2 & 3** — per-venue (Webull) quote book + venue-sourced stop. Blocked: Webull market-data
  not entitled (see header). The Polygon-vs-Webull stop-basis risk is real but unobserved and small.
- **v2 / Schwab** — untouched.
