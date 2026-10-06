"""Narrow, hash-bound transformation of the recorded deployed preopen gate."""
import re
import shlex
from release_policy import APP, BASELINE_GATE, CHANGED, NEW_ENV, PM, RETRY_ENABLED, RETRY_MAX, digest, need


def assignment(script, key, value):
    pattern = rf"^{re.escape(key)}=.*$"
    need(len(re.findall(pattern, script, re.M)) == 1, "ambiguous assignment: " + key)
    return re.sub(pattern, lambda _: key + "=" + shlex.quote(str(value)), script, flags=re.M)


def build(original, final, snapshot, record, *, restart_strategy=False):
    need(digest(original.encode()) == BASELINE_GATE, "deployed gate differs from recorded reviewed baseline")
    script = original
    script = assignment(script, "EXPECTED_DATE", "DYNAMIC_ET_TODAY")
    script = script.replace("EXPECTED_DATE=DYNAMIC_ET_TODAY", 'EXPECTED_DATE="$(TZ=America/New_York date +%F)"')
    script = assignment(script, "EXPECTED_SHA", APP)
    script = assignment(script, "SNAPSHOT", snapshot)
    script = assignment(script, "INSTALL_RECORD", record)
    report = "REPORT=/home/trader/known_defect_regression_watch/v2-restart-evidence-20261006.md"
    need(script.count(report) == 1, "report anchor ambiguous")
    script = script.replace(report, 'REPORT="/home/trader/known_defect_regression_watch/v2-restart-evidence-${EXPECTED_DATE//-/}.md"')
    owners = [("schwab-1m-v2", ""), ("oms", "OMS_"), ("orb-schwab", "ORB_SCHWAB_"), ("control", "CONTROL_")]
    if restart_strategy:
        owners.append(("strategy", "STRATEGY_"))
    for name, prefix in owners:
        need(name in CHANGED or (restart_strategy and name == "strategy"), "non-scoped pin update")
        state = final[name]
        need(state["MainPID"] > 0, "new pin lacks PID")
        script = assignment(script, "EXPECTED_" + prefix + "PID", state["MainPID"])
        script = assignment(script, "EXPECTED_" + prefix + "START", state["ExecMainStartTimestamp"])
    old = 'check_identity momentum-paper "$PAPER_UNIT" "$EXPECTED_PAPER_PID" "$EXPECTED_PAPER_START"'
    need(script.count(old) == 1, "paper check anchor ambiguous")
    script = script.replace(old, '''if "$REPO/.venv/bin/python" /home/trader/preopen-daily/daily.py paper; then
  pass "paper active/NRestarts0 and guard-linked today's03:40 admission"
else
  fail "paper/guard admission unreadable or failed; no identity exemption"
fi''')
    old_restart = "  --restarted strategy \\\n"
    need(script.count(old_restart) == 1, "restart declaration ambiguous")
    script = script.replace(old_restart, (old_restart if restart_strategy else "") + "  --restarted orb-schwab \\\n")
    restart_anchor = "  --restarted orb-schwab \\\n"
    need(script.count(restart_anchor) == 1, "control restart declaration ambiguous")
    script = script.replace(restart_anchor, restart_anchor + "  --restarted control \\\n")
    expectations = {(name, key) for name in ("oms", "schwab-1m-v2") for key in PM}
    expectations.update({("oms", NEW_ENV[2]), ("orb-schwab", NEW_ENV[2]),
                         ("orb-schwab", "MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED"),
                         ("orb-schwab", "MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED")})
    expectations.update((name, RETRY_ENABLED) for name in ("oms", "schwab-1m-v2"))
    additions = ""
    for name, key in sorted(expectations):
        value = "false" if key == "MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED" else "true"
        line = f"  --expect-flag '{name}:{key}={value}' \\\n"
        if line not in script:
            additions += line
    anchor = "  --expected-alembic-head 20261005_0022"
    need(script.count(anchor) == 1, "schema proof anchor ambiguous")
    need("--no-schema-change" not in script, "unchanged-schema declaration already present")
    script = script.replace(anchor, "  --no-schema-change \\\n" + anchor)
    script = script.replace(anchor, additions + anchor)
    numeric = "".join(f"  --expect-flag '{name}:{RETRY_MAX}=0' \\\n" for name in ("oms", "schwab-1m-v2"))
    need(RETRY_MAX not in script, "retry numeric expectation already present/ambiguous")
    script = script.replace(anchor, numeric + anchor)
    annotation = 'preopen_record_restart_evidence "$evidence_rc" "$REPORT" "$evidence_output"\n'
    need(script.count(annotation) == 1, "verdict route anchor ambiguous")
    # The wrapper also checks report generation time; the hand-run gate checks its ET date.
    script = script.replace(annotation, annotation + '''if ! "$REPO/.venv/bin/python" /home/trader/preopen-daily/daily.py report-date "$REPORT"; then
  fail "restart report missing or its generated ET date differs from today's gate"
fi
''')
    return script
