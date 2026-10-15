# MOMWARN1 Monitor F1/F3 Local Packet

Base: composed X/S `d9e92b92a93b771edf2539fa9d954fcf6fa2b5d9`. Source isolated from frozen expiry/install worktrees. Required marker: `Co-Authored-By: OpenAI Codex <noreply@openai.com>`.

## F1 Local Fix

Runtime scope ONLY `src/project_mai_tai/market_data/gateway.py`. Existing `[MOMWARN1-AUTO-REVERT] reason=monitor_unreadable A_only=true` WARNING now carries JSON `detail={exception_class,message}`. The existing latch emits it once, not a second diagnostic line. Exception input capped at 2,048 characters, message at 512, class at 80; JSON ASCII escaping prevents newline/control-code injection. URLs, labelled credential/token/chat fields and authorization values are redacted. Unformattable exception text becomes `[message unavailable]` so formatting cannot bypass the existing safety path. No traceback or settings dump.

Revert reason, sticky latch, unsubscribe/drain/cancellation, normal-window behavior and all watched T/Q/fallback/owner paths unchanged. No provider, quote, health-source, settings, OMS or trading source edits. Log detail is built only on the exception path, no per-tick work or DB/HTTP added.

Focused run: **169 PASS, 0 FAIL, 0 SKIP, 4 timing/burst controls deliberately deselected; 1.39 seconds**. Includes 22 new F1/F3 controls plus 147 unchanged gateway/live-health/health-contract controls. No benchmark or load suite. Ruff and diff check PASS.

- XML: `/tmp/momwarn1-monitor-f1-f3-focused-20261013.xml`.
- Output: `/tmp/momwarn1-monitor-f1-f3-focused-20261013.log`.
- Earlier new-only component: `/tmp/momwarn1-monitor-f1-f3-controls-20261013.xml`, 19 PASS before three extra redaction/first-pass controls.
- Preserved initial component: `/tmp/momwarn1-monitor-f1-initial-20261013.xml`, 4 FAIL / 5 PASS. Four new fixtures incorrectly assumed provider `_warning_failed`; native gateway latch disables A without changing that provider flag. Corrected to assert actual gateway latch/A disable, without runtime-policy changes. Initial logs also exposed duplicate WARN risk; final implementation extends the existing latch's line and final tests assert exactly one total WARNING.

The local tests deny OS read permission on four controlled temporary log files as the nonroot Mac user; each produces real `PermissionError`/EACCES in actual reader/gateway methods. This is not actual Linux `trader` readiness. Tests also cover first-monitor denial before any A subscription, latched no-repeat/no-reenable, owners/persistence unchanged, no new quote/Redis publication, cancellation not mislabelled, escaping/bounds/credential redaction and broken exception formatting.

Command (rerun on exact committed source for final receipt):

```sh
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python -B -m pytest -p no:cacheprovider tests/unit/test_momwarn1_monitor_diagnostic.py tests/unit/test_momwarn1_service_user_proof.py tests/unit/test_momwarn1_gateway.py tests/unit/test_momwarn1_live_health.py tests/unit/test_momwarn1_health_contract.py -k 'not fair_live_loop and not huge_synthetic and not slow_offloop and not raw_SDK_normalization' -q --junitxml=/tmp/momwarn1-monitor-f1-f3-exact-20261013.xml
```

## F3 Staged Caller / Explicit Dependency

`ops/health/momwarn_service_user_proof.py` is **STAGED ONLY, NOT RUN ON BOX**. Current four root:root 0640 log permissions are the reported live cause and have not been fixed by this lane. M3's earlier root/config checks omitted actual service-user `read_live`; they cannot substitute for this proof.

Main, after today's close and only after its normal reviewed gates:

1. Apply reviewed F2 read access plus durable logrotate configuration. F2's isolated-copy rotation proof alone does not repair box permissions.
2. Stage reviewed exact composed sources/caller readably in the service checkout; the caller checks an explicit expected 40-character local HEAD. The gateway has not restarted yet, so the live running gateway environment/identity can be retained.
3. Run this caller as actual `trader:trader`, using the service virtualenv interpreter and `-B`. It refuses root/wrong user, mismatching effective UID/GID/supplementary groups, nonactive gateway or wrong repository/HEAD. It reads ONLY allowed systemctl identity fields, then copies the current gateway's bounded `/proc/PID/environ` into its own ephemeral process memory; no values or secret hashes are emitted, no env-file/default placeholder masquerades as the gateway environment.
4. The caller performs exactly two `WarningHealthReader.read_live` calls one second apart, using actual native reader/source implementations. Both must succeed, and every listed file must be readable. One configured loopback Redis client (max connections 1, connect/read timeout 2 seconds), `XREVRANGE prefix:strategy-state-isolated count=32` only, then close; no DB/provider/external socket or Redis write. No daemon import or provider construction. This local Redis read is required by the existing live-health contract, not a second provider connection.
5. Retain stdout/exit code and caller/source hashes. `READ_LIVE_2_OK` is ONLY a read proof: watchdog health verdict is explicitly `NOT_EVALUATED_NO_GATEWAY_PUBLISH_LAG`. Any error/missing evidence remains UNKNOWN/nonzero exit, never root-success or fabricated health.
6. Only after proof/review permits: main's after-close gateway restart resets the already-sticky live latch. G1/G-OC/G2 must rebaseline identity AFTER that restart and run read-only guard pre-start checks. Caller does not restart, re-arm, rebaseline, alter permissions or install anything.

Future main invocation, **not executed by this worker**; `FINAL_REVIEWED_HEAD` must be replaced by main's exact installed candidate, not this component head if other lanes are composed:

```sh
sudo -n -u trader -g trader -- /home/trader/project-mai-tai/.venv/bin/python -B /home/trader/project-mai-tai/ops/health/momwarn_service_user_proof.py --repository /home/trader/project-mai-tai --expected-head FINAL_REVIEWED_HEAD
```

Exact health-source opened-path inventory (8 distinct paths; PIDs from validated source config):

- Configured health JSON (default `/run/project-mai-tai/momentum-warning-health.json`).
- Configured log directory's `oms.log`, `strategy.log`, `schwab-1m-v2.log`, `orb-schwab.log`.
- `/proc/{oms_pid}/stat` and `/proc/{v2_pid}/stat` (actual configured source instances).
- `/proc/meminfo`.

`LiveWarningHealthSource.read` opens the two process stat files plus OMS/v2 logs. `WarningHealthReader.read_live/read` additionally opens config, all four error logs and meminfo. Each distinct path's actual owner/group/mode/device/inode and successful nonblocking regular-file read-open are recorded without content. Context identity separately reads the running gateway's stat/status/environ and systemctl fields; no secret-file scan. Permission metadata is a pre-read snapshot, not a durability assertion or a guarantee against later rotation. Native source integrity/age/EOF checks run unchanged in each read.

Local caller controls use explicit controlled proc/log/Redis inputs only. They prove two native calls/path enumeration, root/wrong-user refusal, exact service-environment adoption without exporting values, permission denial/UNKNOWN, readable files with missing state still UNKNOWN, malformed PID rejection before path opens, and no daemon/provider/write commands. They are NOT box evidence. Actual F3 remains pending F2/main after close; today's snapshot fallback stays untouched. Main alone owns final exact-composition broad/all-ON 60s 3/3 and deployment. G3 HTTP-ACK-only finding unchanged.
