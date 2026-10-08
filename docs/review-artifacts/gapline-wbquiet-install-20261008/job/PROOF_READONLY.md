# Oct 8 Read-Only Postinstall Proof

Current integration owner: Lane C. Earlier receipts below are historical,
not fresh install authorization. Latest scope is reviewed P1-only; no
WBQUIET hook/data observation contract remains in this collector.

## CLI Contract

Run on the box as root using the deployed application's Python and source path:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/trader/project-mai-tai/src \
  /home/trader/project-mai-tai/.venv/bin/python proof_readonly.py \
  baseline --output attempt/proof-before.json

# The runner waits at least 600 seconds after the latest of the four new starts.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/trader/project-mai-tai/src \
  /home/trader/project-mai-tai/.venv/bin/python proof_readonly.py \
  after --baseline attempt/proof-before.json --approved-sha "$APPROVED_SHA" \
  --line-enabled true --retirement attempt/orb-retirement.json \
  --restart-window attempt/v2-restart-window.json --output attempt/proof-after.json
```

`--restart-window` is optional. Its sole required field is `stop_started_utc`,
the runner's actual v2 stop/restart call boundary, not preparation time. Without
it the collector requires exactly one systemd `Stopping` record since baseline.
Both boundary sources are explicitly labelled; failure to establish one is
UNKNOWN, never a fabricated after-hours N/A. Outputs are exclusively created;
their parent attempt directory must already exist. Stdout is only verdict,
receipt exit code, path and SHA256. No output contains provider credentials or
an unfiltered process environment/error payload.

## Report Versus Gate

The `assessment.verdict` may be PASS, FAIL, or UNKNOWN. It is **not a trading
admission**. Partial bars/tape, unavailable DB metrics, incomplete sync
coverage, or scanner-validation limits remain explicit in the receipt but
return rc 0; they do not prevent preopen bookkeeping. rc 1 is a measured
identity/start failure, unexpected identity/flag change, eviction/memory
failure, or new-process ERROR/traceback. rc 2 is unreadable critical
identity/checkout/flag/Redis evidence or inability to construct a receipt.
The helper does not retry, wait, restart, authorize recovery, or call a broker.

Required postinstall process values are literal:

- GAP_LINE_CARRY=true on v2.
- LINE_CHART_RESTORATION=true on OMS and v2 under the latest explicit GO.
- FALSE_FLIP=true on OMS and v2.
- ATR_REPRICE_HANDOFF=false on OMS and v2.
- All other explicitly whitelisted live process flags retain their baseline values.

Checkout proof means clean exact SHA, newly started identities and process cwd;
it is not an in-memory bytecode hash. No false loaded-code attestation is made.
Paper/guard may transition to clean inactive only with the exact original
invocations and a root-owned guard `scheduled_session_close` receipt matching
their stop interval. Without it, the transition is not claimed unchanged.

## Measurement Bounds And Limits

- Thirteen service identities, four restarted log cursors and process environments (262 KiB each).
- Redis: exact five-owner population plus marker/checkpoint; pre-bound hash 100 KiB, paper cap 16;
  isolated state read one event per request, at most 80 requests, 262 KiB per event, watchlist <=128.
- Log inventory <=256 entries; only files modified since cursor or matching its inode are considered;
  <=24 relevant files, <=32 MB postcursor data, baseline offset <=512 MB.
  Rename and gzip rotation locate the baseline byte anchor. Cursor loss, truncation,
  or ambiguity is UNKNOWN, not silently skipped. Every range carries actual path,
  inode, byte offsets and hash. Raw log/error/provider payloads are not serialized.
- New PID journal records and post-start timestamped file logs are independently counted.
  A journal with zero entries is labelled as such, not treated as proof of file-log silence.
- Complete OMS sync-pass IDs, duplicates and boundary partial passes are listed.
  Sync duration max/p95 is measured from actual completed passes. No shadow-log
  hooks, reader observations or future study coverage are required or certified.
- SQL is read-only, connection timeout 5 s, statement timeout 5 s, lock timeout 500 ms;
  bar read <=4096 rows around the actual restart. DB transaction rate includes the whole
  database and collector reads, not OMS-only attribution. Stats resets invalidate the rate.
- Persisted missing minutes are listed per pre-stop watched name. Lack of an observed
  postrestart bar stays UNKNOWN; no tape-traded or zero-hole claim is inferred. N/A is
  restricted to a same-day weekday restart entirely after 20:00 ET, never 16:00-20:00.
- Query and grading wall times are separate measurements. Strategy GAPLINE repair cost
  remains UNMEASURED because this collector neither runs nor times that repair.
- Scanner receipt proves only fresh heartbeat/healthy main loop. Tomorrow's rule 9b
  scanner acceptance is explicitly UNMEASURED.

## Verification

Focused tests: **70 passed**; Ruff clean. Six assertion controls RED on independent
in-memory source mutations: required flags, evictions, untouched identity,
file ERROR/traceback, journal error, and retained flags. No source file was
mutated on disk. Synthetic operating-system/receipt
fixtures cover rotation/gzip, lost cursor, duplicated observations, owner
population, flag drift, evictions, guard close, DB counter reset, after-hours
continuity and report-only limits. No trading print/bar replay is invented.

Read-only SSH rehearsal, **2026-10-08 12:22:42.718059 UTC / 08:22:42 ET**:
baseline captured in **3.20648 s**, collection errors `{}`. Actual box SHA
`d244f602491e5d316a05670b205315466aed2a4d`; OMS `1408231`, v2 `1895743`,
strategy `1408242`. The earlier 12:17 capture measured owners five + marker,
evictions 0, Redis 807,912,576 bytes, watched AIXI/DKI/FLYE, scanner healthy.
The rehearsal also read real bounded postcursor ranges and the three unit
journals without writing any box file. Journals contained zero entries for
those PIDs during this two-second rehearsal; this is **not** a ten-minute
postinstall receipt and does not certify WBQUIET emission before its install.
