# NFQ2 + HOTFIX1 scoped offline proof

User-authorized proof worktree only. No production action, publish, merge, pin,
source branch edit, runtime invention, or rebase. Local detached commits contain
the clean NFQ-only replay plus owned tests/receipts.

Base HOTFIX1 APP: 994f08aee2c35b3809b3c3384e0d8628807dcfb7.
Whole tree equals pinned 5525739a: 7d827a407825d1d93aae8c3946d8b55320b9680f.
NFQ2 source: 0318ef84dc11b1b052073092828b80ae23c1bf54 at
/Users/velkris/Projects/project-mai-tai-nfq2-side, branch codex/nfq2-eh-hold-side.
NFQ-only commits replayed: 0239726f, 16c435b0, 0318ef84.
Combined runtime commit: 494bbeefdc0cd392747f528010c4f73043d2477b;
tree 5563510bce5b6e255d8daceca7835b58b87991d3.
The initial documentation-only conflict and aborted attempt are retained in
initial-composition-conflict.txt. No runtime conflict was manually resolved.

Prior NFQ2 sustained receipt disabled drift and proved only NFQ2 in isolation.
The prior HOTFIX1 full-run 158.85844 ms failure remains unexplained and is not
waived by this proof. NFQ2's prior full unit receipt is not a green suite.

New test: tests/unit/test_nfq2_hotfix1_combined_proof.py. Runner:
docs/review-artifacts/nfq2-hotfix1-proof/run_proof.py. Run with this worktree's
src first on PYTHONPATH, using Python 3.12.13. Each --label creates a new
directory exclusively and preserves logs, exit statuses, exact HEAD/tree,
source/test/runner hashes, import path, dependency versions, numeric and boolean
settings, and disclosed boundaries. Controls mutate loaded methods only.

Benchmark: real monotonic 60 seconds, target 240 quote events/s, alternating
11-second-old matching BIYA asks and fresh unrelated OTHER asks. One NFQ2 hold
must remain held for every tick. Real enabled drift reader has a separately
seeded BIYA working limit at 2.55 on a simulated paper account; 2.54 quotes
exercise its actual in-memory drift predicate. Drift tolerance is 1 cent.
The retained mirror flag is ON, but no retained-mirror hold is seeded.
Eligibility SQL must be zero; real SQLAlchemy transaction begins and statements
are measured. Only periodic NFQ2 ownership proof and drift cache refresh are
excluded from the tick budget, each on the 5-second cadence and off-loop.
These functions are called by the benchmark's periodic task; the full broker
sync runner is not started. Initial seed/hold/cache SQL is setup before timing.

SQLite StaticPool, fixed logical NOW and AM helpers, fake Redis, simulated
broker, no active exit position/armed hard stop. Full quote handler is real;
counting wrappers delegate to real drift functions. No drift stubbing, GC
disabling, timing acceleration, or threshold relaxation. This is not live OMS,
PostgreSQL, broker cancellation latency, or installation proof.

Fast controls cover no-hold, unrelated and stale 1,000-event zero-SQL paths;
150ms delayed real eligible worker persistence and 1,000 concurrent duplicates;
real drift scheduling/read/write with broker-boundary stub and duplicate quotes;
both EH paths/accounts through full quote handler and duplicate serial delivery.
The serial replay uses the prior explicit fillability/collision test boundaries.
Serial claim and intent-pipeline SQL is outside the tick measurement and remains
synchronous in current source; no all-OMS off-loop SQL claim is made.

Preserved fast-expanded.txt failed because the new test invoked shutdown before
awaiting broker dispatch. HOTFIX1 correctly suppresses broker actions once
closing. The test now drains admitted work before shutdown. Runtime was untouched;
fast-drift-repaired.txt reports 9 passed / 1 benchmark deselected.

Reproduction (from proof worktree):

    export PYTHONPATH="$PWD/src"
    /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/nfq2-hotfix1-proof/run_proof.py fast --label fast-new
    /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/nfq2-hotfix1-proof/run_proof.py controls --label controls-new
    /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/nfq2-hotfix1-proof/run_proof.py legacy-controls --label legacy-new
    /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/nfq2-hotfix1-proof/run_proof.py suite --label suite-new
    /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/nfq2-hotfix1-proof/run_proof.py benchmark --label benchmark-new

Results are recorded after this scope/test freeze, without changing runtime or
tests. All failed receipts remain. Do not infer NFQ2 pin/readiness or live OMS
qualification from passing offline benchmarks. Remaining broader gaps include
dispatch-uncertainty recovery, legacy identity/drain policy, delayed feedback
and restart ordering, exact NFQ2+SLOTCLEAR1 integration, current-main/Linux CI,
and the parent lane's paired installation-runner staging.
