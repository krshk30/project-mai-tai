"""Future after-close SEC preparation; no implicit universe or contact defaults."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date
from pathlib import Path

from project_mai_tai.db.session import build_timed_session_factory
from project_mai_tai.momentum_live_sec import CountryStore, SecClient, SecError
from project_mai_tai.settings import get_settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--common-universe", type=Path, required=True)
    parser.add_argument("--lookup-day", type=date.fromisoformat, required=True)
    parser.add_argument("--max-requests", type=int, default=20_000)
    parser.add_argument("--max-seconds", type=float, default=3600)
    args = parser.parse_args()
    try:
        contact = os.environ.get("MAI_TAI_SEC_USER_AGENT", "")
        if not contact:
            raise SecError("verified_sec_contact_required")
        raw = args.common_universe.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise SecError("common_universe_bound")
        universe = json.loads(raw)
        if (
            not isinstance(universe, dict)
            or universe.get("security_type") != "COMMON"
            or not isinstance(universe.get("symbols"), list)
            or not isinstance(universe.get("provenance"), str)
        ):
            raise SecError("common_universe_provenance_required")
        store = CountryStore(
            build_timed_session_factory(get_settings(), service="momentum-live-sec")
        )
        client = SecClient(store, user_agent=contact)
        result = client.nightly(
            universe["symbols"],
            lookup_day=args.lookup_day,
            universe_provenance=universe["provenance"],
            max_requests=args.max_requests,
            max_seconds=args.max_seconds,
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "FULL" else 2
    except SecError as error:
        logging.getLogger(__name__).error("[MOMLIVE-SEC-PREPARE] status=refused reason=%s", error)
    except Exception:
        logging.getLogger(__name__).error("[MOMLIVE-SEC-PREPARE] status=unavailable")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
