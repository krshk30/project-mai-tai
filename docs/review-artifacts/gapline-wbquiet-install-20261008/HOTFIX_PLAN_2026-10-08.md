# PG-only hotfix mechanics: prepared, not staged

Sole writer Lane C, branch codex/gapline1-wbquiet1-after-close-1008.
Literal runner: hotfix-job/runner.py. Future unique box job:
/home/trader/after-hours/2026-10-08/falseflip-pg-hotfix/job.
Future unit: project-mai-tai-falseflip-pg-20261008.service/.timer.
No actual hotfix manifest or approval exists: final reviewed application is not
yet supplied. H #1137 successor 5d17199caff81e2b9e232ac78091579aa3ff4334
is not assumed pinned/merged/green by this plan. Source changes belong to H.

## Original install: applied but FAILED proof

Application eced4599d05e72adab77551f18df8050a94028e2 is installed, schema 0023.
Original plan a5e43efa55f0e6cf21111c4974f5f44cf91216df; manifest
c053e0324221a01b89236da3b0d6396b55099dd9c265acc2a85ffb4f9cd38d21.
Original ABORT sealed 2026-10-08 20:35:55.236530 UTC, stage
ten-minute-observation, rc 1. It remains immutable and must never be rerun.
Original runner.log sha256:
21802d9367527a4aec84c01e65db5c42c1b547b5c7577d95ea3e6f42488399a7.
Original post-install-proof.json sha256:
874f3b72cc83568993048daf53e11883f2d2ebb81e36fd42d73debc9937701a8.

Actual running identities after that attempt: OMS 2073383 and strategy 2073394
(20:22:58 UTC), v2 2074723 (20:25:04), control 2075090 (20:25:35).
These are evidence of running services, not permission to adopt changed identities.
The future manifest requires a new fresh whole-fleet baseline.

OMS proof has 75 error/traceback lines, not 75 distinct exceptions. The PG int4
JSON epoch cast overflow is a real runtime failure. Four v2 ERROR lines at
20:25:11 are closed-fill confirmation-exit line_unproven observations (FLYE
871c4868, 27660cee, 31a558a7; AIXI 44a2770a), not tracebacks or orders.
PG-only source changes do not fix those v2 observations. They may repeat at the
next v2 startup: this conflicts with the zero-error completion requirement.
No waiver or error filter is assumed. If repeated, the new proof remains FAILED
and services remain running; no improvised restart, fallback or rollback.

## Exact future sequence

Bind only an exact independently pinned, two-Validate-green #1137 landing, based
on installed eced. Verify real PostgreSQL epoch test receipt and exact landed
source/test files. F #1133 and I #1136 are excluded. Generate from committed
hotfix-job blobs, verify every staged byte, use one exclusive installer lock.

After-close fresh read-only broker/order/row gates and native OMS/v2 fences run
before the first write and each restart. Only existing tested rc-2 retries
(three attempts, 60 seconds apart) apply. No armed override. No clock abort
after first write. The reviewed existing clock-only interface is unchanged.

Verify schema is already 20261008_0023 by read-only SQL; never run an upgrade.
Record actual RemovedWaitStore.restore counts before/after; archive types and
all existing rows remain untouched. A recreated active row is reported, not
deleted or forced to zero. Direct boot-memory count lacks a source marker and
is UNMEASURED; the read-only database replay is labelled separately.

Install only the approved catalog namespace, with backup and byte hashes.
No environment write or flag activation occurs: LINE/GAP/FALSE remain true,
MIRROR and both SLOT keys true, RPG false. Preserve the numeric file exactly.
Deploy OMS with the repo deployer (its built-in strategy stop/start), then v2;
MAI_TAI_RUN_MIGRATIONS=0. No control, ORB or other service action.
Check actual new identities, flags and fresh trading reads, then collect ten
minutes of proof. No COMPLETE unless actual required evidence passes.

## Audit namespace disposition

Independent read at 20:44:22 UTC: both checkers import current Settings at
/home/trader/project-mai-tai/src/project_mai_tai/settings.py, SHA256
daff8ea9bf4da8b17ad7d1af11773fe055987b078dc9efcec638119462684fa0.
Current checkout eced; Settings has 132 bools. Box catalog has 139 rows.
The seven legacy bool fields were removed by reviewed commit
daed2ccbaab32806cc9973a4ce469b242445c4ce; this does not stop the old ORB unit.

Repo checker plus stale box catalog returns rc 2, checked 0/0: invalid orb
also_check_services. Box checker plus stale box catalog also returns rc 2,
checked 0/0: seven extra legacy Settings fields. Neither is a PASS receipt.
Current repo checker SHA256:
7931090ffc4e98429d31432aaf2cdd46291100314c2a2e504dea21339d8721c3.
Box checker SHA256 remains:
4822bcc1733429ec31b18de0b3a67d6e5fd517418b9505f7fbb8c7d3a67fdd58.

Parent's resolved authority: install current approved 132-row bool catalog,
all 132 expected values unchanged versus box, including RPG false. Reviewed
owner updates are metadata only. Preserve box numeric ten checks, retry budget
zero on both consumers: 153/155 measured with two paper UNKNOWNs. Repo defaults
alone have eight numeric checks (151/153) and are not substituted for box numeric.
Legacy ORB identity remains independently checked and unchanged. No discarded
ORB/paper result is described as current Settings coverage.

## Preopen bookkeeping

The prior preopen pin was not updated because original proof failed. The new
helper binds immutable original manifest/ABORT/failed-proof/source-log hashes
and original before-restart snapshot. A combined install record includes the
earlier authorized control restart plus the actual new OMS/v2/strategy starts.
Control is not restarted a second time and cannot be adopted from an arbitrary PID.
Historical failure receipts remain FAILED. Dynamic ET date and paper shape,
root permissions/adapter, runtime dependency hashes and exact orb-schwab Redis
upgrade acknowledgement are preserved. No report or helper invents gate PASS.

Local mechanics verification: 302 passed in 6.28 s, seven native-platform skips;
13 mutation controls RED; Ruff runtime/tests clean, bash syntax clean.
This is not a production rehearsal, final manifest, global suite parity,
zero-restored-boot claim, or actual preopen PASS receipt.
