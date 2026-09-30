# FLAGGATE: running flag contract

Requirement card (operator 2026-09-29): each morning the pre-open gate checks every reviewed
boolean switch against the **running process** that owns it. A fix that should be ON but runs
OFF, or a staged/parked switch that should be OFF but runs ON, blocks readiness and names the
switch. An unreadable process is UNKNOWN and also blocks readiness; the gate never changes a
setting or restarts a service.

`ops/health/expected_flags.json` explicitly classifies all 120 boolean fields in
`Settings.model_fields` at main `71edb961`: name, expected value, owning service, reason, and
ruling. The three active-rule exceptions are ORB live orders (staged OFF), the CW floor (Card 10
OFF), and Massive ATR seed (parked OFF). Other OFF entries are dormant, legacy, or optional
paths, **not** dark fixes to enable. The 2026-09-29 21:01 ET read-only `/proc` audit of ten
running units matched 124/124 owner checks (120 settings; four extra checks for shared ORB and
dual-broker flags). This is a baseline observation, not a live gate run or proof of tomorrow's
state.

The checker reads `systemctl MainPID` and `/proc/<pid>/environ`, then rechecks the PID to reject a
restart during the read. It never prints unrelated environment values. An unset boolean uses
the reviewed `settings.py` default. The box checkout's `.env` currently has eight boolean keys;
all eight are also present in the owning process environment. **Before installation**, repeat
that comparison. If any effective `.env` boolean is not mirrored in `/proc`, stop the install:
the checker must be extended to model that source before a default fallback is trustworthy.
CI's catalog test rejects any new, removed, or duplicated settings boolean without an explicit
catalog edit. A malformed catalog is UNKNOWN, never PASS.

## Reviewed installation seam (not executed by merging)

An exact-SHA operator GO is required. First confirm the production checkout's settings boolean
inventory still equals the reviewed catalog, all listed units are running, and the isolated
`/home/trader/restart_evidence/` installation is not being changed concurrently. Back up
`/home/trader/preopen.sh` and the installed router. Copy these three files from the pinned head
and verify each installed SHA-256 against its Git blob:

- `ops/health/expected_flags.json`
- `ops/health/expected_flags_check.py`
- `ops/health/preopen_restart_evidence.sh`

In the existing `/home/trader/preopen.sh`, after the restart-evidence section and before the
final verdict, add this reviewed call without altering the existing checks:

```bash
printf '\n=== RUNNING FLAG CONTRACT ===\n'
FLAGS_CATALOG=/home/trader/restart_evidence/expected_flags.json
flags_output="$("$REPO/.venv/bin/python" /home/trader/restart_evidence/expected_flags_check.py \
  --catalog "$FLAGS_CATALOG" 2>&1)"
flags_rc=$?
printf '%s\n' "$flags_output"
preopen_record_expected_flags "$flags_rc" "$FLAGS_CATALOG" "$flags_output"
```

Run `bash -n`, record the backup/diff/hashes, re-pin the one-day pre-open gate to the final
checkout SHA and unchanged/current service identities, then run it read-only as `trader`.
Require a single `Final call: PASS; checked=124/124 ...` from FLAGGATE and an overall green gate
before calling the install complete. Any mismatch is REAL FAILURE; unreadable data or an
exit-code/final-call disagreement is UNKNOWN. Do not restart a bot to force green.
