"""UNRUN: keep the original surviving confirmation assertions intact.

Only the fixture Settings gain WBEXIT2/KEEPWATCH/BROKERDECIDES true. No APP
owner/quantity/permission function is replaced by this successor. The original
tests retain their pre-existing controlled broker/clock seams and limitations.
"""

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
import pytest
from sqlalchemy import select

from project_mai_tai.db.models import SystemIncident
from tests.unit import test_confirmation_exit_fanout as original
from tests.unit.test_confirmation_exit_fanout import _inject_regular_session as _inject_regular_session


@pytest.mark.asyncio
@pytest.mark.parametrize("case_name", ["normal-Schwab", "PM-bare-software", "PM-pair-unchanged-refusal"])
async def test_existing_confirmation_neighbours_with_WBEXIT2_ON(monkeypatch, record_property, case_name):
    factory = original._service
    made = []
    def configured(*args, **kwargs):
        service, sf = factory(*args, **kwargs)
        for field in ("strategy_schwab_1m_v2_wbexit2_enabled", "strategy_schwab_1m_v2_keepwatch1_enabled",
                      "strategy_schwab_1m_v2_brokerdecides1_enabled"):
            assert field in type(service.settings).model_fields
            object.__setattr__(service.settings, field, True)
        made.append((service, sf))
        return service, sf
    monkeypatch.setattr(original, "_service", configured)  # Existing fixture factory only.
    if case_name == "normal-Schwab":
        await original.test_controlled_fanout_flag_pair_closes_only_managed_accounts(
            monkeypatch, False, [original.SCHWAB])
    elif case_name == "PM-bare-software":
        await original.test_bare_webull_position_can_close_outside_rth_without_releasing_a_pair(monkeypatch)
    else:
        await original.test_webull_pair_is_not_released_outside_rth_and_schwab_still_closes(monkeypatch)
    assert made
    for service, sf in made:
        with sf() as session:
            assert session.scalar(select(SystemIncident).where(SystemIncident.payload["source"].as_string() == "wbexit2")) is None
        assert service.settings.strategy_schwab_1m_v2_wbexit2_enabled is True
    record_property("SPRC_0753_actual_PM_native_chronology", "NOT_COVERED_EXISTING_CONTROLLED_NEIGHBOUR_ONLY")
