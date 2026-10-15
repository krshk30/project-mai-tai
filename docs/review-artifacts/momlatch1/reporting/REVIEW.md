# MOMLATCH1 Reporting / Page / Normal Warning Incident

Sole writer Popper, isolated f9b71cc0 lineage. Explicit HUMAN NOW preempted C;
checkpoint preserved. H823/Rb926 untouched. Source scope ONLY control_plane.py
and new momwarn_pause_incidents.py; gateway/provider/watchdog/policy/paperapp
unchanged. Local build, not deployment or restart approval.

## Main Integration API

```python
store = PauseIncidentStore(timed_session_factory)
reporter = MomwarnPauseIncidentReporter(store, wait_budget_seconds=2)
# Existing gateway 10s metrics TASK, AFTER publishing its metric:
await reporter.observe(full_metrics, now_ns=time.time_ns())
```

full_metrics.pause_state is Tesla WarningPausePolicy.metrics() unchanged,
except main exports at most8 recent history rows within the16KiB metric cap.
Main publishes paired observed_ns / observed_monotonic_ns. No policy import,
forked threshold/classification or fabricated strategy/account/position state.
observe returns QUEUED/NO_ALERT/UNKNOWN promptly; no SQL await on the metrics
caller or quote path. One offloop SQL worker and one latest pending sample;
timeout keeps SAME worker until completion, never launches orphan SQL threads.
Timed connect/pool/statement session factory REQUIRED from main; outer async
wait alone cannot cancel DB work. Failure is logged and retryable, never an A
pause/re-arm instruction. close() is bounded shutdown only, not per sample.

Normal SystemIncident rows: service_name market-data-gateway, severity warning,
account_name paper:momentum, strategy_codes momentum_30s_warn/momentum_60s_warn,
source momwarn_pause_health. No symbol, positions, quantities, broker account,
trade intent, Telegram or trading pager. Uses existing ORM incident table and
control-plane incident surface; NOT reconciler critical broker-alert cron.
Consume policy alert_reasons; actual pause_flapping and pause_over_10min are
policy-owned. Flapping dedup per gateway/alert/observed UTC hour; long-pause
dedup per gateway/alert/actual monotonic start. No start => UNKNOWN/skip, not
invented identity. Operator closure remains closed; acknowledged stays so.

## Page And Evidence

Existing bounded64-row,250ms,16KiB read retained. Missing/stale>20s/future or
conflicting GUID/generation is UNKNOWN, not zero or healthy. New page shows
state, reason/class, clean streak/required reads, reason segments, actual
episode/wire/interrupt counts, last-hour lower bounds, and last8 histories with
start/end/duration. History is monotonic ONLY. Optional UTC estimate uses
observed UTC-minus-monotonic offset and is labelled NON-EXACT; missing paired
clock/domain remains UNKNOWN. Text/JSON escaped; no Redis writes/control calls.

26 functional controls PASS; Ruff PASS. Direct actual Tesla policy source
contract tests selected with MOMLATCH_POLICY_SOURCE, not a copied policy.
Snapshot compatibility includes exact >600s boundary and >6 actual pauses/hour
flapping, normal warning rows, readable page, private SQLite only. Policy
source read SHA4343bbd79875564bffcd82cf34e516cb9316834f94ab584bba37b20c62c585f0;
other controls prove offloop SQL, two-row threshold dedup, operator closure,
stale/invalid/conflicting evidence, coalescing, wait timeout single-worker,
DB failure/retry and explicit paired-clock estimates. No native runtime claim.

## Pending Main Gates

Main wires helper into existing10s metrics task postpublish; no call here.
Policy/gateway/source composition, health/scanner/ticks/LGHL/performance and
reviewed runner remain main/Tesla/Boyle work. Control-plane display requires an
EXTRA service restart: reviewer scope decision required; not added to the
approved gateway+guard+paper runner. Display can wait, incident helper can
integrate separately after review. No production/provider/API/realDB/rawSQL,
benchmark/queue/status writes, restart or deployment in this lane.
