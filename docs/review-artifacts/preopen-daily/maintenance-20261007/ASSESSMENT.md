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

## Actual closeout, 06:41 ET

Installed exact Redis package exclusions and one literal system Git trust
at 06:33:49 ET. Backups and hash receipt:
`/home/trader/preopen-daily/maintenance-20261007T103349391493Z/installed.json`
(sha256 `1052432939e0e4b356d2281e4b915c4214278d8dee83c2e833a96f3c02890477`).
The original 06:20 failure receipt is unchanged. No trading service action.

The first rerun established clean checkout, the exact acknowledgement and
157/157 flags, but the after16-only LINESRC classifier refused before07
exceptions before the official report was written. A tested report-only
repair sends such unclassified lines unchanged to the official parser.
The next read then exposed the official collector's own early UNKNOWN:
`orb-schwab.log contains a traceback before any timestamp; its time scope
is unknown`. The second report-only repair writes a today-dated UNKNOWN
with that original stderr instead of suppressing the report. It adds no
error allowance and never makes the result PASS. Unique sidecars preserve
every prior invocation; both repair backup directories remain on the box.

Final tests: 129 pass, 10.18s. Final listable root unit rerun at 06:39:26 ET
completed 06:39:32 ET: checkout/clean tree PASS, the measured restart
ACKNOWLEDGED (actual NRestarts still1), FLAGGATE157/157 PASS, explicit retry
zero2/2, report present. Overall rc2/UNKNOWN remains due to untimestamped ORB
log scope, not permissions. No scope waiver, counter reset, app edit, flag
edit, rollback or trading service start/stop/restart.

Report: `/home/trader/known_defect_regression_watch/v2-restart-evidence-20261007.md`
sha256 `bb93610ca7a564b2dd07ab8871d2860931036f411431eb4afeea54c94fcbf6fc`.
Gate hash (mode0700/trader retained):
`40e57465f08c9cd9e34dc5739a3c68585d162ee2a5daa3e6b387b5d9fa24972e`.
Immutable run receipt:
`/home/trader/preopen-daily/runs/20261007T103926738157Z/morning-mechanics-receipt.json`
sha256 `8aeef56355df0df615bb420d1b487d52b8c4b14013a4df7570e200921030fefa`.
Journal: `/home/trader/fleet_health/deployments-20261007.md`.
Live OMS626190/v2626439/strategy626773/control626835/gateway2907 are unchanged
from the successful install. ORB-Schwab765206/NRestarts1 is the acknowledged
02:31 automatic restart, never a maintenance-script restart.
