"""Offline recorded-price replay through the OMS decorator and adapter formatters.

Original decision-cache timing and counterfactual fills are not reconstructed.
"""

from copy import deepcopy
from decimal import Decimal
import json
import logging
from pathlib import Path

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.v2_entry_sizing import resting_buy_limit, resting_buy_stop, sized_entry_quantity

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/roundup1/orders_179.json"


def service(enabled=False):
    svc = object.__new__(OmsRiskService)
    svc.settings = Settings(_env_file=None, oms_v2_emit_native_oco_bracket_enabled=True,
        strategy_schwab_1m_v2_resting_buy_round_up_enabled=enabled)
    svc._cw_target_pct, svc._cw_stop_pct = 5.0, 8.0
    svc.logger = logging.getLogger("roundup1-replay")
    return svc


def replay(row, *, enabled=False):
    md = deepcopy(row["payload"])
    leg = "webull" if row["account"] == "live:orb" else "schwab"
    raw_stop = Decimal(md.get("entry_price") or md["stop_price"])
    is_limit = row["order_type"].lower() == "limit"
    qty = int(Decimal(row["quantity"]))
    stop = resting_buy_stop(raw_stop) if enabled else Decimal(md["stop_price"])
    if enabled:
        cap = resting_buy_limit(stop, float(md["resting_band_pct"]), leg=leg)
        md.update(stop_price=f"{stop:.4f}", entry_price=f"{stop:.4f}", reference_price=f"{stop:.4f}",
                  resting_wire_stop_price=f"{stop:.4f}", resting_wire_limit_price=str(cap),
                  resting_buy_round_up="true")
        if is_limit:
            # A PM order's actual wire is an ask-priced LIMIT, not its arming cap.
            # Retain the recorded OMS ask; crossing eligibility is replayed separately.
            svc = service(True)
            svc._fresh_ask = lambda symbol, max_age_ms: float(md["oms_v2_eh_resting_entry_ask"])
            limit, _, _ = svc._band_capped_marketable_limit(symbol=row["symbol"], level=float(stop),
                band_pct=float(md["resting_band_pct"]), max_age_ms=2000, wire_cap=cap)
            if limit is None:
                return {"refused": "ASK_PAST_BAND", "trigger": str(stop), "cap": str(cap)}
            md["limit_price"] = limit
        else:
            md["limit_price"] = str(cap)
    event = TradeIntentEvent(source_service="roundup1-offline-replay", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=row["account"], symbol=row["symbol"],
        side="buy", intent_type="open", quantity=Decimal(qty), reason="ATR Flip", metadata=md))
    if not is_limit:
        service(enabled)._apply_v2_oco_bracket_entry(event=event)
    md = event.payload.metadata
    request = OrderRequest(client_order_id=row["client_order_id"], broker_account_name=row["account"],
        strategy_code="schwab_1m_v2", symbol=row["symbol"], side="buy", intent_type="open",
        quantity=Decimal(qty), reason="ATR Flip", order_type=row["order_type"], metadata=md)
    if leg == "webull":
        wire_limit, wire_stop, _, refusal = WebullBrokerAdapter._prepare_single_leg_prices(
            request=request, order_type="LIMIT" if is_limit else "STOP_LIMIT",
            limit_price=Decimal(md["limit_price"]), stop_price=None if is_limit else Decimal(md["stop_price"]))
        assert refusal is None
    else:
        payload = object.__new__(SchwabBrokerAdapter)._build_order_payload(request)
        wire_limit = Decimal(str(payload["price"]))
        wire_stop = Decimal(str(payload["stopPrice"])) if "stopPrice" in payload else None
    if enabled:
        qty = sized_entry_quantity(Decimal(md.get("entry_notional_target_usd", "0")), wire_limit, qty, 1000)
    return {"trigger": str(stop) if enabled else str(raw_stop), "stop": str(wire_stop) if wire_stop else "N/A (LIMIT)",
            "limit": str(wire_limit), "shares": qty,
            "target": md.get("bracket_target_price", "N/A"), "protection": md.get("bracket_stop_price", "N/A"),
            "metadata": md}


def main():
    from project_mai_tai.oms import service as oms
    oms._is_regular_market_session = lambda now=None: True  # STOP_LIMIT rows are recorded RTH.
    raw = json.loads(FIXTURE.read_text())
    results = [{"id": row["id"], "client_order_id": row["client_order_id"], "symbol": row["symbol"],
                "account": row["account"], "order_type": row["order_type"], "status": row["status"],
                "old": replay(row), "new": replay(row, enabled=True)} for row in raw["queries"]["orders"]]
    print(json.dumps({"label": "recorded four-decimal entry input; wire/quantity/5%-8% geometry replay, NOT fills",
                      "read_at": raw["read_at"], "rows": results}, indent=2))


if __name__ == "__main__":
    main()
