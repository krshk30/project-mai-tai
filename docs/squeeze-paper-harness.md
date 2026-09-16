# Squeeze paper harness: pre-registered protocol

**Status:** design frozen before implementation and before the first forward paper event.

**Strategies:** `SQZ30` and `SQZ60`, paper only.

**Purpose:** measure whether the historical pre-market squeeze result survives a next-print fill
model on a forward, universe-wide trade stream. This is a measurement harness, not a live trading
strategy and not a recommendation to change the scanner.

The protocol below may be corrected during independent review before implementation. Once the
first paper event is recorded, thresholds, windows, fill rules, exit rules, and the pass criterion
must not be changed. Later hypotheses are separate, versioned studies over the captured paths.

## 1. Evidence and scope

The motivating study covered 30 mornings from 2026-08-05 through 2026-09-15 using Massive
one-second aggregates. In the pre-market-only 60-second population (`n=65`), the `+5% / -15%`
rule produced an 82% win rate and +1.44% average return when entered at the crossing print, and an
80% win rate and +1.52% average return one print later. The 30-second rule was worse in every
reported cell. Tight stops were especially sensitive to sub-second latency.

Those results motivate this harness; they are not its acceptance evidence. The forward result is
graded only by the rules in this document.

The harness has these hard boundaries:

- It never uses a live brokerage account, broker adapter, OMS route, strategy-intent stream, or
  live position table.
- It never changes the momentum scanner, ORB, ATR v2, gateway, OMS, strategy service, or control
  service.
- It does not detect events before 04:11:00 ET or at/after 09:30:00 ET.
- It reports gross paper P&L. Commissions, fees, borrow availability, locate costs, partial fills,
  and executable depth are not modeled.
- `SQZ30` and `SQZ60` are separate populations. Their events and P&L are never pooled.

## 2. Frozen universe and clock

All session boundaries use `America/New_York`, including daylight-saving transitions.

### U1: detection session

Detection prints are accepted from **04:11:00.000000 through 09:29:59.999999 ET**. Prints before
04:11:00 and at or after 09:30:00 cannot create an event.

### U2: prior-close floor

At 03:55 ET, the service fetches the previous trading day's grouped-daily Massive bar once. A
symbol is eligible only when that snapshot contains a finite prior close greater than or equal to
`$1.00`.

The previous trading day is calendar-aware, not simply the previous date. The snapshot and source
date are persisted before detection begins. Missing, malformed, or failed prior-close evidence
excludes the affected symbol; it is never replaced with a live price or an older close. A restart
reuses the persisted snapshot for that session and does not silently refresh the universe.

### U3: symbol-shape gate

Eligible symbols contain no `.` and have at most five characters. This is a syntactic parity gate,
not a reliable asset-class classifier. Warrant- or unit-like names that pass it, such as `PSNYW`
or `WVVIP`, remain eligible so the forward population matches the historical study, but every row
records `is_warrant_like=true` when the available symbol metadata or suffix heuristic identifies
one. Any later decision to exclude them is an offline comparison, not a retroactive edit.

## 3. Frozen detection rules

The service consumes Massive trade prints in received order and preserves each exchange timestamp,
price, size, and condition list. A strategy needs at least one qualifying prior print before it can
detect an event.

### D1: rolling windows

For each eligible symbol, maintain independent trailing windows:

- `SQZ30`: 30 seconds.
- `SQZ60`: 60 seconds.

Every window and every "strictly later" comparison uses Massive's SIP timestamp field `t`. The
participant timestamp `y` is stored as evidence but is never used for ordering, windows, dedupe,
fills, or exits.

Before a raw `T.*` print can be a reference, detection, fill, or exit, it must be eligible to update
Massive's consolidated price aggregates. Eligibility is derived from the versioned stock-trade
condition metadata returned by `/v3/reference/conditions`: every attached condition must permit the
applicable consolidated OHLC update. A condition that prevents consolidated high/low or open/close
updates makes the print ineligible for all strategy decisions. This excludes corrections, cancels,
average-price trades, and every other condition Massive excludes from aggregate OHLC. When multiple
conditions are attached, any ineligible condition wins. Unknown condition metadata fails closed.

Ineligible prints remain counted as `excluded_prints` per event and per session but never enter a
price window or paper path. The session count covers every rejected raw print while connected. An
event count covers rejected prints for its symbol from the start of its reference window through
the end of its capture horizon. The condition metadata snapshot and its retrieval timestamp are
persisted with the session evidence so the filter is reproducible.

For an eligible candidate print at SIP time `t`, the reference population is eligible prints with
SIP timestamps in `[t-W, t)`. The candidate print itself and any print sharing its SIP timestamp are
not eligible as the reference. Non-positive or non-finite price/size values are rejected from the
calculation and counted separately in health telemetry.

### D2: trigger

An event occurs when:

```text
candidate_price >= 1.30 * minimum_reference_price
```

The exact minimum reference print supplies `ref_ts` and `ref_px`. A tie uses the earliest received
print among the tied minima. The candidate supplies `detect_ts` and `detect_px`. `move_pct` is
`(detect_px / ref_px - 1) * 100`.

### D3: per-strategy dedupe

Each strategy permits one event per symbol per five minutes. A later event is accepted only when
its detection timestamp is **strictly more than 300 seconds** after that strategy's prior accepted
event for the symbol. `SQZ30` does not suppress `SQZ60`, and `SQZ60` does not suppress `SQZ30`.

### D4: detection evidence

Each event stores the reference and detection prints, the number of prints in the reference
window, and the sum of their shares. Detection evidence is immutable after insertion.

## 4. Frozen paper entry model

### E1: next-print fill

A paper marketable BUY is created at detection. It fills at the price of the first valid trade
print whose exchange timestamp is **strictly later** than `detect_ts`. A print at the same timestamp
does not fill the order.

The fill window is `(detect_ts, detect_ts + 10 seconds]`. If no valid print arrives in that window,
the event expires as `NO_FILL`. It remains in the event denominator but not the filled-event
denominator.

### E2: fixed paper size

The frozen notional is **$500 per strategy event**:

```text
qty = max(1, floor(500 / fill_px))
```

For a fill above $500, the one-share minimum makes actual notional exceed $500; the row records the
actual entry notional. There is no portfolio cash constraint and no cross-strategy netting because
the purpose is event-level measurement.

### E3: execution boundary

The existing `SimulatedBrokerAdapter` is not used: it fills immediately at a supplied reference
price and therefore cannot implement the required next-print latency model. The isolated service
simulates entry and exit from its own trade stream and writes only its dedicated durable paper
ledger.

## 5. Frozen exit and path rules

All thresholds are calculated from `fill_px`.

### X1: target

The target is `fill_px * 1.05`. The paper position exits at the first post-fill print at or above
that level.

### X2: stop

The stop is `fill_px * 0.85`. The paper position exits at the first post-fill print at or below
that level. The observed print is the exit price, so adverse print slippage is retained.

### X3: time stop

If neither price exit wins, the position exits at the first valid print strictly after
`fill_ts + 600 seconds`.

If no such print is observed before capture ends, the event is `UNANSWERABLE`, not assigned a
synthetic 600-second price and not included in win rate or average P&L.

### X4: same-second conflict

If any target-qualified and stop-qualified prints occur in the same exchange-clock second, the
stop wins for that second regardless of intra-second arrival order. The service therefore does not
finalize a target until that exchange second is complete. This intentionally conservative rule
matches the historical replay.

### X5: full path

Every event records its eligible print path beginning with the detection print. For a filled event,
capture continues through `fill_ts + 600 seconds`, even if its paper exit occurred earlier. The
stored path is the ordered sequence of `(dt_ms, px, size)` values relative to `detect_ts`; the
detection print has `dt_ms=0`. It contains the fill print and every accepted print used by the exit
evaluator. Exit thresholds and the 600-second exit clock remain anchored to `fill_ts`, not
`detect_ts`.

For `NO_FILL`, the path covers `detect_ts` through `detect_ts + 10 seconds`. This detect-anchored
evidence permits both the historical cross-print entry and the frozen next-print entry to be graded
offline from one event without changing the forward paper verdict.

`mfe_pct`, `mae_pct`, and `halt_gap_s` are computed over the post-fill slice through
`fill_ts + 600 seconds`, not the pre-fill segment and not merely through the paper exit. The same
detect-anchored path is the only input to later offline comparisons of the cross-print entry, the
next-print entry, a `+10%` target, `-5%/-8%/-10%` stops, or different time stops. Those comparisons
cannot alter the frozen forward grade.

## 6. Isolated data service

The implementation is a new service, `mai-tai-squeeze-paper`, with its own systemd unit and no
imports from broker adapters or OMS submission code.

### 6.1 Feed lifecycle

1. At 03:55 ET, fetch and persist the prior-close snapshot.
2. At 04:00 ET, open a dedicated Massive websocket using `MAI_TAI_MASSIVE_API_KEY` and subscribe to
   `T.*`.
3. From 04:11 through 09:29:59 ET, evaluate eligible prints for `SQZ30` and `SQZ60`.
4. At 09:30 ET, stop detection and unsubscribe from `T.*`.
5. If any filled or fill-pending event still needs its 10-second fill window or 600-second path,
   subscribe only to those symbols until every required path is complete. Remove each symbol when
   its final event completes.
6. Disconnect no later than 09:40:01 ET.

The symbol-specific tail is required because an event detected at 09:29:59 can need prints through
09:39:59. Keeping `T.*` through that period would violate the universe-wide pre-market-only load
boundary; disconnecting at 09:31 would violate the mandatory ten-minute path.

### 6.2 Feed failure semantics

Subscription refusal, disconnect, timestamp regression, or an unmeasured gap is visible in service
health. The service reconnects for capture but never backfills a forward paper decision from REST
and never invents a print. Events whose fill or exit cannot be answered from a continuous observed
path are `UNANSWERABLE` and excluded from performance metrics with their count shown.

The gateway is not used. Its watchlist scope and five-second snapshots cannot answer a study whose
historical median time to target was five to eight seconds.

### 6.3 Durable ledger

Use dedicated append-only squeeze-paper tables. One immutable event row stores the scalar fields;
the full path may be stored as a JSONB sequence or an event-child table, provided export reproduces
the exact ordered `path[(dt_ms relative to detect_ts, px, size)]` without aggregation. Runtime state
is rebuilt only from these tables after restart.

Every persisted print also stores its raw SIP `t`, participant `y`, and condition codes. These are
evidence fields; only SIP `t` supplies `dt_ms` or participates in strategy ordering.

Each event records:

```text
strategy, symbol, prior_close, prior_close_session, session_day,
ref_ts_utc, ref_ts_et, ref_participant_ts, ref_px,
detect_ts_utc, detect_ts_et, detect_participant_ts, detect_px,
move_pct, window_prints, window_shares, excluded_prints, order_ts_utc, order_ts_et,
fill_ts_utc, fill_ts_et, fill_px, latency_ms, qty, entry_notional_usd,
exit_reason, exit_ts_utc, exit_ts_et, exit_px, pnl_pct, pnl_usd,
mfe_pct, mae_pct, halt_gap_s, is_warrant_like, evidence_status,
condition_metadata_asof, path_relative_to_detect
```

`exit_reason` is one of `target`, `stop`, `time`, `no_fill`, or `unanswerable`. UTC is authoritative;
ET is stored or rendered for operator readability.

## 7. Paper bot surfaces

The code PR retires `polygon_30s` from the active paper-bot registration and replaces its active
surface with two independent paper strategies:

- `sqz30` / `SQZ30`
- `sqz60` / `SQZ60`

Historical Polygon paper rows are retained; retirement means no active runtime registration,
navigation card, or evaluation. ORB paper remains unchanged.

The runtime registry, `CONTROL_PLANE_ACTIVE_BOT_CODES`, `BOT_PAGE_META`, routes, navigation, and
paper-aware pills register both new strategies. One isolated service may evaluate both, but each
page and every row keeps its strategy identity.

Each strategy card/page shows service and feed health, events today, filled, open, closed, gross
P&L, and the latest 20 event rows. Empty states distinguish `UNEXERCISED`, `NO_EVENTS`, feed
failure, and prior-close failure; an empty table never implies success.

The cards also show a non-gating historical-rate health band: `SQZ60` averaged about 2 detections
per morning with an observed range of 0-8, and `SQZ30` averaged about 1.5. More than 10 accepted
detections for either strategy in one morning sets that strategy's health to `DETECTOR_SUSPECT`.
This warning does not suppress events, alter the formal grade, or convert a low-count morning into
an error.

## 8. Frozen grading rule

The first formal grade occurs after **20 complete sessions**. Earlier daily tables are operational
evidence only and cannot pass or fail a strategy.

For each strategy separately:

- **P1 population:** at least 40 filled, gradable events.
- **P2 performance:** the next-print `+5% / -15% / 600-second` rule has win rate at least 70% and
  arithmetic mean `pnl_pct` at least +1.0%.
- **P3 robustness:** P2 remains true after dropping each single session in turn and after dropping
  each single symbol in turn. Every reduced population prints its remaining count. A missing or
  unanswerable result cannot be treated as a win or silently omitted from the evidence census.

The overall result is `PASS` only when P1, P2, and P3 all pass. Otherwise it is `FAIL`; if the
20-session evidence cannot be measured, it is `COULD_NOT_TELL`. The report prints all event,
filled, no-fill, unanswerable, target, stop, and time-exit counts with denominators.

`SQZ30` versus `SQZ60` is read from the same frozen report. The operator decides what, if anything,
to do after the grade; the harness does not auto-promote a strategy.

## 9. Required controls before deployment

All tests use fixtures and make no network calls.

1. A 30% move in 45 seconds makes `SQZ60` fire and `SQZ30` stay silent.
2. A 30% move in 25 seconds makes both strategies fire.
3. 04:10:59 is rejected, 04:11:00 accepted, 09:29:59 accepted, and 09:30:00 rejected.
4. Prior close `$0.99` is excluded and `$1.00` included.
5. Fill is the first print strictly after detection, never the detection print or a same-timestamp
   print.
6. A cancelled, corrected, average-price, or otherwise aggregate-ineligible print at +40% fires
   neither strategy and cannot fill or exit one; its exclusion is counted.
7. SIP `t` controls ordering even when participant `y` disagrees.
8. Target and stop prints in one exchange second resolve to stop.
9. The time stop uses the first print strictly after 600 seconds from the fill.
10. The stored path starts at detection with `dt_ms=0`, includes the fill and every print used by
    exit evaluation, and continues through 600 seconds after the fill despite an early exit.
11. A second event 200 seconds later is ignored and one 301 seconds later is accepted.
12. No later print within 10 seconds produces a durable `NO_FILL` row whose path remains anchored
    to detection.
13. A 09:29:59 event continues on a symbol-only subscription after `T.*` is removed and records its
    full path without creating a post-09:30 detection.
14. Feed gaps and missing prior-close evidence produce explicit unanswerable/excluded counts, never
    clean paper P&L.
15. Static and dispatch controls prove the package cannot publish an intent, import a broker
    adapter, or write live position/order tables.
16. Dashboard controls prove both strategies remain separate, the expected-rate band and
    `DETECTOR_SUSPECT` threshold render correctly, and the retired Polygon registration
    is absent while ORB remains.

Each numbered rule receives a mutation that makes its named control fail. The full-suite controlled
pair compares failure-name sets, not only counts.

## 10. Deployment and first-session acceptance

Deployment is after market close on explicit operator approval. It installs and starts only the new
service and its paper pages; no gateway, OMS, strategy, ATR v2, ORB, or control-service restart is
part of this change.

Before enabling the unit, prove the Massive `T.*` entitlement and confirm that the dedicated
connection does not displace an existing production connection. A failed proof leaves the unit
disabled.

The first session is a paper dry run. At 09:35 ET the operator must be able to see:

- the prior-close snapshot date and eligible-symbol count;
- Massive connection/subscription health and any gaps;
- separate `SQZ30` and `SQZ60` event/fill/open/closed counts;
- each strategy's expected-rate health band and any `DETECTOR_SUSPECT` warning;
- each event's reference, detection, next-print fill, latency, and current/finished path;
- no strategy intents, broker orders, live positions, or OMS-managed rows attributable to either
  strategy.

A quiet session is `UNEXERCISED`, not a pass. Formal performance grading remains locked until 20
complete sessions.
