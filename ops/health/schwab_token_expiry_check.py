#!/usr/bin/env python3
"""Schwab refresh-token expiry check — reads the token store from OUTSIDE any
service and verdicts on how much life is left on the ~7-day refresh_token clock.

The refresh_token is non-rotating: routine access-token refreshes do NOT reset it,
so once it dies every Schwab call fails `invalid_grant` and a human must re-auth at
/auth/schwab/start. This check gives the heads-up that nothing else does.

Reads `refresh_token_expires_at` from the token store (persisted by the OAuth
callback since the 2026-07-14 capture change). Stdlib only; shares nothing with the
services.

Env:
  MAI_TAI_SCHWAB_TOKEN_STORE_PATH  token store path (default the VPS location)
  SCHWAB_TOKEN_SIMULATE_EXPIRES_AT  ISO ts to force (self-test only)

Exit / verdict (one 'VERDICT: <LEVEL> <detail>' line):
  0 GREEN  > 48h left
  1 AMBER  <= 48h left, OR expiry unknown (pre-capture token)
  2 RED    <= 12h left, or already expired
"""
import json
import os
import sys
from datetime import datetime, timezone

UTC = timezone.utc
DEFAULT_PATH = "/var/lib/macd-webhook-server/data/schwab_tokens.json"
RED_HOURS = 12.0
AMBER_HOURS = 48.0


def _parse_ts(value):
    if value in (None, ""):
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def verdict(refresh_token_expires_at, now):
    """Return (exit_code, level, detail). Pure — unit-tested."""
    if refresh_token_expires_at is None:
        return (
            1,
            "AMBER",
            "Schwab refresh_token expiry UNKNOWN (pre-capture token) — "
            "re-auth at /auth/schwab/start to start tracking",
        )
    hours = (refresh_token_expires_at - now).total_seconds() / 3600.0
    if hours <= 0:
        return (2, "RED", f"Schwab refresh_token EXPIRED ~{abs(hours):.0f}h ago — re-auth NOW at /auth/schwab/start")
    if hours <= RED_HOURS:
        return (2, "RED", f"Schwab refresh_token expires in ~{hours:.0f}h — re-auth NOW at /auth/schwab/start")
    if hours <= AMBER_HOURS:
        return (1, "AMBER", f"Schwab refresh_token expires in ~{hours:.0f}h — re-auth soon at /auth/schwab/start")
    return (0, "GREEN", f"Schwab refresh_token healthy — ~{hours / 24.0:.1f}d left")


def main():
    now = datetime.now(UTC)
    forced = os.environ.get("SCHWAB_TOKEN_SIMULATE_EXPIRES_AT")
    if forced:
        rt_exp = _parse_ts(forced)
    else:
        path = os.environ.get("MAI_TAI_SCHWAB_TOKEN_STORE_PATH", DEFAULT_PATH)
        try:
            store = json.loads(open(path, encoding="utf-8").read())
        except Exception as exc:  # missing/unreadable store == can't assess
            print(f"VERDICT: RED cannot read token store {path}: {exc}")
            return 2
        rt_exp = _parse_ts(store.get("refresh_token_expires_at"))
    code, level, detail = verdict(rt_exp, now)
    if rt_exp is not None:
        print(f"refresh_token_expires_at: {rt_exp.isoformat(timespec='seconds')} (now {now.isoformat(timespec='seconds')})")
    print(f"VERDICT: {level} {detail}")
    return code


if __name__ == "__main__":
    sys.exit(main())
