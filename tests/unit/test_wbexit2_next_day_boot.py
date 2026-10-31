"""SOURCE UNRUN -- exact component driver; not a native/full-service boot PASS."""
import json

import pytest

from tests.unit.test_wbattach1_riskoff import make_case as make_case
from tests.wbexit2_next_day_boot_replay import replay_next_day
from project_mai_tai.oms import wbexit2
from tests.unit.test_wbexit2_route import owner_case, close_case, saved


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["original_terminal", "durable_terminal", "durable_terminal_corrupt",
                                    "orphan_nonce", "replacement_working",
                                    "entry_time_changed", "no_prior_owner"])
async def test_next_day_durable_close_obligation(make_case, record_property, scenario):
    receipt = await replay_next_day(make_case, scenario=scenario)
    assert receipt["native_provider"] == "ABSENT_UNKNOWN"
    assert receipt["actual_process_restart"] == "NOT_COVERED"
    if scenario in {"original_terminal", "durable_terminal"}:
        assert receipt["final_phase"] == "RESOLVED" and receipt["sell_count"] == 1
    else:
        assert receipt["sell_count"] == 0
    record_property("wbexit2_next_day_component_receipt", json.dumps(receipt, sort_keys=True))


@pytest.mark.asyncio
async def test_boot_inventory_overflow_never_publishes_partial_ready(make_case, monkeypatch):
    case = await owner_case(make_case)
    case[2].lag = True
    await close_case(case)
    owner = saved(case)
    before = len(case[2].requests), len(case[2].cancelled), case[2].positions_reads
    # Exercise the EXACT overflow branch with one real durable row and a
    # smaller test cap; do not fabricate 257 model identities or provider data.
    monkeypatch.setattr(wbexit2, "BOOT_OWNER_LIMIT", 0)
    with pytest.raises(ValueError, match="inventory_over_bound"):
        await wbexit2.hydrate_at_boot(case[0])
    assert case[0]._wbexit2_boot_complete is False
    assert saved(case)["owner_id"] == owner["owner_id"]
    assert before == (len(case[2].requests), len(case[2].cancelled), case[2].positions_reads)


@pytest.mark.asyncio
async def test_flag_OFF_boot_hook_has_no_DB_or_provider_operation(make_case):
    case = await owner_case(make_case, switch=False)
    def forbidden_factory():
        raise AssertionError("flag OFF must not read a WBEXIT2 inventory")
    case[0].session_factory = forbidden_factory
    before = len(case[2].requests), len(case[2].cancelled), case[2].positions_reads
    assert await wbexit2.hydrate_at_boot(case[0]) == {"status": "OFF", "owners": []}
    assert before == (len(case[2].requests), len(case[2].cancelled), case[2].positions_reads)
