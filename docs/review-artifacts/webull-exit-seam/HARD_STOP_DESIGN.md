# Hard stop on Webull — how −8% is held at the broker

**Author `claude-1`, 2026-09-21. Reviewer `codex-2`. Design note — NO code in this PR.**
Board row it answers: *"Hard stop is a market sell AFTER the level trades (GIPR −18.4%)"* (handoff 09-19, owner `claude-1`).

## Verdict (read this, skip the rest if short of time)

1. **The row's premise does not survive the sweep.** "Market sell after the level trades" is how all 7 filled `CW_HARD_STOP`
   closes worked, and 6 of 7 filled within −1.33%..+2.25% of the level (median **−0.35%**). GIPR 14:15 ET is **1 of 7**.
2. **GIPR −18.4% was a leg left NAKED, then a one-second collapse.** The Webull pair (native stop 1.0488) was cancelled by the
   confirmation exit at 14:05:10 ET, the close was refused, nothing was re-attached, and 10 minutes later 445,704 shares traded
   from 1.09 to 0.9735 inside one second. The defect is the release-without-re-protect — the class #1014 fixed for the
   confirmation exit and sweep item 1 extends to every other software exit. **No new hard-stop mechanism is proposed.**
3. **The rule this note adds:** on Webull the software hard stop is the SECOND line, never the first. A leg that holds shares
   and has no resting native stop is a defect state with a deadline, not a normal state.
4. **One thing to measure before anything is built:** the software stop decided 2.66 s (GIPR) and 0.65 s (IMCC) after the first
   captured TRADE through the level. n=2. A log field, not a rule change.

## Population — every FILLED `CW_HARD_STOP` close, both live accounts, 2026-09-04 → 2026-09-18

Source: `broker_orders` ⋈ `trade_intents.reason='oms_v2_managed_exit:CW_HARD_STOP'`, `status='filled'`; price = `fills.price`;
level = `metadata.reference_price`; tape = `market_capture_trades` / `market_capture_quotes`. **7 rows — 1 Schwab, 6 Webull**
(the 6 matches the handoff's "58 rejected / 6 filled"). Rejected closes are NOT in this table; they are sweep item 1.

| decided (ET) | symbol | account | level | fill | fill vs level | decision − first trade ≤ level |
|---|---|---|---|---|---|---|
| 09-04 09:31:25 | IMRN | live:schwab_1m_v2 | 1.7751 | 1.8150 | **+2.25%** | UNMEASURED (0 captured trades in the 120 s before) |
| 09-04 15:22:14 | CDTG | live:orb | 1.2920 | 1.3050 | **+1.01%** | UNMEASURED (0 captured trades) |
| 09-04 15:31:30 | IMRN | live:orb | 1.6910 | 1.6901 | **−0.05%** | UNMEASURED (0 captured trades) |
| 09-15 15:46:49 | MEDS | live:orb | 1.7480 | 1.7401 | **−0.45%** | none — no trade at/below the level before the decision (861 trades captured) |
| 09-18 13:17:52 | GIPR | live:orb | 1.1040 | 1.1001 | **−0.35%** | none (1,298 trades captured) |
| 09-18 14:15:05 | GIPR | live:orb | 1.0488 | 0.9300 | **−11.33%** | **2.66 s** (1,814 trades captured) |
| 09-18 14:21:35 | IMCC | live:orb | 5.0876 | 5.0201 | **−1.33%** | **0.65 s** (878 trades captured) |

Median −0.35%. Drop GIPR 14:15 by name: range −1.33%..+2.25%. ⛔ Seven fills is a small population; it supports "the
mechanism is not the class defect", not "the mechanism is proven".

## GIPR 09-18, second trade — the tape (all times ET)

| time | event | source |
|---|---|---|
| 14:03:16 | Webull buy 1 @ 1.14; pair attached 14:03:21 — target 1.1970, **native stop 1.0488** | `fills`, `[WEBULL-PROTECT-ATTACHED]` |
| 14:05:07 | confirmation exit: Schwab sold 2 @ 1.115 (−2.2%) | `fills` |
| 14:05:10.580 | Webull pair released `requested=2 confirmed=2`; 14:05:11.110 close **REFUSED** `reason=pair_cancel_unconfirmed reports=2 confirmed=0`; **nothing re-attached** | `[OMS-V2-CONFIRMATION-EXIT-WEBULL-RELEASED]` / `-REFUSED]` |
| 14:05:10.6 → 14:15:04.7 | **Webull leg holds 1 share with NO order at the broker — 594 s** | `[OMS-OCO-EXIT-MISS]` ×3, no attach line |
| 14:15:00 | 15 trades, 1.08–1.0899 | `market_capture_trades` |
| **14:15:01** | **985 trades, 445,704 shares, 1.09 → 0.9735.** First trade ≤ 1.0488 at **14:15:01.588** | same |
| 14:15:04.055 | first trade ≤ 0.95; from here every print is **0.93** (pinned — reads like a price band; NOT verified) | same |
| 14:15:04.495 | first captured QUOTE with bid ≤ 1.0488 (bid 1.04) | `market_capture_quotes` |
| 14:15:04.660 | software decides `CW_HARD_STOP` — **165 ms after that quote** | `[OMS-V2-MANAGED-EXIT] decided_at` |
| 14:15:05.047 | market sell accepted | `broker_order_events` |
| 14:15:09.465 | filled **0.93** — 4.4 s after accept, into the pinned market | `fills` |

**What a resting native stop would have done [inferred, not pinned]:** triggered by the 14:15:01.588 print and filled somewhere
inside that second's range 1.04 → 0.9735, i.e. **−8.8% to −14.6%** on the 1.14 entry, against the actual **−18.4%**. So the naked
window cost **3.8 to 9.6 points** here — and even a perfect broker stop does NOT hold −8% through a one-second collapse. −8% is a
trigger level, never a guaranteed exit price; nothing in this note changes that.

⚠ **Unresolved:** the captured quote stream shows bid 1.07 at 14:15:02.090 while captured trades print 0.9735 at 14:15:01. The
two streams disagree by ~3 s. Capture `received_at − event_ts` medians in that window: quotes 950 ms, trades 7,745 ms (a burst
backlog in the capture consumer — this is the CAPTURE's lag, not a measurement of what the OMS saw). Which clock is right is not
pinned; the OMS does not log the quote age it acted on.

## The design — four states, each ends SOLD or PROTECTED-AGAIN + PAGED

| state of a Webull leg that holds shares | first line | second line | what is missing today |
|---|---|---|---|
| **A. PROTECTED** — native pair resting | the broker's stop | **Schwab:** the software ladder STANDS DOWN on fresh broker confirmation (`_native_oco_stand_down_active`, `MAI_TAI_OMS_NATIVE_OCO_STAND_DOWN_ENABLED=true` read from the OMS process 09-21). **Webull: the stand-down FAILS OPEN** — the docstrings at `oms/service.py:3088-3093` and `:7868` say Webull exposes no confirmation capability, so the ladder runs ALONGSIDE the resting pair and must take the pair back before it can sell (the shares are reserved) | **on Webull the software hard stop and the native stop sit at the SAME level (both −8%) and race.** Requirement for sweep item 1: at the hard-stop level the routine must treat a filled or working native stop leg as the exit (`resolved_by_fill`) and must never end with the pair cancelled and no sell. [read from docstrings, NOT traced on a tape — reviewer please check] |
| **B. RELEASED for a software close** (confirmation, hard stop, floor, flip, overnight flatten) | the software close, retried by broker-answer class | **re-attach the pair, then page** — the #1014 terminal rule | #1014 covers the confirmation exit only. **Sweep item 1** moves the other four onto the same routine. A release with no close and no re-attach inside the deadline is the GIPR state and must be impossible |
| **C. NEVER PROTECTED** — all attach attempts failed (3 of 75 positions: IMRN 09-04, QCLS 09-16, DLXY 09-16) | none today — software ladder only | — | `PROTECT_FAILED_FORENSIC.md` reads the 5 attach answers each. Proposed terminal rule, to be confirmed by that forensic: attach failed ⇒ **flatten now + page**; never sit bare behind the software stop |
| **D. EXTENDED HOURS** — a native stop leg is refused or cannot trigger | software hard stop | — | out of scope here; the software stop IS the only line and the row stays as it is. Stated so nobody reads state A as covering 04:00–09:30 |

**Deadline for B and C.** GIPR sat bare for 644 s (13:07:07.7 → 13:17:52.0) and 594 s, measured here from the `WEBULL-RELEASED` line to the
hard-stop decision (the 09-18 sweep quotes 653 s / 611 s with a different end point); SUNE 09-09 for 1,985 s [sweep number, not re-derived]. Proposal: a leg that holds shares with no
resting native stop during RTH for more than **30 s** is paged as `UNCOVERED`, whatever routine put it there. The number is a
proposal; the reviewer should push on it. It is a detector, not a fix — the fix is B.

## What is explicitly NOT proposed

- **No stop-limit, no "sell at the level" limit order.** In the GIPR second a limit at 1.0488 would not have filled at all.
- **No tighter stop percentage.** 8.0 is the operator's number (`MAI_TAI_OMS_V2_CW_HARD_STOP_PCT=8.0`, read from the OMS
  process 09-21).
- **No trade-print trigger yet.** Two instances (2.66 s, 0.65 s) are a reason to MEASURE. Ask for `codex-2`/sweep item 1: log, on
  every hard-stop decision, the age of the quote acted on (`quote_age_ms`) and the bid. Five sessions of that field decide
  whether a last-trade trigger is worth building.

## What this note cannot see

- The 58 REJECTED Webull hard-stop closes — how long each position stayed open after its first refusal, and what it cost. That
  is the larger number and it belongs to sweep item 1's fixtures, not here.
- Capture had 0 trades for the three 09-04 rows, so their detection lag is UNMEASURED, not zero.
- Whether 0.93 was a LULD band. Read from the prints only.
