"""[codex] Existing dated inactive-paper population admission, Oct7 only."""
from datetime import datetime, time
import re
from release_policy import DAY, ET, need, digest

PAPER_FLAGS = {("momentum_paper_enabled", "momentum-paper"),
               ("market_data_subscription_startup_enabled", "momentum-paper")}


def inactive_paper(before, after, now, scheduled_close=None):
    from release_policy import system_time
    need(before == after, "paper identity/state changed during FLAGGATE")
    need(now >= datetime.combine(datetime.fromisoformat(DAY).date(), time(16), ET),
         "paper exception before dated after-close window")
    need(after["MainPID"] == 0 and after["ActiveState"] == "inactive" and after["SubState"] == "dead"
         and after["Result"] == "success" and after["NRestarts"] == 0
         and after["ExecMainCode"] == 1 and after["ExecMainStatus"] == 0, "paper not clean expected inactive")
    stopped = system_time(after["InactiveEnterTimestamp"]).astimezone(ET)
    if scheduled_close is not None:
        need(scheduled_close["paper"] == after and scheduled_close["no_service_actions"] is True,
             "paper lifecycle pin differs during FLAGGATE")
        audit = scheduled_close["close_audit"]
        from release_policy import moment
        from paper_lifecycle import audit_in_stop_interval
        need(isinstance(audit, dict) and audit.get("action") == "stop_paper"
             and audit.get("reason") == "scheduled_session_close" and type(audit.get("systemctl_rc")) is int
             and audit["systemctl_rc"] == 0 and moment(audit["at_utc"]) <= now
             and audit_in_stop_interval(after, scheduled_close["guard"], moment(audit["at_utc"]))
             and system_time(scheduled_close["guard"]["InactiveEnterTimestamp"]) <= now
             and stopped.date().isoformat() == DAY and stopped.time().replace(tzinfo=None) >= time(9, 40),
             "paper stop not exact saved scheduled-close proof")
        return
    need(stopped.date().isoformat() == DAY and stopped.hour == 9 and stopped.minute == 40
         and 0 <= stopped.second <= 30, "paper stop not dated scheduled09:40")


def flag_result(rc, text, catalog, *, paper_before=None, paper_after=None, now=None, scheduled_close=None):
    final = re.findall(r"^Final call: (PASS|FAIL|UNKNOWN); checked=(\d+)/(\d+) mismatches=(\d+) unknown=(\d+)$", text, re.M)
    from release_policy import catalog_ids as identities
    total = len(identities(catalog))
    full = final == [("PASS", str(total), str(total), "0", "0")] and rc == 0
    paper_only = final == [("UNKNOWN", str(total - 2), str(total), "0", "2")] and rc == 2
    need(full or paper_only, "FLAGGATE unexpected verdict/population")
    rows = [line for line in text.splitlines() if line.startswith(("PASS ", "REAL FAILURE ", "UNKNOWN "))]
    need(len(rows) == total, "FLAGGATE population mismatch")
    found = [re.match(r"(?:PASS|UNKNOWN) flag=([^ ]+) service=([^ ]+) ", line) for line in rows]
    need(all(found), "FLAGGATE identities unreadable")
    actual = [(match[1], match[2]) for match in found]
    expected = {(row["name"], service) for row in catalog
                for service in (row["owning_service"], *row.get("also_check_services", []))}
    need(len(set(actual)) == len(actual) and set(actual) == expected, "FLAGGATE identities duplicate/missing/foreign")
    unknown = {identity for identity, row in zip(actual, rows) if row.startswith("UNKNOWN ")}
    if full:
        need(not unknown, "PASS verdict contradicts unknown rows")
    else:
        need(unknown == PAPER_FLAGS and all(row == "UNKNOWN flag=" + name + " service=momentum-paper reason=momentum-paper is not active"
             for (name, service), row in zip(actual, rows) if row.startswith("UNKNOWN ")), "unexpected paper UNKNOWN reason/identity")
        need(paper_before is not None and paper_after is not None and now is not None, "fresh paper inactivity proof absent")
        inactive_paper(paper_before, paper_after, now, scheduled_close)
    return dict(original_rc=rc, original_verdict=final[0][0], checked=int(final[0][1]), total=total,
                unknown=int(final[0][4]), coverage="expected-dated-inactive-paper" if paper_only else "all-measured",
                raw_sha256=digest(text.encode()), paper_before=paper_before, paper_after=paper_after)
