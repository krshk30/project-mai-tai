"""Exercise the real OMS decorator offline on the recorded PFSA entry."""

import argparse
import json
import logging
from copy import deepcopy
from decimal import Decimal

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import service
from project_mai_tai.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture")
    args = parser.parse_args()
    with open(args.capture) as handle:
        capture = json.load(handle)
    order = next(row for row in capture["queries"]["orders"]
                 if row["client_order_id"] == "schwab_1m_v2-PFSA-open-7f6f022b3c50")
    risk = service.OmsRiskService.__new__(service.OmsRiskService)
    risk.settings = Settings(oms_v2_emit_native_oco_bracket_enabled=True)
    risk.logger = logging.getLogger("roundup1-offline-seam")
    risk._cw_target_pct = 5.0
    risk._cw_stop_pct = 8.0
    original_session = service._is_regular_market_session
    service._is_regular_market_session = lambda now=None: True
    output = {}
    try:
        for mode in ("recorded", "naive_single_seam"):
            md = deepcopy(order["payload"])
            if mode == "naive_single_seam":
                md.update(stop_price="3.7800", entry_price="3.7800",
                          reference_price="3.7800", limit_price="3.7989")
            event = TradeIntentEvent(source_service="roundup1-offline-assessment",
                payload=TradeIntentPayload(strategy_code="schwab_1m_v2",
                    broker_account_name=order["account"], symbol=order["symbol"],
                    side="buy", intent_type="open", quantity=Decimal(order["quantity"]),
                    metadata=md, reason="recorded PFSA seam assessment"))
            risk._apply_v2_oco_bracket_entry(event=event)
            output[mode] = {key: event.payload.metadata[key] for key in (
                "stop_price", "limit_price", "bracket_target_price", "bracket_stop_price")}
    finally:
        service._is_regular_market_session = original_session
    assert output["recorded"] == {
        "stop_price": "3.77", "limit_price": "3.79",
        "bracket_target_price": "3.96", "bracket_stop_price": "3.47",
    }
    assert output["naive_single_seam"] == {
        "stop_price": "3.78", "limit_price": "3.80",
        "bracket_target_price": "3.97", "bracket_stop_price": "3.48",
    }
    print(json.dumps({"source": service.__file__, "result": output,
                      "verdict": "single-seam-only changes exit prices"}, indent=2))


if __name__ == "__main__":
    main()
