#!/usr/bin/env python3
"""ALERTS1: READ-ONLY review of open system incidents with each symbol's live exposure.

For every open/acknowledged incident that names a symbol, print the symbol's current broker
positions (account_positions), live-book rows (virtual_positions, oms_managed_positions),
working broker orders and active intents on live:schwab_1m_v2 (Schwab) and live:orb (Webull),
and whether the ALERTS1 reconciler rule would auto-resolve it. Resolves nothing.

Run on the box from the repo root with the service environment loaded:
    PYTHONPATH=src .venv/bin/python scripts/alerts1_open_incident_review.py [--json] [--all]

By default only the two ALERTS1 sources are listed; --all lists every open incident with a symbol.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import re
import sys

from sqlalchemy import select, text

from project_mai_tai.db.models import SystemIncident
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.reconciliation.flat_incidents import (
    ACTIVE_INCIDENT_STATUSES,
    AUTO_RESOLVE_SOURCES,
    _old_enough,
    symbol_exposure,
)
from project_mai_tai.settings import get_settings

_TITLE_SYMBOL = re.compile(r":\s*([A-Z][A-Z0-9.]{0,9})\b")


def _symbol(incident: SystemIncident) -> str:
    payload = incident.payload or {}
    symbol = str(payload.get("symbol") or "").strip().upper()
    if symbol:
        return symbol
    match = _TITLE_SYMBOL.search(incident.title or "")
    return match.group(1) if match else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="ALERTS1 read-only open-incident review")
    ap.add_argument("--json", action="store_true", help="print one JSON document")
    ap.add_argument("--all", action="store_true", help="every open incident with a symbol")
    args = ap.parse_args()

    now = datetime.now(UTC)
    rows: list[dict] = []
    session_factory = build_session_factory(get_settings())
    with session_factory() as session:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        incidents = session.scalars(
            select(SystemIncident)
            .where(SystemIncident.status.in_(ACTIVE_INCIDENT_STATUSES))
            .order_by(SystemIncident.opened_at)
        ).all()
        for incident in incidents:
            payload = incident.payload or {}
            source = str(payload.get("source") or "")
            if not args.all and source not in AUTO_RESOLVE_SOURCES:
                continue
            symbol = _symbol(incident)
            if not symbol:
                continue
            exposure = symbol_exposure(session, symbol)
            eligible = source in AUTO_RESOLVE_SOURCES
            rows.append(
                {
                    "incident_id": str(incident.id),
                    "service": incident.service_name,
                    "source": source or None,
                    "title": incident.title,
                    "opened_at": incident.opened_at.isoformat() if incident.opened_at else None,
                    "session_date": payload.get("session_date"),
                    "symbol": symbol,
                    "exposure": exposure,
                    "would_auto_resolve": bool(
                        eligible and exposure["flat"] and _old_enough(incident, now)
                    ),
                }
            )
        session.rollback()

    if args.json:
        print(json.dumps({"checked_at": now.isoformat(), "incidents": rows}, indent=2))
        return 0
    print(f"ALERTS1 open-incident review (read-only) at {now.isoformat()} — {len(rows)} incident(s)")
    for row in rows:
        exp = row["exposure"]
        print(
            f"\n{row['opened_at']}  {row['symbol']:<6} {row['service']}  {row['title']}\n"
            f"  id={row['incident_id']} source={row['source']} session_date={row['session_date']}\n"
            f"  FLAT={exp['flat']}  would_auto_resolve={row['would_auto_resolve']}\n"
            f"  broker_positions={exp['broker_positions']}\n"
            f"  virtual={exp['virtual_positions']} managed={exp['managed_positions']}\n"
            f"  working_orders={exp['working_orders']}\n"
            f"  active_intents={exp['active_intents']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
