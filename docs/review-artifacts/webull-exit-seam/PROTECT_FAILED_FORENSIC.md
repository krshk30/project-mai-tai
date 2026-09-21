# `[WEBULL-PROTECT-FAILED]` — the three positions "held with no broker stop"

**Author `claude-1`, 2026-09-21. Reviewer `codex-2`. Read-only forensic — NO code in this PR.**
Board row it answers: *"WEBULL-PROTECT-FAILED 3 of 75 (IMRN 09-04, QCLS 09-16, DLXY 09-16 — held with no broker stop)"*.

## Verdict

1. **None of the three is "Webull refused a stop we could have placed".** One was a **false alarm** — the position was being sold
   as the attach loop started (sell working at attempt 1, account flat 63 ms later; attempts 2–5 ran against nothing). Two were **trading halts** — nothing is placeable in a halt, at any broker.
2. **The alarm text is wrong in 1 of 3.** `THE POSITION IS HELD WITH NO BROKER-SIDE STOP … Place one by hand` fired for IMRN
   while the account was flat. An operator following it would have placed a sell against nothing.
3. **The retry budget does not match the cause in 2 of 3.** Five attempts in ~31 s against a halt that lasts minutes: QCLS
   halted ~12:15–12:19 ET, DLXY ~15:42–16:00 ET. The loop gave up while the halt was still on and never came back.
4. **DLXY exposed a second, larger defect, outside this row:** 13 `CW_FLOOR` market sells were accepted by Webull and then
   **cancelled by the broker 19.6–33.9 s later**, one after another for 14 minutes (15:43:35 → 15:57:46 ET), all inside the halt.
   That is **13 of the 17** cancelled Webull `CW_FLOOR` closes 09-04→09-18 (the other 4: QCLS ×2, ZTG, DAIC) — one symbol, one halt.
5. **Terminal rule proposed** (for sweep item 1, not built here): an attach that cannot succeed ends in one of three named
   states — `FLAT` (row closed: stop, say nothing), `HALTED` (park, re-attach on the first print after the halt, page once),
   or `REFUSED` (flatten + page). Never "gave up, place one by hand".

## Population

`oms.log` markers, 10 sessions 2026-09-04 → 2026-09-18 (rotated files `oms.log-20260905…20260919.gz`; 0906/07/08/13/14 hold
no Webull fills — weekend, Labor Day, rotation of non-trading days): **77** `[WEBULL-BARE-FILL]` · **74**
`[WEBULL-PROTECT-ATTACHED]` · **3** `[WEBULL-PROTECT-FAILED]`. 74 + 3 = 77. ⚠ The handoff says "3 of 75"; I count 77 bare
fills by marker and cannot reproduce 75 — the unit behind 75 is not recorded. The 3 are the same 3.

| session | bare fills | attached | failed |
|---|---|---|---|
| 09-04 | 7 | 6 | 1 (IMRN) |
| 09-08 | 7 | 7 | 0 |
| 09-09 | 9 | 9 | 0 |
| 09-10 | 2 | 2 | 0 |
| 09-11 | 2 | 2 | 0 |
| 09-14 | 6 | 6 | 0 |
| 09-15 | 13 | 13 | 0 |
| 09-16 | 14 | 12 | 2 (QCLS, DLXY) |
| 09-17 | 10 | 10 | 0 |
| 09-18 | 7 | 7 | 0 |

## Case 1 — IMRN 09-04: FALSE ALARM. The position was sold 63 ms after the first attach attempt (times ET)

| time | event | source |
|---|---|---|
| 13:33:42.912 | Webull buy 1 @ 1.64 filled at the broker | `broker_order_events` |
| 13:33:48.347 | OMS processes the fill — **5.4 s later** — `[WEBULL-BARE-FILL]` | `oms.log` |
| 13:33:48.667 | `CW_FLIP` decides to close (ref 1.63); market sell accepted 13:33:48.801 | `[OMS-V2-MANAGED-EXIT]`, events |
| 13:33:48.836 | attach attempt **1/5** refused: `OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION` — the market sell was working | `[WEBULL-PROTECT-RETRY]` |
| 13:33:48.899 | **sell filled @ 1.64 — account FLAT** (0.0% on the leg) | events, `fills` |
| 13:33:51 → 13:34:19 | attempts **2–5** refused: `OPENAPI_NEW_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K` — there was nothing left to protect | `[WEBULL-PROTECT-RETRY]` ×4 |
| 13:34:19.393 | `[WEBULL-PROTECT-FAILED] … THE POSITION IS HELD WITH NO BROKER-SIDE STOP` — **false** | `oms.log` |

Both refusals are the "shares committed / gone" family of the 09-18 broker-answer catalogue. **Class: the attach loop does not
re-read the managed row between attempts**, so it cannot tell "refused because we are already out" from "refused".
Bare-at-broker time: **6.0 s** (broker fill → broker sell fill). Harm: none measured; the defect is the false page.

## Case 2 — QCLS 09-16: HALT

| time | event | source |
|---|---|---|
| 12:14:36 | Webull buy 1 @ 1.51 | `fills` |
| 12:15:31 → 12:16:02 | attempts 1–5, **all five** `OPENAPI_TICKER_STATUS_NOT_ALLOW_TREADE_CS — This stock is currently halted by exchanges` | `[WEBULL-PROTECT-RETRY]` ×5 |
| 12:15 → 12:18 | **0 captured trades** (12:14: 4,390 trades; 12:19: 2,254) — the halt, seen on the tape | `market_capture_trades` |
| 12:16:02 | `[WEBULL-PROTECT-FAILED]`; no further attach attempt after the halt lifted | `oms.log` |
| 12:20:03 | `CW_FLOOR` market sell filled @ 1.635 — **+8.3%** | `fills` |

Bare at the broker **327 s**, of which ~4 minutes were the halt (no stop could rest) and ~1 minute after the resume was
avoidable. The broker's answer names the cause outright; the loop treats it like any other refusal.

## Case 3 — DLXY 09-16: HALT, and we never sent a pair at all

| time | event | source |
|---|---|---|
| 15:41:48 | Webull buy 1 @ 1.90 filled (`fills.filled_at`); OMS processes it 15:43:03 — **75 s later** | `fills`, `oms.log` |
| 15:41 → 15:42 | tape runs 1.64 → 2.03 and pins at 2.03; **15:43 → 15:59: 0 captured trades**; 16:00: 9,938 trades, 1.90–2.76 | `market_capture_trades` |
| 15:43:03 → 15:43:34 | attempts 1–5: **our own pre-check** `[WEBULL-PROTECT-UNPLACEABLE]` — target 1.9950 is below the market (2.03). **Nothing was sent to Webull** — so the row's "5 attach answers" are 5 of OUR refusals, 0 of the broker's | `oms.log` |
| 15:43:34 | `[WEBULL-PROTECT-FAILED]` | `oms.log` |
| 15:43:35 → 15:57:13 | **13 `CW_FLOOR` market sells, each `accepted` then `cancelled` with `event_source=broker`** — accept→cancel 24.8, 33.9, 23.4, 29.1, 30.2, 23.1, 28.1, 33.7, 19.6, 31.3, 24.4, 22.3, 33.5 s. No reason is stored on the cancel event | `broker_orders`, `broker_order_events` |
| 16:00:05 | a `limit` sell (the after-16:00 order type) filled 16:00:06 @ 2.27 — **+19.5%** | `fills` |

Two separate things went wrong. (a) The pair was unplaceable only because its TAKE-PROFIT leg was already behind the market;
the STOP leg at 1.7480 was perfectly valid, and a position that is already +6.8% past its target arguably should be sold, not
bracketed. The pre-check has no branch for "target already passed". (b) The close loop re-sent a market sell 13 times (gaps 25 s to 292 s) into
a halt and was cancelled every time — it never learned it was halted. The +19.5% is luck: the stock reopened up.

⚠ **Cause of the cancels: NOT pinned.** The other 4 cancelled floor sells show the same shape — ZTG 09-16 10:58:18→10:58:45,
QCLS 12:17:25→12:17:45 (inside its halt) and 12:19:28→12:20:01 (the minute prints resumed), DAIC 09-17 08:58:35→08:58:59 — so
17 of 17 were cancelled by the broker 20–34 s after accept. That reads like a broker-side timeout on a market order that
cannot fill, which a halt guarantees; it is NOT proof that every one was a halt (ZTG and DAIC tapes not checked here).

## What this changes on the board

- The row *"3 of 75 held with no broker stop"* closes as: **1 false alarm, 2 halts; 0 cases of Webull refusing a placeable stop.**
- Three asks move INTO sweep item 1 (`claude-1`, claim on `oms/service.py`) — they are not new rows:
  1. re-read the managed row before every attach attempt and before the FAILED line; flat ⇒ stop silently;
  2. classify `TICKER_STATUS_NOT_ALLOW` (and a tape with no prints) as `HALTED`: stop spending attempts, re-attach on the first
     print after the halt, page once;
  3. "target already passed" ⇒ sell (or stop-only), never five identical pre-check refusals.
- **One new fact for the broker-answer catalogue:** Webull ACCEPTS a market sell it cannot fill and cancels it 20–34 s later with
  no reason (17 of 17 cancelled `CW_FLOOR` closes; 13 of them DLXY inside its halt). Any retry routine that reads `accepted` as progress will loop here. Fixture class for
  sweep item 1.

## What this cannot see

- Webull's reason for the 13 cancels — not stored (`broker_order_events.payload` has none). The halt reading is inferred.
- Whether a STOP-only order is accepted by Webull for a position like DLXY's. Never sent; needs the qty-1 harness, not a guess.
- Fill-processing lag (5.4 s IMRN, 75 s DLXY between broker fill and `[WEBULL-BARE-FILL]`) is noted, not explained here.
