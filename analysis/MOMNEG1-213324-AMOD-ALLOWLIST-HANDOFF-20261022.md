# Approved AMOD exact-delta acceptance successor

Task MOMNEG1-213324-AMOD-ALLOWLIST-ACCEPTANCE-20261022; Cicero sole author.
Authority: Claude TOP 2026-10-22 21:33:24, read directly. SOURCE BUILD ONLY.
Base cac98b036e0663911c7582b7f7af112446c99623.
WT /private/tmp/mai-tai-momneg1-amod-allowlist-20261022.
Branch codex/momneg1-amod-allowlist-20261022.
Separate frozen fixture head f2ec4056 is untouched and NOT a prerequisite.

## Exact scope / mechanics

No production change. One proof helper, one existing acceptance test, one new
nine-case unit module and this handoff. Original failed XML/raw evidence is untouched.
Legacy `equal` still reports actual whole-file inequality. Acceptance now requires
`approved`: pinned ORIGINAL PRE handoff and admission bytes, exact reconstruction
of POST by the literal approved insertion and parsed-price reuse, and byte-identical
admission. Every other byte, including the entire producer/_emit, must remain equal.
No new POST digest substitutes for reviewing the exact delta.

The actual AMOD acceptance uses distinct real source roots and fresh `python -I`
processes (-B explicitly prevents bytecode writes even though -I ignores Python
environment variables), not two current LiveHandoff instances. PRE files must equal git PRE_REF
99fcbeaacb8e8d9a487ad5534a3862c488fcbdb1 before launching workers. Each worker imports
the actual LiveHandoff and EpisodeLedger from its root, verifies class file paths,
and reports actual root, HEAD, PID and source hashes. The same source-pinned service
offer method is extracted from each root. Method hashes/AST/age constant must match.

Both workers separately verify every original manifest/CSV/gzip/log pin; exactly nine
raw event rows and exactly two selected warning DETECTED rows. No packet data changes.
The result retains the two raw CSV dictionaries, entire original payload serializations,
forwarded offer-call serialization/hashes, actual route callback evidence, actual
Signal serialization before real claim, exact decisions, counters and empty queue/ledger
for all five original cache contexts. The claim wrapper records then calls the actual
root's original claim, never changes its return or permission. NoRedisIO refuses any
Redis access. No worker _emit or native order is run. Full source allowlist proves
unchanged producer serialization code; no emitted-event claim for refused AMOD rows.

Existing unknown limits remain: historical cache ages, owner book and exact callback
clock are not retained. The cache ages and empty/restored-ready ledger are CONTROLLED,
not native historical reconstruction. No AMOD fill/PnL/native acceptance claim.

Nine new source controls: exact allowed delta + preserved legacy inequality; six
nonallowed mutations (guard, producer, unrelated comment, admission, PRE, absent guard);
same-root refusal; wrong-PRE refusal before worker/input read. All UNRUN.

## Raw approved delta

This is the inspected raw Git delta from PRE_REF to the base handoff. The runtime
report includes its independently generated raw_allowlisted_diff. Admission has no diff.

```diff
@@ -83,6 +83,23 @@ class LiveHandoff:
             return "not_live_signal"
         try:
             detect = record.payload["detect"]
+            signal_price = Decimal(str(detect["price"]))
+            # The paper engine seals its in-memory prior close into this record.
+            # Never fetch a replacement or let this live-only rule alter paper.
+            raw_prior_close = record.payload.get("prior_close")
+            try:
+                prior_close = Decimal(str(raw_prior_close))
+            except (ValueError, TypeError, ArithmeticError):
+                prior_close = None
+            if (prior_close is None or not prior_close.is_finite() or prior_close <= 0
+                    or (signal_price.is_finite() and signal_price < prior_close)):
+                reason = "momneg1_prior_close_unknown" if (
+                    prior_close is None or not prior_close.is_finite() or prior_close <= 0
+                ) else "momneg1_down_day"
+                self.counts[reason] += 1
+                logger.info("[MOMNEG1-REFUSED] symbol=%s price=%s prior_close=%s reason=%s",
+                            record.symbol, signal_price, raw_prior_close, reason)
+                return reason
             reference = record.payload["reference"]
             end = int(detect.get("end_ms", detect.get("sip_ts_ms")))
             start = int(reference.get("end_ms", reference.get("sip_ts_ms")))
@@ -96,7 +113,7 @@ class LiveHandoff:
                 "is_us": getattr(decision, "is_us", None), "cik": getattr(decision, "cik", None),
                 "as_of": getattr(decision, "as_of", None).isoformat() if getattr(decision, "as_of", None) else None}
             signal = Signal(str(record.session_date), record.symbol, record.logical_id,
-                            start, end, Decimal(str(detect["price"])), observed, day_volume,
+                            start, end, signal_price, observed, day_volume,
                             str(decision.broker), str(decision.reason), json.dumps(evidence, sort_keys=True))
             seen = (signal.session_date, signal.symbol, signal.source_id) in self.ledger._sources
             episode, fresh = self.ledger.claim(signal, now_ns=now_ns)
```

## Proposed FIRST bounded actual claim (main only, AFTER review)

Main prepares a NEW detached read-only PRE checkout; do not adopt a foreign tree:

```sh
git -C /Users/velkris/Projects/project-mai-tai worktree add --detach /private/tmp/momneg1-amod-pre99fc-213324-20261022 99fcbeaacb8e8d9a487ad5534a3862c488fcbdb1
```

From this successor WT, FIRST nine new controls plus ONE changed actual retained
acceptance, ten expected parameter cases total (actual collection/result authoritative):

```sh
env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u DATABASE_URL -u MAI_TAI_DATABASE_URL PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. MAI_TAI_MOMOLD1_PRE_ROOT=/private/tmp/momneg1-amod-pre99fc-213324-20261022 /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q -s --tb=short -o addopts= -p no:cacheprovider tests/unit/test_momneg1_amod_allowlist.py tests/acceptance/test_momold1_retained_acceptance.py::test_actual_amod_1021_0815_offer_bytes_require_exact_retained_payload --basetemp=/private/tmp/momneg1-amod-213324-first10-20261022 --junitxml=/private/tmp/MOMNEG1-AMOD-213324-FIRST10-20261022.xml > /private/tmp/MOMNEG1-AMOD-213324-FIRST10-20261022.log 2>&1
```

No second standalone proof/duplicate actual replay is needed for that claim: the test
prints its complete raw JSON report with -s. Retain log/XML/pins and both worker root
receipts. Each worker has a 60-second timeout; no retry loop. If main instead claims
the CLI, use --pre-root PRE --root POST --amod-only --output a NEW exclusive JSON;
do not also rerun the acceptance without a new explicit reason/claim.

The old combined CLI also now requires --pre-root and uses real separate-root AMOD
comparison; it does NOT treat current/current handoff comparison as PRE/POST proof.
The older compare_amod helper remains for synthetic unit controls, not this acceptance.

All tests/imports/AST/compile/replay/runtime/DB/provider actions UNRUN by author. Main
owns source review/first claim/runtime/reporting. Avicenna nested PRICE reader work
does not change the producer; any producer edit MUST fail this exact allowlist.
