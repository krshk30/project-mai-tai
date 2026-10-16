# WEEKMAINT1: source-only backend, not qualified or install-ready

## Current F1-F6 Successor (All UNRUN)

The append after `494a97c1` implements actual backend operations in `backend.py`
and existing-client position collection in `collector.py`. It is not a dummy
success backend and does not turn a copy preview into broker proof. The default
apply/boot unit now targets that backend, but a missing/unreviewed root-private
manifest still refuses. NO commands, imports, tests, SQL or operational actions
have been executed for this successor. The older foundation and receipts remain.

The 10:0x ruling DROPS the earlier Friday TodayOrders cross-check entirely.
There is no hook, capture path, extra orders request or root-UID handoff. Each
live account gets ONE existing positions read by the job at START. A failed
attempt is never retried. Manual shares do not gate when bot ownership is zero;
unknown/positive bot quantity refuses. The original Webull wire age remains
bound. Post-stop DB/writer checks reuse this exact evidence, refuse changed
ownership or age >30s, and never call the collector again. This can refuse a
slow stop; it is not permission to refresh or forge a newer wire timestamp.

The DB gate includes ALL-status bot orders and intents created/updated within
two hours, not only nonterminal rows. Nonterminal bot TIF != DAY also refuses.
Broker-order creation is represented by its real updated_at plus submitted_at
or exact-bound intent.created_at (the table has no created_at field). Missing
anchors refuse. Saturday bot DAY expiry is explicit reviewer policy, not a
broker open-order response. Old DAY rows can enter retirement; same-symbol
manual shares leave rows unchanged/report CSV. The exact 04:00 ET anchor stays.

Implemented backend source:

- Exact clean Git/source pins; private approved manifest, exact systemd unit
  inventory and reviewed cron/drop-in/directory digests; nonblocking installer
  locks. State and lock files must already exist: no pre-gate creation. Actual
  package-policy/input hashes and invocation are checked before the first stop.
- Exclusive per-SEC-success claim AND global unresolved-operation fence before
  any stop. Different SEC tokens do not bypass a failed/interrupted operation.
  Only an actual HEALTHY report matching the reviewed operation releases it.
  Durable original
  active/inactive inventory; stop recorded active writer timers first, then
  writers. Post-stop DB/writer recheck with original start positions evidence;
  no wildcard starts or enablement. The current own nonwriter oneshot alone may
  be activating/start, bound to its actual cgroup + InvocationID.
- Existing archive transaction and CSV, separate skipped-row CSV, actual
  AUTOCOMMIT vacuum and same-tablespace filesystem disk checks. No pruning.
- Scoped Saturday post-SEC package permit bound to root-private state, exact
  systemd cgroup/invocation/boot/SEC success and at most two hours. The existing
  maintenance hook uses this for INSTALL only; protected-service policy stays
  deferred. No global attended override. A dedicated security-origin config
  requires an independently reviewed preserved-blacklist fragment/hash.
- A package overrun is durably flagged, never killed or retried. Its lifetime
  guard surrounds spawn before any diagnostic write; record/fsync/logger or
  signal exceptions cannot leave a known package child alive when returning.
  The unit has
  no systemd hard kill deadline for dpkg; the backend waits for that process to
  finish and then refuses automatic continuation after an overrun. This choice
  explicitly requires main/reviewer review before installation.
- 10:1x F6 is VERIFY ONLY after reboot: systemd starts enabled units. The job
  never starts/stops any unit in the boot verifier and never adds a restoration
  or start-intent framework. It checks prior active/inactive state, excluding
  timer-owned bodies (must be inactive); their timers alone certify scheduling.
  The invoking nonwriter maintenance oneshot is not restored as a daemon.
- OMS boot health is one finite journal observation ending at OS boot +300s,
  not five minutes from handler start. Require a successful sync plus BOTH live
  accounts' `[RECEIPT-FIRST-COMMIT]` lines in the current journal `_BOOT_ID` and
  `_SYSTEMD_INVOCATION_ID`. The message's runtime boot UUID is NOT the OS boot ID.
  Late handlers can inspect history only through the same original deadline.
  A current-invocation recheck fences restarts during observation. The separate
  boot unit's finite 360s timeout bounds report/census I/O; no restart loop.
- Missing health returns failed + loud report, leaves the marker/global fence,
  and never returns execute-success. PG/Redis and exact approved timer instants
  (guard, preopen, universe, SEC, prune) plus RETAIN1 hashes remain required.
  Existing no-reboot scope restores only original active writers, NEVER timer
  bodies; inactive units remain inactive. It uses a durable restore barrier,
  not pre-stop sync reuse. No proof is manufactured for disabled services.

### Concrete Input/Integration Questions (Not READY)

1. Main must review/provision the actual unit/cron/installer inventory, source
   hashes, SEC-success path, Monday timer instants and preserved operator
   blacklist fragment. The manifest additionally needs exact reviewed
   `timer_started_services` (including externally triggered preopen bodies),
   `reviewed_timer_started_services=true` and a `prune_scheduled` timer binding.
   Built-in known bodies + actual systemd TriggeredBy also forbid direct starts.
   No real names/values are fabricated in a runnable config.
2. Scoped security-window hook/config and package-overrun behavior need source
   review, then controlled tests. They are not installed or implicitly approved
   by being authored. Plain copy preview remains effect-free; real backend
   applies only with the explicit reviewed private manifest. The new backend
   `--dry-run` uses existing private state/lock files without creating them,
   performs only the existing broker/DB reads, and prints the full step/candidate
   plan. It cannot claim SEC success, stop writers, archive, vacuum, run packages
   or create markers. This source and the authored fake-OS/process controls are
   UNRUN; no Friday input is needed or requested.
3. Actual Linux unit ordering, OMS journal routing/fields, package semantics,
   copy/archive transaction and reboot controls remain unexecuted. No source
   control claims actual broker truth, installation, startup timing or readiness.

The rest of this document preserves the original foundation assessment. Its
"missing backend" and raw all-open requirements are historical, superseded by
the source/status above. It is not a current acceptance claim.

## Preserved Foundation Assessment

Authority: operator/reviewer WEEKMAINT1 07:1x card and subsequent 07:1x gate
answer, 2026-10-16. Source base 37de794a. No production changes authorized here.
All authored controls and the disposable-copy preview are UNRUN. Main owns the
exclusive host; ATR800 and ORB qualification take priority over maintenance.

## Resolved Policy

Open managed rows with nonzero or unknown quantity block maintenance. Open
zero-quantity rows do not block; retirement still requires fresh, complete,
successful broker-wire positions and open-order reads at both venues, for every
live account. A position or working order refuses the entire apply before any
stop/archive. The preview retains candidate rows and refusal reasons for review;
its candidate list is not a list of authorized changes. A live refusal-report CSV
writer remains part of the unimplemented backend contract.

No snapshot pruning is implemented. `dashboard_snapshots` receives plain VACUUM
ANALYZE only, not VACUUM FULL or age retention. Control, owner, latest-per-key and
unknown types are retained. Existing PATH_PRINT, per-module retention and Sunday
RETAIN1 are unchanged. The separately retained reader inventory is a static
source audit, not evidence of every external/dynamic reader.

`policy.py` supplies strict evidence gates, bounded calendar/SEC validation,
conditional retirement planning, four-table disk gates and injected lifecycle
orchestration. `archive.py` stages a private fsynced CSV, locks and revalidates
original rows, archives and updates them in one PostgreSQL transaction, then
publishes the CSV. A committed archive can reconstruct the CSV after a crash.
An uncommitted staged CSV never proves changes. Neither trading history nor
snapshot rows are deleted. A changed source row aborts the entire transaction.

The existing Saturday SEC timer is the sole proposed trigger. Its OnSuccess
drop-in is not sufficient authorization: the current exact-head FULL receipt
must also validate. No new independent timer, persistent catch-up, automatic
retry or repeated reboot is proposed. The Saturday 00:00-01:00 Redis window is
excluded even with supervised `--force-test`.

## Explicit Missing Live Integration

The proposed units are NOT install-ready. Their execute/boot commands refuse
with exit 78 before any connection. No live backend or provider client exists
in this component. The injected test backend is controlled evidence only.

Required reviewed interfaces before live apply:

- Raw-wire collector: exact live account/provider bindings, HTTP success,
  complete pagination, no cache/error-empty, original aware wire timestamps,
  positions and all working orders. Existing Webull cached positions and
  today-order lists cannot silently certify this contract. A post-stop read
  must be later than the recorded stop barrier and at most 30 seconds old.
- Durable inventory: all actual active writers/services/timers/cron and exact
  restore set, cooperative installer exclusion, single-flight operation claim
  per successful SEC run, and recovery of interrupted operations. A new private
  advisory lock alone does not fence existing installers or writers.
- Database/vacuum backend: complete typed inventory; quiescent recheck; archive
  transaction; plain VACUUM ANALYZE outside a transaction; measured four-table
  rewrites only with bounded locks and fresh same-filesystem disk >=2x relation
  bytes. Disk-short rewrites are reported as skipped, not successful.
- OS backend: preserve existing security package policy, protected packages,
  needrestart and policy-rc.d; resolve the existing Saturday 00:00-01:00 package
  window versus the post-SEC schedule explicitly. Never globally set an
  attended override. Journal retention is 30 days; reboot only when required.
- Boot backend: durable original boot ID and stopped inventory; claim one
  report before probes; observe a completed OMS sync, Redis/PG health and
  Monday guard/preopen/universe/SEC schedules. Preserve legitimate weekend
  inactivity and oneshots instead of blindly starting every project unit.
  Keep failed marker/inventory, loud alert, no retry loop.

The policy calls those interfaces in order but does not fabricate their proof.
The Saturday pre-04:00 session anchor is still the prior day's 04:00 ET; this
can leave Friday session rows unretired. No implicit weekend anchor widening.

## Queued Copy Preview (UNRUN)

Only after main explicitly grants maintenance execution on a disposable PG16
copy of the approved private export:

```sh
umask 077
PYTHONPATH="$CHECKOUT/src:$CHECKOUT" "$PYTHON" -B -m ops.weekmaint.copy_plan \
  --dry-run --copy-dsn "$EXPLICIT_DISPOSABLE_COPY_DSN" > "$PRIVATE_OUTPUT"
```

The DSN must identify `replay_*` or `qualification_*`, an explicit nondefault
port and literal loopback or `/private/tmp` Unix socket. The read transaction
is read-only/repeatable, bounded and identity-checked. Original row JSON remains
private in the output. Exit 2 and UNKNOWN broker/install/writer/SEC gates are
expected; this is not a live GO, routing replay or whole OMS boot proof.

Next queued execution: focused authored controls, actual read-only copy plan,
then separately reviewed archive transaction controls on another write clone.
No host slot is assumed, and no measurements or PASS counts are claimed.
