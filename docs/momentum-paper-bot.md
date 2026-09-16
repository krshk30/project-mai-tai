# Momentum Bot paper test: locked rules

**Status:** pre-registered before implementation and before the first paper event. These rules may
be corrected during review, but they cannot be tuned after paper trading begins.

**Purpose:** test whether a fast pre-market momentum move remains profitable when entry is modeled
at the next real print instead of the detection print. This is paper-only measurement, not a live
strategy.

## The two bots

| Bot | Trigger | Historical event rate |
|---|---|---:|
| **Momentum 30** (`momentum_30s`) | Price rises at least 30% from the lowest eligible print in the prior 30 seconds | About 1.5 per morning |
| **Momentum 60** (`momentum_60s`) | Price rises at least 30% from the lowest eligible print in the prior 60 seconds | About 2 per morning; observed range 0-8 |

The bots use the same universe, entry, exit, sizing, and grading rules. They are measured and shown
separately; one never suppresses or improves the other's result.

## Locked trading rules

| Rule | Definition |
|---|---|
| **Hours** | New detections are accepted only from **04:11:00 through 09:29:59 ET**. |
| **Price floor** | Prior close must be at least **$1.00**, using the previous trading day's Massive grouped-daily close captured once at 03:55 ET. Missing evidence excludes the symbol. |
| **Symbol shape** | No `.` and no more than five characters. Warrant- or unit-like names that pass remain included for study parity but are labeled. |
| **Trade eligibility** | A raw Massive `T.*` print can be a reference, detection, fill, or exit only when its condition codes are eligible to update Massive consolidated OHLC. Corrections, cancels, average-price trades, unknown conditions, and other ineligible prints are excluded and counted. |
| **Clock** | Every window and ordering decision uses Massive's SIP timestamp `t`. Participant timestamp `y` is stored as evidence but never drives a decision. |
| **Reference** | Lowest eligible print in `[t-window, t)`. The candidate and any same-SIP-timestamp print cannot be the reference. |
| **Repeat signal** | One event per bot, symbol, and five minutes. A new event requires **more than 300 seconds** since that bot's previous event. |
| **Paper entry** | Buy at the **first eligible print strictly after detection**. Same-timestamp prints do not fill. No print within 10 seconds produces `NO_FILL`. |
| **Paper size** | **$500 per event**; `qty = max(1, floor(500 / fill_price))`. P&L is gross and does not model fees, borrow, partial fills, or market depth. |
| **Target** | First post-fill print at or above **+5%** from fill. |
| **Stop** | First post-fill print at or below **-15%** from fill, using the observed print price. |
| **Time exit** | If neither price exit occurs, use the first eligible print strictly after **600 seconds from fill**. |
| **Same-second conflict** | If target and stop both occur in one exchange-clock second, the **stop wins**. |
| **No answer** | A feed gap or missing exit print produces `UNANSWERABLE`, never a synthetic price or a clean result. |

## Evidence saved for every event

The path begins at the detection print (`dt_ms=0`). For a fill, it continues through
`fill_time + 600 seconds`, even after an early exit. For `NO_FILL`, it continues through the
10-second fill window. Exit rules remain anchored to the fill, not detection.

Each event stores ET and UTC times, bot, symbol, prior close, reference and detection prints,
window print/share counts, excluded-print count, detection-to-fill latency, fill and quantity,
exit and P&L, MFE/MAE, largest no-print gap, warrant-like label, condition-metadata version, and the
ordered print path. Every path print retains SIP `t`, participant `y`, condition codes, price, and
size.

This one detect-anchored path can grade both the historical detection-print entry and the forward
next-print entry offline. Offline comparisons may test other targets or stops, but they cannot
change the frozen forward result.

## Isolation and screen

- A dedicated `mai-tai-momentum-paper` service opens its own Massive websocket at 04:00 ET and
  subscribes to `T.*`. It does not use the gateway, OMS, broker adapters, live accounts, strategy
  intents, or live position/order tables.
- At 09:30 ET it stops detections and removes `T.*`. Only symbols with unfinished fill windows or
  ten-minute paths remain subscribed; all connections close by 09:40:01 ET.
- Paper events and paths are append-only and restart-safe. REST data never backfills a forward
  decision.
- The existing active `polygon_30s` paper registration is retired, but its historical records are
  retained. ORB paper remains unchanged.
- Momentum 30 and Momentum 60 each get a paper card showing feed health, events, filled/open/closed,
  gross P&L, and the latest 20 rows.
- More than **10 detections for either bot in one morning** shows `DETECTOR_SUSPECT`. This is a
  health warning only; it does not suppress events or change the grade.

## Decision after 20 sessions

Each bot is graded separately after 20 complete sessions:

1. At least **40 filled, gradable events**.
2. Win rate at least **70%** under the next-print `+5% / -15% / 600-second` rule.
3. Average `pnl_pct` at least **+1.0%**.
4. Rules 2 and 3 still pass after dropping every single session in turn and every single symbol in
   turn; every reduced sample prints its count.

The result is `PASS` only when all four requirements pass. It is `FAIL` when a measured requirement
fails and `COULD_NOT_TELL` when required evidence is unavailable. The report always shows event,
fill, no-fill, unanswerable, target, stop, and time-exit counts with denominators. A quiet day is
`UNEXERCISED`, never a pass. The operator alone decides what happens after the study.

## Required proof before installation

Fixture tests must prove: a 45-second move fires only Momentum 60; a 25-second move fires both;
04:10:59 and 09:30:00 are rejected while 04:11:00 and 09:29:59 are accepted; `$0.99` is excluded
and `$1.00` included; entry uses the first strictly later print; a corrected/cancelled +40% print
cannot trigger, fill, or exit; SIP `t` wins when participant `y` disagrees; same-second stop beats
target; the time exit is after 600 seconds; paths start at detection and survive early exits;
200-second repeats are blocked while 301-second repeats pass; missing next prints become `NO_FILL`;
09:29:59 events finish through symbol-only subscriptions; gaps fail closed; no live route is
reachable; both cards remain separate; Polygon is retired; and ORB remains.

Each rule needs a mutation that makes its named test fail, followed by a full-suite comparison by
failure-name set. Before the unit is enabled, prove the Massive `T.*` entitlement and that the new
connection does not displace an existing production connection. Deployment is after market close
on explicit operator approval and does not restart any existing trading service.
