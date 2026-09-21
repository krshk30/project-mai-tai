# Webull API pressure — where the 429s land

**Author `claude-1`, 2026-09-21. Reviewer `codex-2`. Read-only measurement — NO code, and NO fix proposed before the table.**
Board row it answers: *"Webull API pressure (~1,261 TOO_MANY_REQUESTS, 416 status-fetch failures in 10 sessions) — calls/min by
endpoint vs where the 429s land; no fix before the table."*

## Verdict

1. **Every 429 is the order-STATUS read. 1,473 of 1,473** `TOO_MANY_REQUESTS` answers in 10 sessions came from
   `GET /trade/order/detail`. **0** came from `/trade/order/place`, **0** from `/trade/order/cancel`.
2. **So the rate limit never refused an exit or a cancel directly.** Its harm is second-hand: a status read that fails leaves
   the OMS acting on a stale view of an order — the "stale HELD read" in the 09-18 late-close spec. The "amplifier" reading in
   the 09-18 sweep stands, but only through that path.
3. **It is not a burst problem.** 1,033 of the 1,242 affected seconds hold exactly ONE 429; the worst second holds 5; the worst
   minute 16 (09-16 15:34 ET). A single lone status read gets refused — which says the budget is already spent by reads that
   SUCCEEDED in the same window, and those are not logged.
4. **What is being polled when it happens:** resting ENTRY orders 639 (43%), the protect pair's STOP leg 455 (31%) and TARGET
   leg 375 (25%), software closes 4 (0.3%). Polling the pair costs two reads per position per cycle (`…S` and `…T`).
5. **The denominator the row asked for — calls per minute by endpoint — CANNOT be produced from the logs.** Only failures are
   logged. One counter per endpoint (success + failure, per minute) is the measurement to add before any fix is chosen.

## Population and method

`oms.log` rotated files for the 10 sessions 09-04 → 09-18 (`oms.log-20260905 … 20260919.gz`). Unit = one
`[webull.core.client] ServerException occurred` block; the endpoint is the block's `_action_name`, the answer its
`HTTP Status / Code`. Every block was classified (the two tallies below both sum to 1,473). ⚠ The handoff's ~1,261 and 416 do
not reproduce: by this unit I count **1,473** 429 blocks and **500** `Webull order-status fetch failed` lines. The earlier unit
is not recorded, so I cannot say which is right — only that this one is stated.

| session | 429 on `/trade/order/detail` | minutes with ≥1 | peak minute (ET) | `order-status fetch failed` lines |
|---|---|---|---|---|
| 09-04 | 85 | 52 | 13:33 (2) | 15 |
| 09-08 | 237 | 109 | 12:54 (6) | 103 |
| 09-09 | 444 | 206 | 14:37 (5) | 38 |
| 09-10 | 0 | 0 | — | 0 |
| 09-11 | 12 | 10 | 15:31 (3) | 10 |
| 09-14 | 4 | 3 | 15:45 (2) | 1 |
| 09-15 | 93 | 60 | 11:21 (4) | 11 |
| 09-16 | 295 | 83 | **15:34 (16)** | **232** |
| 09-17 | 212 | 110 | 10:33 (5) | 84 |
| 09-18 | 91 | 48 | 14:05 (4) | 6 |
| **total** | **1,473** | 681 | | **500** |

Two things in that table worth a second look, not explained here: 09-10 has **zero** 429s on a day with 54 place/cancel
refusals (so trading happened); and 09-16 carries both the worst minute (15:34 ET, 16) and 232 of the 500 status-fetch
failures — 15:34 is BEFORE DLXY's halt (15:43–15:59 ET, see `PROTECT_FAILED_FORENSIC.md`), so the halt does not explain it.

**429s per second** (1,242 distinct seconds; 1,033 + 2×194 + 3×10 + 4×3 + 5×2 = 1,473): one → 1,033 · two → 194 · three → 10 · four → 3 · five → 2.

**What was being read** (by `client_order_id` shape): `-open-` 639 · `-protect-…S` 455 · `-protect-…T` 375 · `-close-` 4.

## Every other Webull refusal in the same blocks (for the catalogue — none is a rate limit)

| endpoint | answer | count, 10 sessions |
|---|---|---|
| `/trade/order/place` | `NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K` | 76 |
| `/trade/order/place` | `ORDER_RISK_RULE_PRICE_AGGRESSIVE` | 52 |
| `/trade/order/place` | `CANT_TRADE_FOR_PRICE_BETWEEN_0099_AND_0999` | 25 |
| `/trade/order/place` | `CAN_NOT_CREATE_A_OPEN_ORDER` | 20 |
| `/trade/order/place` | `STOP_PRICE_MUST_BE_GREAT_THAN_MARKET_PRICE` | 3 |
| `/trade/order/place` | `NO_SUCH_TICKER` | 1 |
| `/openapi/trade/order/place` | `OPENAPI_TICKER_STATUS_NOT_ALLOW_TREADE_CS` (halt) | 5 |
| `/openapi/trade/order/place` | `OPENAPI_NEW_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K` / `…REVERSE_OPTION` | 4 / 1 |
| `/trade/order/cancel` | `cancel` (code text as logged) | 101 |
| `/trade/order/cancel` | `ORDER_CAN_NOT_BE_CANCEL` | 77 |
| `/trade/order/cancel` | `ORDER_CAN_NOT_BE_CANCEL_FOR_PENDING_CANCEL` / `ORDER_NOT_FOUND` | 1 / 1 |

⚠ `ORDER_RISK_RULE_PRICE_AGGRESSIVE` = 52 here against the handoff's "41 in 4 sessions": this is 10 sessions and counts SDK
blocks, not orders. Not reconciled; do not mix the two.

## What this means for sweep item 1 (the repaired take-back-and-sell routine)

- Its retries are `cancel` and `place` calls — the two endpoints with **no** rate-limit refusals in 1,473. Retries do not
  compete with the 429 budget as far as this tape shows.
- Its cancel-AND-READ step IS a `/trade/order/detail` read, and that is the endpoint that gets refused. **A refused read must
  not be scored as "unconfirmed release"** — it is "could not tell", and the routine needs a named branch for it (retry the
  read; never sell and never stand down on a 429).
- Do not add polling to "be sure". The pair already costs two reads per position per cycle.

## Not proposed here (the row says no fix before the table — this IS the table)

Candidates the table points at, for the reviewer to argue with, none built: one combined read per pair instead of `S` + `T`;
slower polling of a resting ENTRY order that the broker will report on fill anyway; back-off after a 429 instead of the next
cycle's identical read. Each needs the per-endpoint call counter first, or its effect cannot be measured.

## What this cannot see

- Successful calls — so no calls/min, no ratio, no proof of where the budget goes. This is the gap, not a detail.
- Webull's actual limit for `/trade/order/detail` (per second? per minute? per app key or per account?). Not in these logs.
- Which of the adapter's three `OrderDetailRequest` call sites (`broker_adapters/webull.py:394`, `:723`, `:958`) issued each
  read. The blocks carry the order id, not the caller.

## Separate observation — the SDK writes the request headers into `oms.log`

Every one of these `ServerException` blocks dumps the full SDK request, including the `x-app-key` header and the per-request
`x-signature`. The files are `root:root 0640`, rotated and gzipped with the same mode. Not the app SECRET, and not
world-readable — but an API key at rest in ~1,840 log blocks (1,473 + 367 other refusals) is worth one line of logger configuration. Not boarded by me:
it needs an owner and a decision (`codex-2` holds the adapter). Values were redacted in everything I printed.
