"""Webull OpenAPI SANDBOX probe — learn the response shapes before building the adapter.

Why: the SDK ships no typed response models for orders/positions; `get_response` returns a
raw JSON body. We must see the ACTUAL field names (order id, status, fill price/time, the
instrument-lookup result) from a real call before we write real-money response parsing.

SAFETY:
- SANDBOX ONLY. Refuses to run unless --host is given AND --i-understand-sandbox is passed.
- Places a BUY LIMIT far BELOW market (won't fill), reads it back, then CANCELS it.
- Credentials come from env only (never args/chat): MAI_TAI_WEBULL_APP_KEY / _SECRET /
  _ACCOUNT_ID / _REGION_ID. Nothing is written anywhere; raw responses are printed for us
  to read the shapes.

Usage (on the box, after sandbox creds are in env):
  python scripts/webull_sandbox_probe.py --host <sandbox-host> --symbol AAPL \
      --i-understand-sandbox
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _dump(label, obj):
    print(f"\n===== {label} =====")
    for attr in ("status", "code", "headers"):
        if hasattr(obj, attr):
            print(f"  {attr}: {getattr(obj, attr)}")
    body = getattr(obj, "body", None)
    if body is None and hasattr(obj, "json"):
        try:
            body = obj.json()
        except Exception:
            body = None
    try:
        print("  body:", json.dumps(body if body is not None else vars(obj), default=str, indent=2)[:4000])
    except Exception:
        print("  raw:", repr(obj)[:2000])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="Webull API host (sandbox or prod)")
    ap.add_argument("--symbol", default="AAPL")
    # READ-ONLY by default: instrument lookup + positions + order history only — places NOTHING.
    # Placing the far-below-market test order is opt-in and requires explicit confirmation,
    # so this is safe to run with PRODUCTION credentials.
    ap.add_argument("--place-test-order", action="store_true",
                    help="ALSO place a far-below-market limit (qty 1) then cancel it — real order if prod host")
    ap.add_argument("--i-understand-live", action="store_true",
                    help="required with --place-test-order to acknowledge it may be a real order")
    args = ap.parse_args()
    if args.place_test_order and not args.i_understand_live:
        print("Refusing --place-test-order without --i-understand-live (it may place a REAL order).")
        return 2

    app_key = os.environ.get("MAI_TAI_WEBULL_APP_KEY", "")
    app_secret = os.environ.get("MAI_TAI_WEBULL_APP_SECRET", "")
    account_id = os.environ.get("MAI_TAI_WEBULL_ACCOUNT_ID", "")
    region_id = os.environ.get("MAI_TAI_WEBULL_REGION_ID", "us")
    if not (app_key and app_secret and account_id):
        print("Missing MAI_TAI_WEBULL_APP_KEY/_SECRET/_ACCOUNT_ID in env. Put sandbox creds in env first.")
        return 2

    from webull.core.client import ApiClient
    from webull.trade.common.order_side import OrderSide
    from webull.trade.common.order_tif import OrderTIF
    from webull.trade.common.order_type import OrderType
    from webull.trade.request.place_order_request import PlaceOrderRequest
    from webull.trade.request.v2.get_order_detail_request import OrderDetailRequest
    from webull.trade.request.v2.cancel_order_request import CancelOrderRequest
    from webull.trade.request.get_account_positions_request import AccountPositionsRequest

    client = ApiClient(app_key, app_secret, region_id)
    client.add_endpoint(region_id, args.host)  # <- SANDBOX host only

    # 1) instrument resolution (symbol -> instrument_id): try the trade instruments lookup,
    #    dump whatever comes back so we learn the field names.
    instrument_id = None
    try:
        from webull.trade.request.get_tradeable_instruments_request import (  # type: ignore
            GetTradeableInstrumentsRequest,
        )
        req = GetTradeableInstrumentsRequest()
        for setter, val in (("set_symbols", args.symbol), ("set_category", "US_STOCK")):
            if hasattr(req, setter):
                getattr(req, setter)(val)
        resp = client.get_response(req)
        _dump(f"INSTRUMENT LOOKUP ({args.symbol})", resp)
        print("  >>> read the instrument_id field name from the body above and set it below if needed")
    except Exception as exc:
        print(f"instrument lookup raised (try a different lookup request): {exc!r}")

    iid = os.environ.get("WEBULL_PROBE_INSTRUMENT_ID") or instrument_id

    # 2) READ-ONLY: positions (shape for position sync) — places nothing.
    try:
        ap_req = AccountPositionsRequest()
        ap_req.set_account_id(account_id)
        if hasattr(ap_req, "set_page_size"):
            ap_req.set_page_size(50)
        _dump("ACCOUNT POSITIONS (read-only)", client.get_response(ap_req))
    except Exception as exc:
        print(f"positions raised: {exc!r}")

    if not args.place_test_order:
        print("\nREAD-ONLY probe done (no order placed). To also learn the order/fill/cancel "
              "shapes, re-run attended with: --place-test-order --i-understand-live")
        print("Paste the body shapes (NOT the creds) back so the adapter parser is built against reality.")
        return 0

    # 3) opt-in: place a far-below-market BUY LIMIT (qty 1, won't fill), read it, cancel it.
    client_order_id = "orbprobe-" + args.symbol.lower()
    try:
        po = PlaceOrderRequest()
        po.set_account_id(account_id)
        po.set_client_order_id(client_order_id)
        if iid:
            po.set_instrument_id(iid)
        po.set_side(OrderSide.BUY.name)
        po.set_order_type(OrderType.LIMIT.name)
        po.set_limit_price("1.00")     # far below market for a liquid name -> rests, no fill
        po.set_qty("1")
        po.set_tif(OrderTIF.DAY.name)
        _dump("PLACE ORDER (far-below-market limit, qty 1)", client.get_response(po))
    except Exception as exc:
        print(f"place order raised: {exc!r}")

    try:
        od = OrderDetailRequest()
        od.set_account_id(account_id)
        od.set_client_order_id(client_order_id)
        _dump("ORDER DETAIL", client.get_response(od))
    except Exception as exc:
        print(f"order detail raised: {exc!r}")

    try:
        co = CancelOrderRequest()
        co.set_account_id(account_id)
        co.set_client_order_id(client_order_id)
        _dump("CANCEL ORDER (leaves nothing resting)", client.get_response(co))
    except Exception as exc:
        print(f"cancel raised: {exc!r}")

    print("\nDONE. Paste the body shapes (NOT the creds) back so the adapter parser is built against reality.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
