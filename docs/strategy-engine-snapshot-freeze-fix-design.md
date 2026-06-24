# strategy-engine snapshot-freeze fix — design (#350 follow-up)

**Status:** design only, no code. Touches `services/strategy_engine_app.py` (a Pre-Merge-
Regression-Check hot file) → design-first → flag-gated PR → hard validation → attended
deploy. All three changes default OFF (byte-identical to today when off).

## Root cause (measured, not inferred)
py-spy close-window capture 2026-06-23 (185,339 samples, 11 segments, strategy PID
2415361): **JSON encode `iterencode` = ~52% of CPU, decode `raw_decode` = ~20% → ~72%**,
synchronous on the asyncio loop. Hot path, per Redis stream message:
```
_run_main_loop_iteration → _handle_stream_message
  → _publish_strategy_state_snapshot            (strategy_engine_app.py:6958)
    → _persist_scanner_snapshots                 (:9222)
      → _replace_dashboard_snapshot              (:10089)
          safe_payload = json.loads(json.dumps(payload, default=str))   # encode + decode (sanitize)
          session.add(DashboardSnapshot(payload=safe_payload)); commit() # psycopg re-encodes JSONB (2nd encode)
```
So **per persist = 2 encodes + 1 decode** of the (large) dashboard/scanner snapshot, on the
loop, on **every** stream message (call sites 6677/6713/6715/6736/6968 + per-iteration
6314/6321). At the close, message volume spikes → loop saturation → the measured 60–76s
snapshot-batch gaps.

Note: #350's existing offload (`persist_offload_enabled`, default off; flushes per-bot
buffered **bar** writes at :4648) does **not** cover this dashboard-snapshot path.

---

## Fix — priority order (ship & validate one at a time)

### (1) Throttle the snapshot persist — highest leverage, lowest risk
**Change.** The expensive DB persist (`_persist_scanner_snapshots → _replace_dashboard_snapshot`)
fires on every `_publish_strategy_state_snapshot`. Debounce it: persist at most once per
`strategy_snapshot_persist_throttle_secs` (e.g. 1s), or only when the snapshot content
changed (cheap dirty-flag / content hash), coalescing the high-frequency calls. The
lightweight Redis live-state publish can stay frequent; only the JSON+commit is throttled.
**Why highest leverage.** Cost is per-message × snapshot-size; at the close that's dozens of
full encode+persists/sec. Coalescing to ~1/sec cuts the work by the message rate (≈10–50×) —
likely removes most of the freeze on its own.
**Risk.** Persisted snapshot lags real state by up to N seconds. Mitigation: keep N=1s (a
human-facing dashboard view — 1s lag is invisible); the live Redis state stream that bots/
control consume is separate and unaffected.
**Flag-off / no-loss.** Gate `strategy_snapshot_persist_throttle_secs` default **0 = off =
current per-message behavior (byte-identical)**. When >0, **trailing-edge** debounce: always
persist the latest snapshot, only coalesce intermediates; force-flush on shutdown / day-roll.
No snapshot lost — only duplicate intermediates dropped.

### (2) Offload the encode + DB write off the event loop
**Change.** Move `_replace_dashboard_snapshot`'s encode+commit into `asyncio.to_thread` (or a
single dedicated background writer task fed by a 1-slot "latest" mailbox). Extend the existing
#350 offload pattern to cover the dashboard-snapshot path (currently bar-only).
**Why.** Even at 1/sec, a large-snapshot encode+commit can be 50–200ms — a per-second loop
stall. Offloading removes it from the loop entirely.
**Risk.** Thread-safety: each write must use its **own** session (`_replace_dashboard_snapshot`
already opens a fresh `session_factory()` session — safe per-thread). Ordering: ensure
last-wins (pairs with the throttle — coalesce to latest). Backpressure: bound the mailbox to 1
(keep latest, drop stale intermediates).
**Flag-off / no-loss.** Reuse/extend `strategy_persist_offload_enabled` default **off →
synchronous (current)**. When on, the writer **flushes on shutdown/roll**; a failed offload
write logs + falls back to a synchronous retry (never a silent drop).

### (3) Encode once — kill the sanitize round-trip + double-dump
**Change.** Replace `safe_payload = json.loads(json.dumps(payload, default=str))` (a full
encode **and** full decode purely to sanitize non-JSON types) so the snapshot is serialized
**once**: build it JSON-safe up front (or apply a single custom `default=` encoder), and write
the JSON column without a second psycopg re-encode (serialize once, store via the appropriate
JSONB/text path; ideally reuse the same encoded payload for the Redis publish and the DB write).
**Why.** That round-trip is literally the `iterencode` **and** `raw_decode` hotspots — a large
fraction of the measured cost — and psycopg then encodes a third time. One encode replaces
encode+decode+encode.
**Risk.** Correctness of the sanitize replacement (datetime/Decimal must still be handled by
the single encoder); the JSONB column must still round-trip on read. Lowest leverage of the
three but removes a structural inefficiency. Needs a characterization test proving persisted
content is byte-equivalent.
**Flag-off / no-loss.** Gate `strategy_snapshot_single_encode` default **off → current
round-trip**. Characterization: persisted snapshot JSON is content-equivalent before/after.
Same data, fewer encodes — no loss.

---

## Cross-cutting
- **Sequencing:** ship **(1) throttle** first → re-run the py-spy capture at a close → if
  residual stall, add **(2) offload** → then **(3) encode-once**. Each is its own flag +
  validation. **Do not ship all three at once** (isolate cause/effect on a hot file).
- **Validation gates (hard):** (a) **flag-off byte-identical** — characterization test: same
  snapshot content + same persist cadence as today; (b) **flag-on at a close** — re-run the
  #350 py-spy capture; success = `iterencode` share collapses and **snapshot-batch gaps < 50s**
  (the freeze threshold); (c) **no snapshot loss** — latest snapshot always persisted, dashboard
  renders, DB retention/row-count unchanged; (d) the live Redis state stream cadence unaffected.
- **Deploy:** attended, capture PIDs, validate at a real close (the freeze only manifests at the
  close), with the #350 capture re-armed to confirm. Per `[[project-mai-tai-multi-agent-deploy-rules]]`.
- **Hot-file discipline:** Pre-Merge Regression Check (callgraph audit of `_publish_strategy_
  state_snapshot` consumers), no net behavior change with flags off.
