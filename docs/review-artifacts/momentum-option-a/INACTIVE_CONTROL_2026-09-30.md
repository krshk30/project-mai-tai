# Option A inactive-morning control, 2026-09-30

## Call and provenance

The operator accepted this 07:00-09:40 ET control **with the gaps below**, rather
than classifying the whole control UNKNOWN. This is a baseline for a proposed
paper-service slowdown monitor, not an Option A deployment, a live-trading
performance verdict, or evidence that the gateway has headroom. The original
protocol was committed before the session at `53ee02fc`; the operator moved the
control to September 30 and accepted incomplete warning-only load coverage.
The parser was committed before outcomes were calculated. After the first
completed-window read exposed seed/replay probes, its marker-based exclusion
was added and tested; no stop multiplier or minimum was changed.

The box checkout was `3389090a7d30bdc88736a53211d968f4c82f0288`, clean.
At start and end, gateway PID 2202865, old Momentum-paper PID 2704889,
v2 PID 1664453 and OMS PID 1663656 were unchanged, each with `NRestarts=0`.
The gateway and old paper processes predate this checkout; their exact loaded
source SHAs are not provable from the current checkout. Option A was not
installed. The old paper service remained disconnected from the gateway but
kept probing its own `T.*` Massive socket.

## Coverage and measured references

All times below are ET; the control has 160 one-minute evaluations. A strict
`>` applies to the numerical bounds, and missing treatment evidence resets
the consecutive-minute counter. Control references are nearest-rank p95 of
the underlying observations, not a p95 of rolling p95 values.

| Signal | Covered / expected | Control reference and proposed treatment threshold | Gap |
| --- | --- | --- | --- |
| One-minute load, warning only | 6,875 distinct seconds / 9,600 (71.6%). Early sampler: 07:00:05.408-07:00:55.040, 51 seconds; main trace: 07:29:14.654-09:39:59.250, 6,824 seconds / 7,846 in that span. | Maximum 5.06; 219 sampled seconds above 3.5. **Warning, never automatic stop.** | First five seconds, most of 07:01-07:29, and 1,022 seconds within the main trace missing. CPU contributors at the 219 high-load seconds were not captured. |
| Snapshot batches | 1,185 distinct IDs / 1,920 nominal whole-control batches; 07:29:14.531-09:39:56.534. Within the observed span, 1,185 / 1,570 nominal; 1,184 measured intervals. | Overall interval p95 7.331 s, max 11.846 s. Matching-hour p95 / stop: 07:00 7.038 / **>14.076 s**; 08:00 7.084 / **>14.168 s**; 09:00 7.553 / **>15.106 s**. Require at least 20 intervals in each rolling five-minute window and five consecutive exceedances. | No snapshot observer before 07:29. All three observed hour bins nevertheless have at least 20 valid minutes (31/60/40); no whole-control fallback is invoked. |
| Gateway heartbeat | 638 unique events / 640 nominal, 07:00:12.538-09:39:53.367; events in all 160 minutes; 638/638 `healthy`. | Healthy observer-age max 15.410 s (6,824 observer samples from the main trace); stop after two consecutive non-healthy samples **or** age **>30.819 s**. Interarrival p95 15.229 s, max 16.220 s. | Observer-age maximum was sampled only from 07:29 onward, although heartbeat events cover the whole control. |
| OMS quote refusals | Timestamped OMS log from 07:00:12.355-09:39:46.548, 2,919 records. Zero direct `NO_FRESH_QUOTE` or `no valid OMS market snapshot` refusals. | A treatment rolling-five-minute direct-refusal count must be **at least 3 and >0** for two consecutive evaluations. | Seven live v2 intents existed: five buy-open/sell-close candidates and two cancels. Thus 0/5 is an upper-bound quote-sensitive denominator, **not** a proven exact eligible-intent denominator. Exact OMS ask age and the published-quote/observer-receipt proxy were not captured. |
| v2 ATR-probe lag | 267 marker-screened, unique live-candidate probes; 309 marker-coincident seed/replay/warmup probes excluded by same-symbol receipt second. Known watched minutes: LGHL 156, NCI 88, VBIO 49, TGE 9. | Per-symbol, per-hour bounds below. Stop only if rolling-five-minute p95 (at least three probes) exceeds the bound for **five consecutive evaluations**. | Log omits an explicit phase on each probe. VBIO/TGE bursts are demonstrably seed; an isolated live probe sharing a marker second could also be conservatively excluded, while unmarked replay cannot be excluded with certainty. Active-watch minutes are an upper bound on bars expected when prints occur. |
| Gateway Massive 1008 | Four `received 1008` lines in the current gateway log; two are attributable to the control at about 07:01 and 08:32 ET, none to the 09:00 hour. | The control count is diagnostic. In the first Option A treatment session, **any new gateway 1008 stops the paper service**; no matching-hour subtraction. | Gateway lines lack embedded timestamps. Attribution is reconstructed from fleet-health pages at 05:35, 07:05 and 08:35 ET, the gateway log mtime at 12:32:18 UTC, and old paper cool-offs at 09:31:03, 11:01:43 and 12:32:24 UTC. The event times are approximate; co-occurrence is not causal proof. |

The old **Momentum-paper** log, a different socket, records **11** timestamped
1008 cool-offs during 07:00-09:40 ET (07:01:43 through 09:32:52), roughly one
probe per 15 minutes; its `consecutive_1008` rose from 86 to 96. Those are not
gateway 1008s and are never added to the gateway count. Co-occurrence does not
prove that the old paper socket caused gateway disconnects.
Across today's 23 old `T.*` probes, four coincided with a gateway 1008;
that association is diagnostic, not proof of a shared-socket cause. The
fleet-health pages lag the inferred events by minutes and are not themselves
the websocket close timestamps. The third correlated gateway event is anchored
by the 12:32:18 UTC gateway log mtime and the 12:32:24 UTC paper cool-off;
the other control event is reconstructed near 07:01 ET from the 07:05 page and
11:01:43 UTC paper cool-off. The 05:35 page corresponds to a pre-control
event near 05:31 ET; the fourth event is outside the two identified control
times and is not assigned a more precise clock minute here.

### v2 symbol references

The denominator after the first marker-screened live probe is a **watched-minute
upper bound**, not a claim that a trade printed every minute. The first live
probe for LGHL/NCI closed at 07:09 ET, for VBIO at 08:12 ET. TGE had only
documented seed probes during its nine known watched minutes, so it has no
symbol-specific control; use the explicitly labeled pooled reference if it is
watched in treatment. For a symbol with fewer than 20 valid probe minutes in
a matching hour, use that symbol's whole-control reference; if it has none,
use the pooled watched-symbol reference.

| Symbol | Marker-screened probes / watched minutes after first probe | Overall lag p95 / max | 07:00 threshold | 08:00 threshold | 09:00 threshold |
| --- | ---: | ---: | ---: | ---: | ---: |
| LGHL | 147 / 147 | 3.089 / 3.317 s | >6.018 s | >6.288 s | >6.086 s |
| NCI | 79 / 79 | 3.204 / 3.446 s | >6.136 s | >6.632 s | >6.408 s (symbol whole-control fallback) |
| VBIO | 41 / 46 | 3.111 / 3.246 s | >6.222 s (symbol fallback) | >6.222 s (symbol fallback) | >6.222 s |
| TGE | 0 / 9 known watched minutes; no first live probe | n/a | >6.096 s (pooled) | >6.408 s (pooled) | >6.086 s (pooled) |

The first parser pass incorrectly made the VBIO/TGE seed bursts look 35-45
minutes late. The source log shows `[V2-DB-SEED-GAP]`, a rapid burst of
`[V2-ATR-PROBE]` for the same symbol, then `db-seed: ... hydrated` in that same
receipt second. The corrected parser excludes those documented bursts before
deduplication, and keeps late unmarked probes rather than deleting them by an
arbitrary age cutoff. The 309 exclusions and zero remaining duplicate probes
are reported so the selection is auditable.

## Treatment decision and raw evidence

These values are **proposed for independent review**, not an authorization to
deploy. The numerical v2, snapshot and heartbeat rules above use the
pre-registered 2x relative bounds and 5 s / 10 s / 30 s absolute floors.
The 3-refusal, two-evaluation OMS rule is unchanged. Load above 3.5 remains a
warning. The quote-age proxy is warning-only and was not measured here.

The control is accepted with gaps as ruled; individual observability gaps
remain UNKNOWN. The recovered 1008 association supplies a control count but
not direct event timestamps. The first-session amendment in
`FIRST_SESSION_PROTOCOL.md` therefore requires a read-only, one-second gateway
log byte-offset/size/mtime capture and stops on **any** new gateway 1008.
Under the fail-closed monitor rule, **do not call a treatment session OBSERVED
or deploy on this report alone**: independent review must approve that capture
and the unavailable quote-age coverage. No service was restarted,
stopped, reconfigured, or installed for this control.

Raw sources:

- Load/snapshot trace: `/private/tmp/claude-502/-Users-velkris/9b59bc17-8b7a-4dde-be74-ff74d9edd1d0/scratchpad/control_raw_0930_claude.txt`
- Early load sampler: `/home/trader/after-hours/2026-09-30/option-a-control-load-1hz.log` on the box.
- Heartbeat dumps: `/private/tmp/claude-502/-Users-velkris/9b59bc17-8b7a-4dde-be74-ff74d9edd1d0/scratchpad/heartbeats_0930_*.txt`, including `heartbeats_0930_dump_final.txt`.
- v2, OMS, gateway and old paper logs on the box: `/var/log/project-mai-tai/schwab-1m-v2.log`, `/var/log/project-mai-tai/oms.log`, `/var/log/project-mai-tai/market-data.log`, `/var/log/project-mai-tai/momentum-paper.log`.
- Gateway 1008 attribution: fleet-health page times (05:35, 07:05, 08:35 ET), gateway log mtime 12:32:18 UTC, and old paper probe/cool-off timestamps in the logs above; the correlation is reviewer-supplied and should be preserved with the treatment journal.
- OMS candidate denominator: read-only `trade_intents` join to `strategies` and `broker_accounts`, created 11:00-13:40 UTC, accounts `live:orb` and `live:schwab_1m_v2`.
- Reproducible parser and synthetic marker/coverage tests: `scripts/option_a_inactive_control.py`, `tests/unit/test_option_a_inactive_control.py`.
