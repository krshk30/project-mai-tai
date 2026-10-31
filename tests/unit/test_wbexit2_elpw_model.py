"""UNRUN: two log-bound ELPW models through surviving controlled broker seam.

No actual native preimage. Fixture UTC steps are model inputs, not monotonic
timing measurements. The source proves no new historical uncovered interval.
"""
from datetime import datetime
from decimal import Decimal
import json

import pytest

from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from tests.unit.test_wbattach1_riskoff import make_case as make_case, closes
from tests.unit.test_wbexit2_route import ControlledOwnerBroker, owner_case, close_case, saved

# Literal reference below, not a private-path runtime dependency or source verification.
RAW_SHA = "cfeb0147ffb298e5f0296ce42e1a255efa60749d1aade138607421234564d760"


def at(time):
    return datetime.fromisoformat("2026-10-28T" + time + "+00:00")


class ElpwBroker(ControlledOwnerBroker):
    fast = False
    active = False
    later = False
    released_at = None
    sell_at = None

    def advance(self, stamp):
        self.clock[0] = max(self.clock[0], at(stamp))

    async def cancel_exit_pair(self, **binding):
        if self.active:
            self.advance("13:31:05.278")
            self.released_at = self.released_at or self.clock[0]
        return await super().cancel_exit_pair(**binding)

    async def release_exit_pair_for_close(self, **binding):
        # CONTROLLED original outer surface says terminal while LIST may lag.
        # ON-before-cancel native response is unretained, not inferred here.
        await self.cancel_exit_pair(**binding)
        return ExitPairReleaseResult(outcome="released", reports=tuple(
            self.report(WebullBrokerAdapter._combo_leg_coid(binding["base_client_order_id"], suffix))
            for suffix in ("T", "S")))

    async def confirm_exit_pair_from_today_list(self, **binding):
        if self.active and binding["base_client_order_id"] in self.cancelled:
            self.advance("13:31:05.778" if self.fast else "13:31:16.714" if self.later else "13:31:06.019")
            self.lag = not self.fast and not self.later
        return await super().confirm_exit_pair_from_today_list(**binding)

    async def submit_order(self, request):
        if self.active and request.metadata.get("webull_exit_only_pair") != "true":
            self.advance("13:31:05.778" if self.fast else "13:31:17.255")
            self.sell_at = self.clock[0]
        return await super().submit_order(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [False, True], ids=["H2-OFF", "WBEXIT2-ON"])
@pytest.mark.parametrize("fast", [False, True], ids=["reserved-then-terminal-MODELED", "terminal-inside1s-MODELED"])
async def test_ELPW_log_bound_models_no_new_working_after_release(make_case, record_property, switch, fast):
    case = await owner_case(make_case, switch=switch, symbol="ELPW", quantity="91", entry="3.2968",
        port_type=ElpwBroker, target_pct=5., stop_pct=8., at_clock=at("13:30:00"))
    broker = case[2]
    pair, = [r for r in broker.requests if r.metadata.get("webull_exit_only_pair") == "true"]
    assert WebullBrokerAdapter._round_to_tick(Decimal(pair.metadata["bracket_target_price"])) == Decimal("3.46")
    assert WebullBrokerAdapter._round_to_tick(Decimal(pair.metadata["bracket_stop_price"])) == Decimal("3.03")
    broker.clock, broker.active, broker.fast = case[4], True, fast
    broker.bid = 3.26
    case[4][0] = at("13:31:02.544")
    first = await close_case(case, "CW_FLIP")
    before = saved(case) if switch else None
    if not fast and switch:
        assert not closes(broker), "reserved original child is not authority to SELL"
        broker.later = True
        case[4][0] = at("13:31:16.536")
        second = await close_case(case, "CW_FLIP")
    else:
        second = None
    if switch:
        assert len(closes(broker)) == 1 and closes(broker)[0].quantity == Decimal(91)
        assert len(broker.cancelled) == 1 and broker.working_reads == 1
        assert broker.total == 1000 and saved(case)["phase"] == "RESOLVED"
        if not fast:
            assert saved(case)["originals"] == before["originals"]
            assert saved(case)["original_attempt_outcome"] == "FAIL"
    record_property("raw_reference_sha_not_reverified_here", RAW_SHA)
    record_property("native_ON_uncovered_seconds", "UNKNOWN_NO_NATIVE_RESPONSE_PREIMAGE")
    record_property("CONTROLLED_model", json.dumps(dict(fast=fast, switch=switch,
        first=first, second=second, model_release=str(broker.released_at), model_sell=str(broker.sell_at))))
