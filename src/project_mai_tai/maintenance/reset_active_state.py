from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from project_mai_tai.db.models import AccountPosition, TradeIntent, VirtualPosition
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.settings import get_settings


async def _run() -> dict[str, object]:
    settings = get_settings()
    session_factory = build_session_factory(settings)
    store = OmsStore()
    oms = OmsRiskService(settings=settings, session_factory=session_factory, store=store)

    sync_summary = await oms.sync_broker_state()
    with session_factory() as session:
        active_accounts = store.list_active_broker_accounts(session)
        active_account_ids = [account.id for account in active_accounts]
        open_orders = store.list_open_orders(session, broker_account_ids=active_account_ids)
        broker_positions = session.scalars(
            select(AccountPosition).where(AccountPosition.quantity > 0)
        ).all()
        if open_orders:
            raise RuntimeError(
                f"refusing active-state reset with {len(open_orders)} open broker orders still tracked"
            )
        if broker_positions:
            raise RuntimeError(
                f"refusing active-state reset with {len(broker_positions)} broker-backed positions still open"
            )

        cleared_virtual_positions = store.clear_virtual_positions_without_account_backing(
            session,
            broker_account_ids=active_account_ids,
        )
        cancelled_stale_intents = 0
        for intent in session.scalars(
            select(TradeIntent).where(TradeIntent.status.in_(("pending", "submitted", "accepted")))
        ).all():
            intent.status = "cancelled"
            cancelled_stale_intents += 1

        session.commit()

    reconciler = ReconciliationService(settings=settings, session_factory=session_factory)
    reconciliation_result = reconciler.run_reconciliation_cycle()

    with session_factory() as session:
        remaining_virtual_positions = session.scalars(
            select(VirtualPosition).where(VirtualPosition.quantity > 0)
        ).all()
        remaining_account_positions = session.scalars(
            select(AccountPosition).where(AccountPosition.quantity > 0)
        ).all()
        remaining_open_orders = store.list_open_orders(session)

    return {
        "sync_summary": sync_summary,
        "cleared_virtual_positions": cleared_virtual_positions,
        "cancelled_stale_intents": cancelled_stale_intents,
        "reconciliation_summary": reconciliation_result["summary"],
        "remaining_virtual_positions": len(remaining_virtual_positions),
        "remaining_account_positions": len(remaining_account_positions),
        "remaining_open_orders": len(remaining_open_orders),
    }


def main() -> None:
    print(json.dumps(asyncio.run(_run())))


if __name__ == "__main__":
    main()
