"""Future after-close SEC preparation; no implicit universe or contact defaults."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date

from project_mai_tai.db.session import build_timed_session_factory
from project_mai_tai.momentum_live_sec import CountryStore, SecClient, SecError, validate_sec_cohort
from project_mai_tai.settings import get_settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cohort", required=True)
    parser.add_argument("--security-type", required=True)
    parser.add_argument("--lookup-day", type=date.fromisoformat, required=True)
    parser.add_argument("--max-requests", type=int, default=20_000)
    parser.add_argument("--max-seconds", type=float, default=3600)
    args = parser.parse_args()
    try:
        validate_sec_cohort(args.cohort, args.security_type)
        contact = os.environ.get("MAI_TAI_SEC_USER_AGENT", "")
        if not contact:
            raise SecError("verified_sec_contact_required")
        store = CountryStore(
            build_timed_session_factory(get_settings(), service="momentum-live-sec")
        )
        client = SecClient(store, user_agent=contact)
        result = client.nightly(
            lookup_day=args.lookup_day,
            cohort=args.cohort,
            security_type=args.security_type,
            max_requests=args.max_requests,
            max_seconds=args.max_seconds,
        )
        print(json.dumps(result, sort_keys=True))
        return (
            0
            if result["status"] == "FULL"
            and not any(result["http_errors"].values())
            and not result["counts"]["FETCH_FAILED"]
            else 2
        )
    except SecError as error:
        logging.getLogger(__name__).error("[MOMLIVE-SEC-PREPARE] status=refused reason=%s", error)
    except Exception:
        logging.getLogger(__name__).error("[MOMLIVE-SEC-PREPARE] status=unavailable")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
