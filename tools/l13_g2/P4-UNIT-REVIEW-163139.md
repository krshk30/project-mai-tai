# L13 P4 revised16:31:39 disposition: UNIT REVIEW / RECEIPT SOURCE ONLY

The16:30 request for a new isolated Linux/systemd executor is SUPERSEDED by the
newest explicit Claude16:31:39 ruling read before building. No systemd fixture,
executor, production kill/restart or provider connection is created. The original
worker-kill proof is not relabelled as native systemd kill proof. Main alone
records this scope correction; no author queue/status/LATEST writes.

P4 now consists of controlled evidence + reviewed unit + CLAUDE's post-install
read. Actual post-install receipt remains UNKNOWN/UNRUN; source is not P4 GO.
P5 latest ALL-ON and boot/P6 remain required. P1/P3 are consumed, not repeated.

## Exact unit diff for reviewer

Base690 had no snapshot-worker unit. Full new26-line file is reproduced in
P4-worker-unit-vs690.patch (review-only patch artifact; DO NOT apply it to an
already-composed tree). Existing tracked production unit is NOT edited here.
Reviewed unit SHA256:
5b4f67837f2a7def8ff29b47e932566c09989e942da156c46a096229082225c5.
Current frozen51b source worker b05660d33c5cde63772feb0f00aafc591e3c300605d4898b6f08c55cc9bcae56;
gateway69eb17cd9f71360e117d98d87d6f55aa090dffc2302736030544e043d074961b;
publisher7deeccbce10266043dd4ad118b706d57ee1558ccb3f9eae2f616e51056e59517;
entrypoint335ec3d2630cb50ea47d2200b5fa312015fdefc36511cd2b9a298b699ad34cb6.
Actual final installed composition needs INDEPENDENT refreshed source manifest;
do not reuse these pins if later approved source composition changes a file.

Restart=always /RestartSec=5 /StartLimitIntervalSec=0. After network/Redis and
project-mai-tai-market-data.service. PartOf=project-mai-tai.target, matching the
gateway and application units. There is NO PartOf/BindsTo/Requires coupling to
the gateway: restarting gateway alone must not restart/stop the worker (the new
11 controlled seam cases checked that independent process keeps publishing).
Target Wants the worker; installer copies its unit. Deploy order is gateway,
worker, then strategy consumer; heartbeat maps to market-data-snapshot-worker,
not gateway. Worker snapshot envelope source remains market-data-gateway for
payload compatibility; separate heartbeat identifies worker PID/instance.

### STOP/guard comparison, explicit not guessed

Worker and market-data units share trader/group, working directory, environment,
RestartSec5,NoNewPrivileges,PrivateTmp,UMask0027 and log destination. Neither has
ExecStartPre or a STOP-file bypass/removal. Snapshot entrypoint just constructs
the independent data service; it neither starts a trading consumer nor touches
the momentum guard. A trade STOP must NOT silently turn the data feed off.

The distinct momentum-paper unit retains its existing guarded
ExecStartPre=+.../daily_guard.py --paper-prestart and RestartPreventExitStatus=78.
This source checkpoint does not edit that unit, daily guard, STOP file, execution
flags or restart order. Momentum consumers must keep their existing STOP read
after restart; absence of a STOP read in a data-only worker is NOT evidence that
trading was authorized. The positive STOP seam already consumed by main checks
real scanner preload, not external daily-guard execution. Installed external
guard bytes/state are UNKNOWN here. Reviewer must confirm this is the intended
"same STOP/guard behaviour" comparison; do not add PartOf=market-data or a new
worker STOP gate by interpretation to defeat independent process behavior.

## CLAUDE-only post-install read recipe: no executable service-action script

After a separately authorized passing-set install, Claude's EXISTING read-only
observer collects within2min of worker start. This is NOT a new author/executor
grant; no SSH/PG/provider/production command is executed by this checkpoint.

Retain full source head + file SHA manifest, installed worker unit bytes/hash,
host/boot identity, exact read times and raw-systemd/read-only-stream packet SHA.
Before/after systemd properties: unit name/load/active/substate,MainPID,NRestarts,
ExecMainStartTimestamp. Both worker reads must show active/running,NRestarts0,
same PID/start; collect gateway PID/start identity at both ends as well. Worker
and gateway PIDs must differ. No kill-to-batch receipt is expected or invented:
it is NOT_PERFORMED under the explicit no-production-kill disposition.

Read retained original snapshot stream rows and original trade_tick envelopes,
not provider endpoints. Batch identity/produced_at/completed_at stay original.
No native PID is invented inside snapshot payloads (the schema has no PID).
Process attribution comes from unit/source/heartbeat identity; temporal stream
association is not a new native causal ID. Read existing baseline cadence packet
and post-start batch sequence. Report actual intervals/sample counts; Claude
compares old cadence. First observed fresh batch must be within120s of start.
Retain ALL selected trade samples in the stated read window, SIP timestamp and
original produced_at; compute nearest-rank produced-minus-SIP p99 strictly<300ms.
No lag filtering,0 defaults, partial-window success or synthetic UTC/monotonic
clock conversion. Required timestamps are actual epoch-ns from original fields
and collected unit-start UTC, not a guessed monotonic-to-wall mapping.

## File-only validator / controls (UNRUN)

tools/l13_p4_postinstall_receipt.py imports only stdlib; no app/SDK/Redis/DB,
subprocess,systemctl,signal,SSH or service access. load_receipt bounds input32MiB;
100000trades/1000batches bounds. validate_receipt requires independent MAIN pins
(expected_head, expected_unit_sha256, expected_source_files), not self-attested
packet values as expected. The schema is explicit in the source and synthetic
test fixture. Missing fields or evidence refuse; only internally consistent
receipts return a result, NEVER a native PASS.

Checks active/running+NRestarts0,stable independent PIDs,source/unit hash equality,
complete window,strict ordered unique actual batches,first observation within
120s,baseline cadence reference/hash,unique bounded raw trade IDs/nonnegative
lags and strict p99<300ms. Returns raw cadence intervals,counts,p99 and
INTERNALLY_CONSISTENT_REVIEW_REQUIRED_NOT_NATIVE_PASS. It cannot authenticate the
collector, prove raw-hash provenance without original packet review, or declare
unchanged cadence from settings. Reviewer compares referenced raw bytes and
cadence; no numerical cadence-tolerance or read-window sample minimum invented.

16 intended source controls in test_l13_p4_postinstall_receipt.py, ALL UNRUN.
Synthetic packets are labelled as such; none is a real post-install receipt.
Main-only new functional proposal: whole module, fresh raw/XML, no filters or
automatic retries; no P1/P3/new native performance run included.

SAIQ985/882 source remains separately frozen/mainqueued. No L9 repair or incident
changes here. No production source/unit changes in this successor.
