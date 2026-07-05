# v2 Extended-Hours EXIT routing fix — DESIGN (mirror of #362 for the exit side)

**Status:** design-first, for operator review. **Ships today** (Sun 2026-07-05) — live open-risk
at Monday 07-06 ~7 AM ET pre-market (v2 trades from 7 AM; a pre-market entry currently cannot be
exited or stopped until 9:30 RTH). CLRO (bought 10 @ 7.185 post-market 2026-07-04) is being closed
manually by the operator in ToS; this fix prevents the next one.

**Class:** OMS-area, live money → full deploy discipline (PR + validate green, attended, fleet-flat
OMS restart, explicit GO).

---

## 1. The bug (confirmed, evidence-pinned)

v2 EXITS route as **MARKET regardless of session**, so they cannot fill in extended hours →
after-hours / pre-market positions are stuck open and unprotected until RTH. Live proof: CELZ
2026-06-30 (10 @ 1.2687 entered 16:30 ET) — the OMS-managed exit churned `-10 MKT` every ~30s, all
cancelled/accepted-never-filled; operator closed it manually via a ToS limit. CLRO 2026-07-04 repeats
it exactly.

**Locus — a single function.** All three v2 managed exits (scale-out partials, floor/ratchet, AND the
intrabar hard stop) flow through `_emit_v2_managed_sell` (`oms/service.py` ~1233-1306), which hardcodes:

```python
"order_type": "market",   # metadata, service.py:1260
...
order_type="market",      # OrderRequest, service.py:1293
```

with **no** `session` / `limit_price` / `extended_hours`. The Schwab adapter defaults `session` to
`NORMAL` (`broker_adapters/schwab.py:746`) → Schwab won't fill a NORMAL/market order in an AM/PM
session.

**The stop/hard-exit path — explicitly checked (operator's concern).** The broker-native resting stop
guard (`_arm_or_rearm_native_stop_guard`) is **RTH-only by design** — it early-returns off-hours
(`service.py:883-884`). So in extended hours the v2 hard stop is NOT a resting broker order; it fires
through the SAME `_evaluate_v2_managed_exit` → `_emit_v2_managed_sell` market path. **Therefore this one
fix also makes the EH hard/protective exit fillable** — it is not just the scale exits.

---

## 2. The model already in-file (Path B — copy it)

The legacy armed-hard-stop path already does this correctly in `_build_hard_stop_metadata`
(`service.py:1939-1975`):

```python
routed_price = _panic_limit_price(trigger_price, stop.initial_panic_buffer_pct)   # buffered marketable limit
metadata.update({"order_type": "limit", "limit_price": routed_price,
                 "reference_price": routed_price, "price_source": "bid" ...})
session = _extended_hours_session()
if session is not None:
    metadata.update({"session": session, "extended_hours": "true"})
```

And the entry-side leaf from #362 (`strategy_core/order_routing.py`) established the RTH-byte-identical
contract: return `{}` (market/NORMAL) in RTH, stamp `session=AM|PM` + `order_type=limit` + `limit_price`
+ `price_source=ask(buy)/bid(sell)` in EH.

The adapter (`_build_order_payload`, `schwab.py:744-770`) already honors `session` / `order_type` /
`limit_price` (falls back to `reference_price` for the price). **No adapter change required.**

---

## 3. The fix

**Scope: `_emit_v2_managed_sell` only + its 3 call sites in `_evaluate_v2_managed_exit`.** No adapter
change, no strategy change, no new import (reuse the in-module `_extended_hours_session` /
`_panic_limit_price` that Path B already uses).

**Design contract (mirrors #362):**
- **RTH → byte-identical to today.** `_extended_hours_session()` returns `None` → order stays
  `order_type=market`, no `session`, no `limit_price`. Zero regression to the proven live scalp ladder.
- **Extended hours (AM/PM) → fillable LIMIT.** Stamp `order_type=limit`, `session=AM|PM`,
  `extended_hours=true`, `price_source=bid`, and `limit_price` = a **marketable buffered limit off the
  live bid**: `_panic_limit_price(bid, buffer_pct)` = `bid × (1 − buffer_pct/100)`, floored at 0.01.

**Per-leg pricing (operator decision 2026-07-05).** A marketable limit fills AT the bid — the buffer is
a disaster-floor, not a price paid — so we differentiate by whether the leg *must* fill:
- **Protective legs — hard-stop + floor (`intent_type="close"`): marketable buffer, default 0.5%.**
  `limit_price = _panic_limit_price(bid, oms_v2_exit_eh_protective_limit_buffer_pct)` = `bid × (1−0.5%)`.
  These fire *because* price is moving against us (esp. the hard stop), so they must reliably cross the
  spread and fill even if the ~5s-stale snapshot bid has stepped down by order-arrival. Fills at the live
  bid; the floor only bites in a genuinely thin book.
- **Scale partials (`intent_type="scale"`): at the bid, zero buffer.** `limit_price = bid`. Profit-taking
  is patient — if the bid moved and it doesn't fill this quote, that's harmless (we keep the position and
  the next quote re-evaluates). Honors best-price where not-filling costs nothing.

**Why bid-anchored (not the leg level):** anchor to the **live bid** (in scope at `service.py:1176`), not
the leg `reference_price` (stop/floor/scale level), because in EH the leg level can be far from a gapped
bid → pricing at the leg level may not fill. The eval already guarantees `bid > 0` and fresh
(`service.py:1169-1178`, staleness-guarded by `oms_v2_exit_quote_max_age_ms`) before any emit, so a usable
bid always exists at emit time.

**Buffer must exceed the adverse move over the snapshot window to stay marketable** — 0.5% covers the
typical ≤5s drift; tunable via `oms_v2_exit_eh_protective_limit_buffer_pct` if fast pre-market names churn.

**`reference_price` is left as the leg level (unchanged).** The adapter uses `limit_price` first and only
falls back to `reference_price`, so the routed limit drives the live order while `reference_price` stays
the leg level for the `[OMS-V2-MANAGED-EXIT]` log and the live-paper re-score agreement the docstring
depends on ("FILL reference_price is the leg LEVEL so live-paper agrees with the re-score"). This keeps
the simulated-adapter / paper-isolation behavior unchanged — verified in tests (§6).

**Signature change:** add `bid: float | None = None` to `_emit_v2_managed_sell`; the 3 call sites
(`service.py:1198/1206/1216`) all have `bid` in scope and pass it. If `bid` is somehow absent/≤0 at emit
(shouldn't happen — eval guards it), fall back to **market** (today's behavior) rather than blocking the
exit — fail-safe, never worse than today.

**Buffer setting (new, single, tunable):** `oms_v2_exit_eh_protective_limit_buffer_pct`, default **0.5**
(%). Governs the **protective** legs (hard-stop + floor) only; scale partials price at the bid (no
setting). 0.5% crosses through a reasonable resting bid while capping thin-book damage.

### Pseudocode (the only changed block)
```python
async def _emit_v2_managed_sell(self, session, row, *, intent_type, quantity,
                                reference_price, reason, bid: float | None = None):
    ...
    metadata = {
        "oms_v2_managed_exit": "true",
        "reference_price": f"{float(reference_price):.4f}",
        "order_type": "market",
        "time_in_force": "day",
    }
    order_type = "market"
    session_code = _extended_hours_session()
    if session_code is not None and bid and bid > 0:      # EH only
        if intent_type == "scale":
            routed = _format_limit_price(bid)             # profit-taking: at the bid, zero buffer
        else:                                             # "close" = hard-stop / floor: buffered marketable
            buffer_pct = float(getattr(self.settings, "oms_v2_exit_eh_protective_limit_buffer_pct", 0.5))
            routed = _panic_limit_price(bid, buffer_pct)
        if routed is not None:
            order_type = "limit"
            metadata.update({
                "order_type": "limit",
                "limit_price": routed,
                "price_source": "bid",
                "session": session_code,
                "extended_hours": "true",
            })
    # OrderRequest(order_type=order_type, ...)   # was hardcoded "market"
```

---

## 4. No new master flag — reasoning

Mirrors #362 (entry side added no flag; EH-conditional, RTH `{}`). The whole v2 managed-exit path
already rides one flag, `oms_v2_exit_management_enabled` (checked at `service.py:1166`) — this change
lives entirely inside it. A dedicated rollback flag's "off" state would be the **known-broken**
market-in-EH behavior, so it isn't a meaningful rollback target. Rollback = revert the commit + OMS
restart (env changes need a restart anyway). The `oms_v2_exit_eh_limit_buffer_pct` setting gives live
tuning without code changes.

---

## 5. Edge cases / honest boundaries

- **AM DAY-limit at the 9:30 boundary:** an AM-session DAY limit that doesn't fill in pre-market expires
  at the session roll; the tick consumer re-evaluates within seconds at RTH and re-emits (now a market
  order, RTH-valid) → self-heals. No unprotected gap beyond one eval cycle.
- **Thin/zero-bid EH book:** buffered marketable limit fills at the best available bid down to
  `bid×(1−0.5%)`; if the book is emptier than that it rests until a bid appears (same as any limit) —
  strictly better than a market order that can't route at all. Floor prevents a $0.01 fill.
- **Simulated/paper adapter:** `reference_price` unchanged (leg level) → sim fill logic and
  `test_v2_exit_paper_isolation` unaffected; new EH `limit_price` only steers the live Schwab route.
  Confirm in tests that the simulated adapter fills a limit sell (or is untouched by the added key).
- **RTH:** provably byte-identical (`_extended_hours_session()` is `None` → the `if` is skipped, every
  field equals today).
- **Not in scope:** entry routing (already fixed, #362); ORB exits (separate bot, in-memory trail +
  Webull native stop — different path); Phase-2 resting brackets (open item #6).

---

## 6. Tests

`tests/unit/test_oms_service.py` (or the v2-exit test module):
- **RTH byte-identical:** frozen clock at 14:00 ET → emitted OrderRequest/metadata `order_type=="market"`,
  no `session`/`limit_price` (characterization: assert equality to today's output).
- **PM exit → limit+session:** frozen clock 16:30 ET, bid=1.20 → `order_type=="limit"`, `session=="PM"`,
  `extended_hours=="true"`, `limit_price==_panic_limit_price(1.20, 0.5)`, `price_source=="bid"`.
- **AM exit → session=="AM"** (frozen 08:00 ET).
- **All three legs** (HARD_STOP / FLOOR_BREACH / SCALE_level) carry the EH routing in extended hours.
- **Fail-safe:** EH but bid missing/≤0 → falls back to `order_type=="market"` (never blocks the exit).
- **paper-isolation** test still green (reference_price unchanged; route still pinned to the row account).
- ruff clean (box venv).

---

## 7. Deploy (full discipline)

1. PR off `origin/main` (`791c6f6`), branch `claude/v2-eh-exit-routing`.
2. CI `validate` GREEN (unit + ruff). Merge only on genuine green (no admin-bypass).
3. Attended, fleet-flat: confirm v2 FLAT (no open `oms_managed_positions`) at the restart moment.
   Re-run the running-tree-vs-origin/main drift check at pull time.
4. `git pull` + restart **OMS only** (isolated; v2/strategy/ORB untouched — the exit path is OMS-side).
   Capture new OMS PID; 0 tracebacks; `/proc` confirms.
5. **Verdict window = Monday 07-06 pre-market (07:00–09:30 ET), attended.** If v2 takes a pre-market
   entry, watch the exit emit as `order_type=limit session=AM` and confirm it FILLS (not the CELZ/CLRO
   `-N MKT` churn). Rollback = revert commit + OMS restart.
