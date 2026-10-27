# MOMOLD1 Lane C review handoff

Authoring only. No tests, imports, replay, render, benchmark, provider, network,
SSH, SQL, orders, installation or guard calibration performed in this lane.
The integrator owns the durable queue, LATEST and execution slot; this file does
not update them or qualify the running 99fc full suite for MOMOLD1.

## Source boundary

Assigned draft a072acdda97740aaf8793740c3360de4b0b8d819 on ffa7b4db was
rebased cleanly onto reviewed 99fcbeaacb8e8d9a487ad5534a3862c488fcbdb1.
Rebased inherited draft: c52b7ee7. Additional source commits: 0683f8e4 and b016745b.
The inherited draft contains both source and test changes; follow-ups separate
source from tests/harness. The frozen 99fc worktree was not modified.

- Production preparation uses a dormant engine with strategy_windows={} for
  reference/session/feed lifecycle; legacy snapshot detection and raw ingest are
  skipped. Offline engine defaults and manually supplied legacy fixtures remain.
- Warning-only restart recovery and state publication use the warning engine.
  Legacy history stays readable and receives no synthetic recovery transitions.
- Existing snapshots still update freshness, connection and LIVE day-volume
  cache before the legacy-detection branch. LIVE handoff remains warning-only.
- Warning registrations retain paper:momentum, broker=none and Massive data;
  they create no broker account. The canonical page still reads actual LIVE
  ledger/owner proof and independent warning ledgers. Old URLs still redirect.
- Compact warning cards are PAPER references. They have no P&L metric at all;
  daily_pnl is not rendered. Warning positions are excluded from the compact
  header's aggregate position count. Generic API listening status reads the
  momentum-paper heartbeat, not strategy-engine/bar-decision activity. The
  existing 90-second UI freshness bound is retained; no guard files are changed.

## Explicit acceptance gap

Full historical warning parity cannot be established. In source, 16 legacy-owned
symbols can refuse a warning that the dormant legacy engine would admit.
Ownership also affects expiry and eviction protection. The authored SYNTH
counterexample records that distinction, rather than weakening an assertion to
claim global equivalence. No change to warning algorithms or caps is proposed.

Reviewer acceptance requested: accept only the following narrow evidence scope,
subject to actual execution and review of every difference. This is not a
blanket waiver, original-feed replay claim, installation approval or permission
to discard a differing warning. If original full-universe parity remains a hard
gate, MOMOLD1 stays blocked and the installed LIVE source remains unchanged.

1. Differential warning records through current service callbacks with legacy
   defaults versus dormant legacy evaluation, driven by 52 captured symbols.
   One-second aggregates and five-second snapshots are explicitly reconstructed.
2. Retained 98-event census and RUMBW event-only disclosure. RUMBW has no captured
   market data, so its DETECTED/NO_FILL history cannot establish data parity.
3. Separate SYNTH cap/expiry/re-admission/eviction/active-warning protection and
   T/Q replacement controls, including transient union <=16. These are controls,
   not retained October 20 observations.
4. Original A OHLC/revisions, sequence/epoch/drop counters, receipt/publication/
   callback clocks, raw subscription intervals/ACKs, original all-symbol order,
   original snapshots/day volumes and effective October 20 settings stay UNKNOWN.

## Authored harness

scripts/momold1_bounded_replay.py verifies all six export checksums and supplied
row/universe counts before modeling. It spools per-symbol event-time rows and
merges them with explicitly synthetic global order. CSV timestamp ties are kept
and counted, not represented as original receipt ordering. Quotes are counted
and hashed only; no LIVE execution/fill model is created. received_at is audited
as a retained raw field, never substituted for warning transport clocks.

The report includes exact reconstructed warning records for both modes, input
hashes, subscription trace hashes, covered/uncovered fields and symbols, retained
event identities, and RUMBW's separate raw events. Exact equality with no warning
detections returns INCONCLUSIVE and a nonzero exit. Any difference also returns
nonzero and preserves both sides. Matching this bounded reconstruction does not
qualify original market-data or transport parity, nor two independent source heads.

## Commands for main after the serial slot is released

Workdir: /private/tmp/mai-tai-momold1-20261021. Commands below are UNRUN.

```sh
env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u MAI_TAI_DATABASE_URL -u DATABASE_URL -u PGDSN -u PGPASSWORD PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -vv --tb=long tests/unit/test_momentum_paper_service.py tests/unit/test_momold1_retirement.py tests/unit/test_momold1_bounded_replay.py tests/unit/test_momentum_warning.py tests/unit/test_momentum_warning_followup.py tests/unit/test_momwarn1_no_data_expiry.py tests/unit/test_momwarn_warning_pipeline.py tests/unit/test_mompage4_live_first.py tests/unit/test_momwarn1_warning_page_metrics.py tests/unit/test_control_plane.py tests/unit/test_control_plane_listening_status.py
env -u MAI_TAI_DATABASE_URL -u DATABASE_URL -u PGDSN -u PGPASSWORD PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python scripts/momold1_bounded_replay.py --export /Users/velkris/.codex/task-queues/analysis/MOMOLD1-EXPORT-1020 --output /Users/velkris/.codex/task-queues/analysis/MOMOLD1-BOUNDED-52-RECONSTRUCTED-20261021.json
```

The harness refuses to overwrite an existing output; main must choose a fresh
receipt path if that name already exists. The focused list includes complete
modules, not filtered cases. It is a proposed focused command, not execution
authorization during the current full suite. Further qualification remains with
main/reviewer. Actual box guard/dry-run/prestart, GUARDPIN1, CPU comparison and
after-close restart are external and unverified here.
