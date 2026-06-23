# Webull live broker adapter for `live:orb` — design (sandbox-first)

**Status:** design + sandbox probe. No real-money cutover, no restart, no credentials in
repo/chat. Credentials live only in the box's env (secret store). Build the real adapter
**after** the sandbox probe confirms response shapes.

## Goal
Give ORB a **dedicated real-money account** (operator's Webull account) — NOT sharing
v2's live Schwab — so the reclaim-entry live test (PR #363) can measure **real** fills.

## Current state (verified)
`broker_adapters/webull.py` is **scaffolding only**: `submit_order` unconditionally returns
`rejected` (even fully credentialed → "official order submission is not implemented yet");
`fetch_order_update`/`list_account_positions` are no-ops. Settings fields and `webull`
provider routing exist. **The order path must be built; there is nothing to revive.**

## Webull OpenAPI (verified by installing `webull-openapi-python-sdk` 2.0.11)
- **Live US equity orders ARE supported** for individual accounts (apply + ~1–2 day approval).
  The old "Webull has no API" note (`settings.py`) is outdated.
- **Auth:** `webull.core.client.ApiClient(app_key, app_secret, region_id, ...)`; execute via
  `api_client.get_response(request)`. **Synchronous (requests-based)** → wrap every call in
  `asyncio.to_thread` (ORB/OMS are async; must not block the loop).
- **Place order:** `webull.trade.request.place_order_request.PlaceOrderRequest`
  → POST `/trade/order/place` (v2). Setters: `set_account_id`, `set_client_order_id`,
  **`set_instrument_id`**, `set_side`, `set_order_type`, `set_limit_price`, `set_stop_price`,
  `set_qty`, `set_tif`, `set_extended_hours_trading`.
- **Enums:** `OrderSide.BUY/SELL/SHORT`; `OrderType.MARKET/LIMIT/…`; `OrderTIF.DAY/GTC/IOC`;
  `OrderStatus.SUBMITTED/CANCELLED/FAILED/FILLED/PARTIAL_FILLED`.
- **Order detail:** `OrderDetailRequest(set_account_id, set_client_order_id)`.
- **Positions:** `AccountPositionsRequest(set_account_id, set_last_instrument_id, set_page_size)` — paginated.
- **Cancel:** `CancelOrderRequest(set_account_id, set_client_order_id)`.
- **Instrument id REQUIRED:** orders take `instrument_id`, not a bare symbol → need a
  symbol→instrument_id resolution step (`get_instruments_request` / instrument lookup) + cache.

## Architecture (the adapter to build)
1. **Lazy client** from env creds (`webull_app_key/secret/region_id`), endpoint host from
   `webull_base_url` (sandbox vs prod). If creds absent → reject (today's safe fallback).
2. **Instrument cache:** `symbol → instrument_id` (resolve once, cache; refresh on miss).
3. **`submit_order`** → `to_thread`: resolve instrument_id; build PlaceOrderRequest
   (side/type=LIMIT/limit_price=OR_high/qty=5/tif=DAY/client_order_id); `get_response`; map
   body → `ExecutionReport(accepted|filled|rejected, broker_order_id, fill price)`. **Sells
   too** — the OMS 3% trail fires a protective SELL; the adapter must place it.
4. **`fetch_order_update`** → OrderDetailRequest → map status (`OrderStatus`) + **fill price/time**
   → ExecutionReport (this is what records to the `fills` table → feeds the slippage script).
5. **`list_account_positions`** → AccountPositionsRequest (paginate) → BrokerPositionSnapshot
   (so the OMS knows the live position exists — required for the trailing stop + flatten).
6. **`cancel`** → CancelOrderRequest.
7. **Account registration:** map ORB account name (`live:orb`) → `webull_account_id`.
8. **Provider wiring (config only):** ORB env `provider=webull`, `account_name=live:orb`.
   Routing already supports `webull`.

## The unverified boundary (why probe first)
`get_response` returns a **raw JSON body**; the SDK ships **no typed response models** for
orders/positions. So the exact field names for order-id, status, fill price/time, and the
instrument-lookup result are **unknown until a real sandbox call**. We will NOT hard-code
guessed field names on a real-money path. `scripts/webull_sandbox_probe.py` (sandbox creds
only) resolves an instrument, places a far-from-market limit (won't fill), reads the order
detail, lists positions, and cancels — **dumping every raw response** so the parser is built
against reality. Also confirms the **sandbox host** and the instrument-lookup shape.

## Validation gates before real money (attended, explicit GO)
- Sandbox: instrument resolve, place/cancel limit, order-detail fill parse, position sync — all green.
- Reclaim path proven independently on `simulated` (PR #363) — don't debut two unknowns at once.
- Real-money safety gate: reclaim emits → **trail attaches on fill** → kill-switch/flatten on a
  real Webull position. qty 5. First real open only after all green.

## Gaps / dependencies
- New dep `webull-openapi-python-sdk` (pulls grpcio/paho-mqtt/cryptography — vet).
- Operator's Webull account must be OpenAPI-approved (external, ~1–2 days).
- Sandbox host + response field names — resolved by the probe.
- Credentials: env only (`MAI_TAI_WEBULL_APP_KEY/SECRET/ACCOUNT_ID`), never chat/code/commits.
