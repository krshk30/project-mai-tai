# Option A first-session slowdown protocol

Pre-registered 2026-09-29 before the 07:00-09:40 ET inactive-Momentum
baseline. This is a measurement and paper-service stop plan, not permission
to deploy or restart the live gateway. The reviewer must approve the measured
baseline and thresholds before the first Option A session. No threshold may be
tuned after that session's outcomes are read.

## Control and observation windows

- Control: the same clock minutes, 07:00-09:40 ET, on 2026-09-29 while Option A
  is not deployed and Momentum is disconnected from the gateway. Record exact
  service PIDs/SHAs, watchlist, missing minutes, and raw paths. If this control
  is not clean or complete, collect another inactive control; do not infer a
  baseline from a partial morning.
- Treatment: the first full 07:00-09:40 ET session after an independently
  reviewed, exact-SHA operator-approved Option A deployment. Sample load once
  per second, and evaluate the trading signals once per minute. The sampler
  reads existing events/logs; it does not replay data or modify services.
- Use America/New_York session boundaries, timestamped records and the same
  parsing/denominators in control and treatment. Report counts and missing
  coverage for every signal. A missing/unreadable signal is UNKNOWN, never a
  pass. The control's matching clock-hour bin is the reference for each
  treatment minute; use the whole-control value if a bin has fewer than 20
  valid minutes and label that fallback.

## Trading slowdown signals and stop rules

For each one-minute evaluation, a rolling five-minute window includes its
current minute and the previous four. `p95` is the nearest-rank value. A
consecutive-minute condition resets whenever the required evidence is missing.
The relative bound is strict `>`; equality is not a stop.

| Signal | Measurement | Stop threshold |
| --- | --- | --- |
| v2 live bar processing | For each watched symbol, `[V2-ATR-PROBE]` log receipt time minus the one-minute bar close (`ts_ms + 60,000`). Exclude documented seed/replay/warmup bars, not late live bars. Require at least three live probes per rolling window; report eligible probes / expected active bars per symbol. | A symbol's rolling p95 exceeds both 2 times the matching control p95 and 5 seconds for 5 consecutive one-minute evaluations. A symbol with no comparable control uses the matching-hour pooled watched-symbol p95, explicitly labeled. |
| OMS quote path | Published quote `produced_at` to read-only observer receipt age, p95, is a **proxy**, not the age of the exact ask in OMS memory. Report it separately from direct `[OMS-ABANDON-INTENT] ... NO_FRESH_QUOTE` and `no valid OMS market snapshot` counts, with the number of eligible intents. | Direct refusals stop if their rolling count is at least 3 and more than 2 times the matching control count for 2 consecutive evaluations. Proxy age alone is a warning until exact OMS ask-age instrumentation is separately approved; proxy p95 over both 2 times control and 2 seconds for 5 consecutive evaluations must be reported. |
| gateway heartbeat | Latest heartbeat status and age, measured by a read-only stream observer. Record control maximum healthy age and unhealthy count. | A non-healthy status in 2 consecutive samples, or no heartbeat for longer than both 2 times the control maximum age and 30 seconds. |
| snapshot cadence | Interarrival of distinct `snapshot_batch` producer timestamps; nominal interval 5 seconds. Report received / expected batches and rolling p95. | Rolling p95 exceeds both 2 times control p95 and 10 seconds for 5 consecutive evaluations, with at least 20 intervals in each rolling window. |
| Massive 1008 | New gateway 1008 disconnects, counted from gateway logs, with control count and exact timestamps. | Any new 1008 during treatment above the matching control count; stop on the first new excess event. |

An independent stop trigger is sufficient. The monitor must preserve the raw
evidence, stop **only** `project-mai-tai-momentum-paper.service`, journal the
trigger and send a low-priority operator page. Never restart or stop the
gateway, v2, OMS or strategy as part of this rule. If monitoring is blind, fail
closed for the paper experiment and ask for an operator decision; do not call
the session healthy. Live positions and live service safety take priority over
finishing the paper observation.

One-minute system load is sampled at 1 Hz with exact start/end and missing
sample count. `load1 > 3.5` is a **warning**, not a stop or a protocol void. It
must be logged with process CPU contributors and the simultaneous trading
signals; it is not evidence of trading harm by itself.

## Warm-up and attribution

Momentum may cause the gateway's **existing** historical warm-up for a symbol
new to the union of all consumers. Do not isolate Momentum or change the
gateway's warm-up behavior. The cap stays at 16 Momentum-owned symbols.
Report per session the count of Momentum replace events, symbols newly added
to the gateway union, and actual historical REST attempts attributable to
those additions (30-second and 60-second calls separately). Distinguish
gateway-startup warm-up for already-owned symbols and calls caused by other
consumers. If the existing evidence cannot distinguish actual REST calls from
fetch invocations or cooldown skips, report a bounded estimate as such, not
an exact call count. Retain raw event and gateway log paths in the journal and
heartbeat report.

## Decision record

The first-session outcome is `STOPPED` with the exact trigger, `OBSERVED` with
complete required coverage and no stop, or `UNKNOWN` when evidence is missing.
None of these is a live-trading performance or Momentum P&L verdict. A stop
does not authorize a gateway restart, a live-trading change or a new test
without review and operator approval.
