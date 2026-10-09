"""Exit reads keep the original discriminator and hydrate only the selected order."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import desc, event, select

from project_mai_tai.db.models import BrokerOrder
from tests.unit import test_drift_cache_projection_equivalence as projection

lane = projection.lane
seed_order = projection.seed_order


def frozen_read(store, session, ids, *, native, include_native):
    orders = session.scalars(select(BrokerOrder).where(
        BrokerOrder.strategy_id == ids["strategy"],
        BrokerOrder.broker_account_id == ids["active"],
        BrokerOrder.symbol == "MI", BrokerOrder.side == "sell",
        BrokerOrder.status.in_(store.OPEN_ORDER_STATUSES),
    ).order_by(desc(BrokerOrder.updated_at))).all()
    for order in orders:
        guard = str((order.payload or {}).get("native_stop_guard", "")).strip().lower() == "true"
        if native and not guard or not native and not include_native and guard:
            continue
        return order
    return None


@pytest.mark.parametrize("value", ["true", " TRUE ", "\tTrUe\n", True, False, None, 1, "false", ["true"]])
@pytest.mark.parametrize("native,include_native", [(True, True), (False, False), (False, True)])
def test_exit_read_matches_discriminator_and_latest_order(lane, value, native, include_native):
    service, factory, ids = lane
    earlier = seed_order(lane, side="sell", intent_type="close", payload={"native_stop_guard": "true"})
    later = seed_order(lane, side="sell", intent_type="close", payload={"native_stop_guard": value})
    seed_order(lane, side="buy", payload={"native_stop_guard": "true"})
    seed_order(lane, side="sell", status="filled", payload={"native_stop_guard": "true"})
    with factory() as session:
        session.get(BrokerOrder, earlier).updated_at = datetime(2026, 10, 9, 17, tzinfo=UTC)
        session.get(BrokerOrder, later).updated_at = datetime(2026, 10, 9, 17, tzinfo=UTC) + timedelta(seconds=1)
        session.commit()
    with factory() as session:
        expected = frozen_read(service.store, session, ids, native=native, include_native=include_native)
        expected_id = expected.id if expected else None
    loaded = []
    def capture(_session, instance):
        loaded.append(instance.id)
    event.listen(factory, "loaded_as_persistent", capture)
    try:
        with factory() as session:
            args = dict(strategy_id=ids["strategy"], broker_account_id=ids["active"], symbol="MI")
            actual = (service.store.find_open_native_stop_guard_order(session, **args) if native else
                      service.store.find_open_exit_order(session, **args, include_native_stop_guard=include_native))
            assert (actual.id if actual else None) == expected_id
            assert loaded == ([] if actual is None else [actual.id])
    finally:
        event.remove(factory, "loaded_as_persistent", capture)


def test_exit_read_preserves_in_transaction_payload_and_identity(lane):
    service, factory, ids = lane
    order_id = seed_order(lane, side="sell", payload={"native_stop_guard": "false"})
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        order.payload = {"native_stop_guard": "true"}
        actual = service.store.find_open_native_stop_guard_order(session,
            strategy_id=ids["strategy"], broker_account_id=ids["active"], symbol="MI")
        assert actual is order
        session.rollback()


def test_native_exit_without_a_guard_hydrates_no_orders(lane):
    service, factory, ids = lane
    for _ in range(25):
        seed_order(lane, side="sell", payload={"native_stop_guard": "false"})
    with factory() as session:
        assert service.store.find_open_native_stop_guard_order(session,
            strategy_id=ids["strategy"], broker_account_id=ids["active"], symbol="MI") is None
        assert not session.identity_map
