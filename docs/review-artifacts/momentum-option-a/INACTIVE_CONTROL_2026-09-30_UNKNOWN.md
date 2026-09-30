# 2026-09-30 inactive control: UNKNOWN

The required 07:00:00-09:40:00 ET control window was not captured. No
read-only sampler or artifact was present on the production box at 06:59:12 ET.
An ad-hoc 1 Hz load sampler was launched, but its first actual sample was
07:00:05.408 ET. It was stopped at 07:00:55 ET after this gap was confirmed.
Its 56 rows cover only 51 distinct seconds, against 9,600 expected seconds.
The first second contains catch-up duplicates. This trace is a diagnostic
artifact, not a control baseline; do not use it to set or approve thresholds.

Production checkout was `3389090a7d30bdc88736a53211d968f4c82f0288`.
The process snapshot at approximately 07:02 ET found market-data PID 2202865,
momentum-paper PID 2704889, v2 PID 1664453, and OMS PID 1663656. The latest
v2 watchlist update before 07:00 was 06:29:15 ET with `LGHL,NCI`; no intervening
watchlist-update line was found, so that is an inference, not a direct 07:00
snapshot. At 07:04:04 ET, v2 logged `LGHL,NCI,TGE`.

The old momentum-paper process was still attempting its own `T.*` subscription
every approximately 15 minutes. Its log reached `consecutive_1008=85` at
06:46:34 ET and made another probe at 07:01:35 ET. The protocol's control
requires Momentum to be disconnected from the **gateway**, not for the legacy
paper process to be stopped. Any complete control with this process running
must include its intermittent work in measured load, disclose the probes, and
count its 1008s separately from gateway 1008s. It cannot be described as a
Momentum-process-inactive control. The operator reported four gateway 1008
events before 05:30 ET; the current gateway log exposes three 1008 stack-trace
lines without event timestamps, so that count cannot be independently
reconciled from this file. These pre-window events would be recorded as
context, not counted as in-window 1008s.

Raw evidence on the box:

- `/home/trader/after-hours/2026-09-30/option-a-control-load-1hz.log`
- `/home/trader/after-hours/2026-09-30/option-a-control-load-1hz.err`
- `/var/log/project-mai-tai/schwab-1m-v2.log`
- `/var/log/project-mai-tai/oms.log`
- `/var/log/project-mai-tai/market-data.log`
- `/var/log/project-mai-tai/momentum-paper.log`

Coverage: load has 51/9,600 distinct expected seconds and a missing start.
No per-minute v2 probe, OMS eligible-intent/refusal, gateway heartbeat,
snapshot cadence, or timestamped gateway 1008 control series was captured as
required. Their coverage is UNKNOWN, not zero. No empirical matching-hour
thresholds are proposed. Collect a new complete, clean inactive morning and
obtain independent review before any Option A deployment or first-session
comparison.
