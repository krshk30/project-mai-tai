# STEP-1 gate runbook — Webull native OCO combo bracket (attended, live, qty 1)

> **Read this end-to-end before running anything.** This is the Webull half of the go/no-go gate
> from [`oco-bracket-design.md`](oco-bracket-design.md), mirroring
> [`oco-step1-runbook.md`](oco-step1-runbook.md) (Schwab, PASSED 2026-07-21). Nothing ships to the
> live v2-Webull mirror until every item below passes on this broker.
>
> **Broker:** Webull (`live:orb` — the only live Webull account, shared with ORB).
> **Consumer:** the v2-Webull mirror (same methodology as Schwab v2: native OCO in RTH → software
> CW ladder in pre/post-market). **Fork:** A (MASTER = LIMIT/MARKET; a buy-STOP master 417s).
> **Code:** Phase 1 write side (PR #515), probe (#516), harness (this PR). **All times ET.**

---

## What is already done (do NOT redo)

- **Item 0 (preview) = PASSED off-hours 2026-07-22.** `scripts/webull_otoco_preview.py` on `live:orb`:
  MASTER **LIMIT** accepted (HTTP 200, `estimated_cost`), MASTER **MARKET** accepted (the real v2
  emit shape), MASTER **buy-STOP 417-rejected** (`invalid order_type, value: STOP_LOSS` — confirms
  Fork A). A `preview_order` validates without placing and works any hour.
- The exact `new_orders` shape (MASTER + STOP_PROFIT + STOP_LOSS, flat list, `EQUITY`/`US`/`CORE`,
  string qtys, per-leg `client_order_id` ≤40, one `client_combo_order_id`) is broker-confirmed.

## Two facts that drive the plan (Webull differs from Schwab here)

**1. NO OMS restart / no deploy is needed for the gate.** Unlike the Schwab gate (which flipped a
service env flag and restarted), the harness `scripts/webull_oco_step1.py` sets
`MAI_TAI_WEBULL_NATIVE_BRACKET_ENABLED=true` **for its own process only** and drives the adapter
directly. The deployed OMS flag stays **off** (the v2-Webull mirror emit is a later phase). So the
gate is a standalone harness run — the fleet does not need to be flat and nothing restarts.

**2. The item-1 run's real job is DATA CAPTURE.** The Webull `get_order_open` combo response shape
and the combo-cancel shape are **not yet known** (a preview does not reveal them). The harness
prints the RAW responses. Those captures are what we build the OMS **read side** from
(`fetch_armed_native_oco_symbols` + `fetch_oco_resolved_by_fill_symbols` for Webull, currently
deferred). Until then the harness's leg-state assertions are best-effort — **read the raw dump, do
not trust the PASS/REVIEW line alone on the first run.**

**Item 4 (the software-exit stand-down) is OUT OF SCOPE for this gate**, same as Schwab: it needs a
real v2-managed position with a bracket attached (the mirror emit wiring). This gate covers items
**0, 1, 2, 3, 5** — the broker-behaviour half, including the E5 one-cancels-other proof. Do not
record item 4 as passed from a green harness run.

---

## Pre-flight (attended, RTH)

- **Timing:** run **after 10:00 ET** so ORB's 09:30–10:00 entry window is closed — ORB and this
  gate share `live:orb`, and a STOP_LOSS leg is RTH-only on Webull anyway.
- **Symbol:** liquid, cheap, and **NOT on ORB's active watchlist** (so the mirror-collision guard
  is moot and the gate never fights an ORB position). A boring large-cap like `F` is ideal.
- **ORB flat on this symbol:** confirm ORB is not holding / not resting an order on the chosen name.

```bash
ssh mai-tai-vps 'cd /home/trader/project-mai-tai && git pull --ff-only && git rev-parse --short HEAD'
# confirm the webull account + creds resolve (expect a non-empty account id)
ssh mai-tai-vps 'sudo bash -c "set -a; . /etc/project-mai-tai/project-mai-tai.env; set +a; sudo -E -u trader .venv/bin/python -c \"from project_mai_tai.settings import get_settings; from project_mai_tai.broker_adapters.webull import configured_webull_accounts as c; print(sorted(c(get_settings())))\""'
```

## Item 0 re-confirm (optional, zero risk) — preview accepts the shape

```bash
ssh mai-tai-vps 'sudo bash -c "set -a; . /etc/project-mai-tai/project-mai-tai.env; set +a; sudo -E -u trader .venv/bin/python scripts/webull_otoco_preview.py --account live:orb --symbol F --entry-type MARKET"'
# expect: RESULT: PREVIEW ACCEPTED
```

## Item 1 + 5 — the combo RESTS far from market, then CANCELS clean (and CAPTURE the shapes)

```bash
# dry-run first (preview only, nothing placed):
ssh mai-tai-vps '... .venv/bin/python scripts/webull_oco_step1.py --stage rest --symbol F'
# then LIVE qty-1 (real money): MASTER limit ~50% below market -> cannot fill -> rests -> cancel
ssh mai-tai-vps '... --env ... .venv/bin/python scripts/webull_oco_step1.py --stage rest --symbol F --confirm'
```
- **CAPTURE:** copy the `[RAW get_order_open]` and `[CANCEL ...]` JSON from the output — this is the
  data the read-side build needs. Save it to the OCO memory / this doc.
- **Item 1 (rests):** the MASTER works; the STOP_PROFIT/STOP_LOSS children are attached but NOT
  independently working (they arm only on the MASTER fill). Verify against the raw dump.
- **Item 5 (cancel + flat):** the combo cancels clean; no live legs remain; account flat.

## Item 2 + 3 — fill arms the pair, one leg cancels the other (the E5 proof)

```bash
# marketable MASTER (limit through the offer) -> fills -> both exits arm.
ssh mai-tai-vps '... --leave-open ... scripts/webull_oco_step1.py --stage fill --symbol F --confirm --leave-open'
```
- **Item 2 (atomic-at-fill):** on the MASTER fill, BOTH SELL legs (STOP_PROFIT + STOP_LOSS) go live
  together — inspect the raw dump.
- **Item 3 (one-cancels-other = the E5 proof):** with the pair resting, **make one leg marketable
  BY HAND in the Webull app** (e.g. drag the STOP_PROFIT limit down to the bid). It fills → the
  broker **auto-cancels the sibling** STOP_LOSS. One fill, no second sell, no oversell. This is the
  whole point.
- **THEN FLATTEN:** with `--leave-open` the position is protected ONLY by the broker OCO. Once item
  3 is observed, ensure the position is closed (the winning leg closed it, or flatten by hand).
  **Never leave `live:orb` holding at the session end.**

## Result → next

- All of 0/1/2/3/5 pass → the Webull broker mechanism is proven. Harden the read side from the
  captured shapes (`fetch_armed_native_oco_symbols` + `fetch_oco_resolved_by_fill_symbols`), then
  wire the v2-Webull mirror emit → item 4 becomes testable → survival test → live enable.
- Any REVIEW/FAIL → read the raw dump, fix the shape/parse, re-run. The preview passing means the
  place shape is right; a failure here is about the working/fill/cancel lifecycle, not the payload.

## Safety invariants (the harness enforces, but verify by eye)

- qty **1** only; MASTER **LIMIT/MARKET** only (buy-STOP 417s); symbol **not** ORB's.
- try/finally **always** attempts the combo cancel; a held position is flagged for **manual**
  flatten (the harness does not auto-market-sell a live position).
- The deployed OMS `webull_native_bracket_enabled` stays **off** throughout — the gate uses its own
  process flag, so the live OMS behaviour is unchanged.
