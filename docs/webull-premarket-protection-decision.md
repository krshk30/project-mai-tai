# Webull pre-market protection — the decision, for the operator

**Status: investigation CLOSED (operator, 2026-08-18). Parts 1/2/4 APPROVED to build. Part 3 closed,
opportunistic reopen only.** Written 2026-08-18 after the #710 acceptance read.

## ⛔⭐⭐ THE PRECISE CLAIM — "accepted" must never harden into "impossible"
Two different statuses, and they must not be collapsed:

| | status | basis |
|---|---|---|
| **combo / paired attach, pre-market** | **BROKER-PROVEN IMPOSSIBLE** | real PLACE, real position, 5x 417 with `ALL_DAY` set |
| **single-leg stop, pre-market** | **UNTESTED — closed BY DECISION, not disproven** | never attempted; `ALL` is valid single-leg and refused on the combo, so the endpoints demonstrably differ |

⛔ We are ACCEPTING this hole, not concluding that no protection can exist. A future reader must be
able to tell that the single-leg path was never tried — otherwise "we accepted it" decays into
"it was impossible", and the one untested dimension is silently written off.
[[feedback_unexercised_is_not_a_result]] · [[feedback_a_wrong_reason_is_worse_than_a_missing_one]]

---

## What is now settled, and how

**Webull validates a pre-market protective pair against the PRIOR CLOSE, not the live tape.**
This was hypothesis until this morning. It is now broker-proven on a real PLACE with a real position
behind it — not a preview.

```
XOS  live:orb  2026-08-18 08:41:59 ET  fan-out fill, qty 1 @ 4.6700
  payload SENT: "support_trading_session": "ALL_DAY", combo_type STOP_LOSS, stop_price 4.44
  x5      417   OAUTH_OPENAPI_TRADE_STOP_LOSS_PRICE_LT_MARKETPRICE
                "The stop price of the stop-loss order should be lower than the current market price."
  then    [WEBULL-PROTECT-FAILED] ... THE POSITION IS HELD WITH NO BROKER-SIDE STOP
```

| XOS at that moment | value | vs our stop 4.44 |
|---|---|---|
| **previous_close** | **2.09** | stop is ABOVE → refuse |
| last trade | 4.64 | stop is below → accept |
| minute close | 4.72 | stop is below → accept |

⛔ **#710 is CORRECTLY IMPLEMENTED AND INEFFECTIVE.** `ALL_DAY` was sent, exactly as designed, and
the refusal was byte-identical to the `CORE` era. **`support_trading_session` is not the lever that
selects the reference price.** That dimension is closed — do not spend another session on enum values.

⛔ Schwab cannot cover the gap either: `[V2-OCO-EMIT] XOS SKIPPED (outside regular hours)`. It refuses
stop legs in extended hours by design. **Pre-market is 0% bracketed on BOTH legs**, and the software
ladder is the only cover that exists.

---

## Proposal — four parts, and part 4 is not optional

### 1. Stop attempting the combo attach pre-market
Broker-proven impossible. Today it cost **5 refusals per fill** of pure noise, each one a full
payload + stack in `oms.log`. It protects nothing and it trains us to skim the very log lines that
carry real refusals. Gate the attach on RTH.

### 2. Replace it with ONE counted line per pre-market fill
⛔ **Not a silent gate.** Removing a failing attempt must not remove the evidence that we are
unprotected. One line per fill, plus a per-session running count:

```
[WEBULL-PREMARKET-UNPROTECTED] <sym> <acct> qty=<n> entry=<px> — no broker-side protection is
  available pre-market (Webull validates against the prior close; Schwab refuses EH stop legs).
  The software ladder owns this exit. unprotected_fills_this_session=<k>
```
The count is the thing to watch: if it ever reads 0 on a day with pre-market fills, the line broke.

### 3. The single-leg stop probe — CLOSED, OPPORTUNISTIC REOPEN ONLY
The only untested dimension left. `ALL` is valid single-leg and refused on the combo, so **the two
endpoints demonstrably differ in at least one dimension** — the prior-close reference may be a
property of the combo endpoint rather than of Webull's stop validation generally.

A single-leg `STOP_LOSS` on a held pre-market share answers it with a real place and a real position,
the same standard as this morning's result. **And if it places, the position is protected.**

⚠️ **Risk, stated plainly:** no OCO linkage. If the software ladder exits first, the stop is orphaned
and can oversell — the E5/NXTC shape. Qty 1, and a human watching.
⛔ **CLOSED as a planned action (operator, 2026-08-18). Never placed on agent initiative.**
It reopens only opportunistically: **if the operator is at the screen during a pre-market fill and
says go, it runs then.** Not scheduled, not queued, not "next chance" — it needs a human already
watching, because the failure mode is an orphaned stop that oversells.

### 4. ⛔⭐⭐ THE FENCE — THE MOST IMPORTANT ITEM IN THE PR, NOT THE LEAST
If we formally accept that pre-market positions have no broker-side protection, then **the software
ladder is the only cover — and the ladder is IN-PROCESS.** An OMS restart while holding a pre-market
position leaves that position genuinely naked, with nothing at either broker.

**Pre-flight must refuse the restart, or at minimum warn loudly and demand an explicit override.**
This ships in the same PR as parts 1–2. Today that fence held by attention alone: we held
`live:orb XOS = 1` unprotected from 08:41 to 09:07, and the standing "no restart while open" rule was
enforced by a human reading a log line.

---

## What NOT to do
- ⛔ Do not try another `support_trading_session` value. Broker-proven closed.
- ⛔ Do not widen or move the stop level to make the refusal stop. That changes what we risk, and
  it is strategy, not execution.
- ⛔ Do not read "the refusals stopped" as success after part 1 lands — part 1 *removes the
  attempts*. Part 2's count is the only honest signal.
