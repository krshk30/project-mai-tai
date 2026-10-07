"""Install-only ticket admission; never changes runtime ownership or ticket rows."""
import json

IN_FLIGHT = frozenset({"requested", "price_wait", "submitting"})
PHASES = IN_FLIGHT | {"prepared", "waiting", "fills_waiting", "clear", "held_unknown",
                      "submit_unknown", "placed", "refused", "expired", "filled"}
ACCOUNTS = {"live:schwab_1m_v2", "live:orb"}
MAX_BYTES = 4_000_000


def inventory(rows):
    if len(json.dumps(rows, default=str).encode()) > MAX_BYTES:
        raise ValueError("ticket evidence exceeds bounded capture bytes")
    seen, counts, summary = set(), {}, []
    for row in rows:
        token, job = str(row["id"]), row["payload"]
        if token in seen or not isinstance(job, dict):
            raise ValueError("duplicate or unreadable ticket")
        seen.add(token)
        old, phase = job.get("old"), job.get("phase")
        if (not isinstance(old, dict) or old.get("broker_account_name") not in ACCOUNTS
                or not isinstance(old.get("symbol"), str) or not old["symbol"]
                or old.get("strategy_code") != "schwab_1m_v2" or old.get("side") != "buy"
                or job.get("slot") not in {"first", "reclaim"} or phase not in PHASES
                or not isinstance(old.get("metadata"), dict) or not old.get("client_order_id")
                or type(job.get("revision")) is not int):
            raise ValueError("untested or unreadable ticket shape: " + token)
        counts[phase] = counts.get(phase, 0) + 1
        summary.append(dict(token=token, account=old["broker_account_name"], symbol=old["symbol"],
                            phase=phase, reason=job.get("reason"), slot=job["slot"]))
    return dict(total=len(rows), phases=counts, tickets=summary,
                in_flight=[row for row in summary if row["phase"] in IN_FLIGHT])


def require_idle(rows):
    result = inventory(rows)
    if result["in_flight"]:
        raise ValueError("requested/price_wait/submitting ticket present")
    return result


def stable_bindings(before, after):
    require_idle(before)
    require_idle(after)
    left = {str(row["id"]): row["payload"] for row in before}
    right = {str(row["id"]): row["payload"] for row in after}
    if left.keys() != right.keys():
        raise ValueError("ticket identities changed during install")
    for token, old in left.items():
        new = right[token]
        for key in ("old", "slot", "segment_id"):
            if old.get(key) != new.get(key):
                raise ValueError("ticket binding changed: " + token + " " + key)
        if old.get("replacement") != new.get("replacement"):
            raise ValueError("ticket replacement changed during install: " + token)
        if new["revision"] < old["revision"]:
            raise ValueError("ticket revision moved backwards: " + token)
        # Accounting/restoration may finish existing tickets, never revive a finished one.
        if old["phase"] in {"refused", "expired", "filled"} and new["phase"] not in {"refused", "expired", "filled"}:
            raise ValueError("finished ticket revived: " + token)
    return inventory(after)
