# Morning mechanics, 2026-10-07

Own reads at 06:24-06:36 ET: root's clean systemd environment rejects the
trader-owned checkout with Git's "dubious ownership" protection. An exact
`safe.directory=/home/trader/project-mai-tai` invocation reads the expected
5b8b4f64 head; no directory access is granted and no wildcard trust is needed.
The first run's raw receipt remains in
`/home/trader/preopen-daily/runs/20261007T102000265335Z/raw.txt`.

Own journal read: Redis stopped and started 06:31:08 UTC (02:31 ET), ORB-Schwab
exited 06:31:09 and automatically restarted 06:31:14 UTC. The actual identity
is PID765206, NRestarts1, InvocationID de16f6a3ae8f438a8aae3d294d7db686.
unattended-upgrades.log names redis-server/redis-tools; dpkg independently
reports both at 5:7.0.15-1ubuntu0.24.04.5. Redis remains persistence-OFF.

Under the explicit morning authority, add only redis-server and redis-tools
to the existing package-exclusion construction. Security updates for them
now require an attended maintenance window; other updates remain enabled.

The acknowledgement is root-pinned to this exact application, unit, PID,
start, invocation, active/running state and NRestarts1. It never resets a
counter or restarts a service. The report preserves the original collector
FAIL and changes only its single ORB restart finding to ACKNOWLEDGED; any
other failure or unknown remains blocking. Sidecars get unique invocation
names so today's read-only rerun cannot overwrite the prior raw report.

126 mechanics tests pass (9.11s), including changed identity/ack refusals,
no unrelated-failure admission, exact-directory trust and isolated gate diff.
No trading-code, switch, ledger, migration or service action is authorized here.
Re-run the listable root preopen checks-only unit before 07:00; report its real
verdict, including any unrelated LINESRC1 or fleet finding without a waiver.
