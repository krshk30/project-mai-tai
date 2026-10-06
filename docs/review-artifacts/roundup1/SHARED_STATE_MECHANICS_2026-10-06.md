# Install1 Shared-State Reader Correction

As-of 17:44 ET, October 6. Installation is NOT COMPLETE.

The authorized fresh attempt started at 17:35 ET from exact runtime plan
2012b090b56dab256aeb4cb782359a23bf4e5f79. OMS and strategy strict-flat returned
rc 0. The census returned rc 0: 103 tickets, no requested/price_wait/submitting
tickets, 95 exact terminal parent reads and four no-broker-id rejection proofs.
Redis evictions were 0, memory 811097768 bytes, five owners plus marker present.
The unmodified OMS gate returned rc 0. Its subsequent flat check timed out once,
then recovered on the reviewed 60-second retry.

The attempt STOPPED at 17:41:10 ET, phase initial, completed actions 0. The armed
reader failed with `published-state producer/type mismatch`. Abort paging was
confirmed. All original application PIDs remain active/NRestarts 0; checkout
7823a6fa remains clean, env SHA256
f8b373c66ad71be4fbbfca131e3c21854dfb8d667e63779164e2e77cc169c5eb.

The shared isolated-state stream's newest entry was ORB, not v2. Own bounded
read at 17:41:44 ET: entry 1791322902251-0, 9868 bytes, isolated_bot_state,
source orb, strategy orb, account paper:orb, produced 21:41:42.244971Z.
This is an expected publisher, not proof of v2's armed state. Own source read
confirms OrbApp and the v2 service both publish IsolatedBotStateEvent.

Only the mechanical reader changes. It scans at most 25 entries using exclusive
descending IDs, COUNT 1 and read-only EVAL_RO, each reply bounded to 262144 bytes.
It may skip valid ORB/ORB-Schwab envelopes, but only v2's exact producer/account/
strategy, fresh timestamp and explicit empty armed field can pass. Unknown or
malformed producers, stale/future state, non-advancing cursors, missing v2 and
nonempty armed fields still stop. No snapshot-batches read, Redis write, trading
code, gate policy, setting, application SHA or service-sequence change.

Plan b76371251c27df6b234f29b0a98364bcbaa89e42 is pushed. Armed tests 35 PASS;
full runner harness 744 PASS in 40.96 seconds. Helper SHA256
e1bb2153f45b8b559845e15f4dc3b8aa636594532a1d9757fd5495d42dc78c28.
Manifest SHA256
2bd128811b7413651a82cc07174df724e0da240b2f2098fdcd6e73ea277841c3.
Standing-authority approval SHA256
f7fcea444a8fb360e121ffb7a666b5b05e0787a4875d3a21ace1ee1b9a7baa6d.
Own read-only corrected helper returned v2/armed=0, age 0.804481 seconds;
local receipt /tmp/install1-shared-state-readonly-proof-20261006.json SHA256
82da7bbf23fc44ed59e3407fcd51bf80b1296f2624bbb3ac63c0d835cbbc911c.

The new exclusive attended job is
/home/trader/roundup1-install1-20261006-b7637125, with all 27 staged artifacts
independently hash-verified before execution. Its runner.log and
attempt-oct6-attended/runner-journal.jsonl retain every raw helper stdout/stderr
and exit code. Running is not a COMPLETE receipt.

Previous preserved STOP:
/home/trader/roundup1-install1-20261006-2012b090-retry1736/attempt-oct6-attended/STOP.json.
Previous runner.log SHA256
544c1809751ed0439e9be21275586a35917b9cc62f835735725ac959ed0f627d.
Failing stderr receipt 165-stderr.txt; transient flat stderr 156-stderr.txt.
No failed receipt or attempt directory was removed or reused.
