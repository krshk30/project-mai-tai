# Backtest broker model — DESIGN ONLY, awaiting operator review

**Operator spec, 2026-08-17 (R1–R10), with a code audit folded in.**

> ## ⛔ SCOPE — EXECUTION ONLY
> Entry/exit RULE design is **strategy** and stays parked. Everything here is about making the
> **simulated broker behave like the observed one** — what happens to an order after we send it.
> **If any item starts to change what we would ENTER rather than what happens to an order after we
> send it, STOP and say so.**

## The framing
**The current engine's implicit broker model is "orders fill."** 08-17's forensics measured the real
one, and it **refuses constantly**. That gap is why the engine has never found a defect the tape
found — and why its exit-geometry output was withdrawn twice in one evening.

---

## ⭐ CODE AUDIT — check which parts already work (do NOT rebuild these)

| item | state | evidence |
|---|---|---|
| **R2** Schwab bars | ✅ **ALREADY SATISFIED for v2** | `backtest/replay.py:523` — `bars = source.schwab_bars(symbol, start, end)`. The Polygon-feed defect is ORB-side or stale for v2. **Verify, do not rebuild.** |
| **R4** refusal model | ⛔ **FULLY ABSENT** | no reject/refusal logic in `replay.py`. "Orders fill" is literally the model. |
| **R5** rest-and-replace | ◐ **PARTIAL** | EH resting IS modelled (`_eh_resting_cross_check`, `_eh_entry_reprice`, band-cap, ABANDON on gap-through). The **RTH** rest→cancel→replace cadence is not. |
| **R6** exit ownership | ◐ **PARTIAL** | trades already carry `geometry` = `rth_static_oco` or `eh_floor_ride`. Missing: the **owner** framing (broker-side vs software ladder) and the reported split. |
| **R3** universe | ◐ **PARTIAL** | `v2_qualified_symbols` derives from `scanner_confirmed_events`; the window rule is not stated in the output header. |

---

## R1 — GOLDEN-DAY REPLAY IS THE ACCEPTANCE GATE, NOT AGGREGATE OUTPUT
08-17 is now a hand-validated session: **20 trades**, broker timestamps and prices, **+$2.26 gross**,
per-symbol **IPST +$2.52 · IVF −$0.26 · WFF +$0.10 · SLE −$0.10**.

The engine must replay that session and **reproduce the trade list** — names, entry times, exit
times, prices within a stated tolerance — before any aggregate it produces is quotable.

⛔ **Report per-trade divergence, never an error metric.** An engine that lands the totals by
cancelling one loss and inventing one win is wrong, and the totals hide it. *(This exact shape
occurred on 08-17: a withdrawn walker showed median +1.47% vs actual +1.52% while individual trades
were off by more than 50pp.)*

Extends the existing 14-test golden gate from cases to a whole session. **R1 costs almost nothing
now that a validated day exists.**

## R2 — FEED SCHWAB BARS, NOT POLYGON
✅ Audit says v2 already does. **Action: add a test that PINS it**, so it cannot regress silently.
Any v2 run on Polygon data is void regardless of what it shows (recursive ATR compounds divergence).

## R3 — THE UNIVERSE IS THE LIVE SCANNER CONFIRM/DROP WINDOW
Prior studies scored flips and name-days outside the window the live scanner acts on.
**Every run states the universe rule it used, in the output header.**

## R4 — MODEL REFUSAL, WITH THE OBSERVED CLASSES ⭐ the biggest lever
From the reject taxonomy — Schwab's own book, six days:

| class | n | modelled as |
|---|---|---|
| Security not electronically tradeable (21 symbols) | ~41 | **order never exists — exclude the name, do not fill it** |
| Buy-stop trigger not above ask on arrival | ~35 | reject at submit |
| Insufficient buying power | 1 | reject |
| Our own transient aborts (timeouts, DNS) | 7 | order never reaches the book |

⛔ **The first row changes results most:** if the engine trades names Schwab will not accept, its P&L
includes trades that could never have happened. **Build the refused-symbol list from the book and
apply it.**

## R5 — MODEL THE REST-AND-REPLACE CADENCE
Median time-at-rest **61–62s, every day**, and the dominant terminal state is **CANCELED-by-us**:
78/113 · 33/49 · 287/336 · 139/191 · 43/56 · 118/139.
An engine that fills instantly at the trigger is simulating a **different order type**.
Model: place → rest ~61s → cancel → replace, **fill only if the tape actually traded through in that
window**. ◐ EH already has an analogue; extend to RTH.

## R6 — MODEL EXIT OWNERSHIP
Exits are not one thing:
- **RTH Schwab fill** → native TRIGGER + childOrderStrategies bracket
- **RTH Webull fill** → native pair
- **Pre-market, either venue** → no broker-side protection; the **software ladder** owns it, **and the
  ladder dies with the process**

Attribute each exit to an owner and **report the split** — a stop that exists only in our process has
different reliability from one resting at the broker.
⭐ **Revisit after the ALL_DAY acceptance read** — if it passes, pre-market Webull moves to
broker-side and this table changes.

## R7 — EVERY OUTPUT CARRIES A DENOMINATOR
No count without one. "3 stop-outs" is unreadable without "of how many positions."

## R8 — GROSS AND NET, ALWAYS, SIDE BY SIDE
At qty 1–2 fees are decisive. The board already has a strategy that read gross-positive and
**net −2.10%/trade**. 08-17's +$2.26 is **gross** and is not yet a result.
⭐ Measured 08-17: our Schwab commissions and regulatory fees were **$0.00** (operator confirms TOS
and Webull charge no commission), so gross == net **at our size** — but that is a measurement, not a
licence to report gross alone.

## R9 — NAME THE POPULATION BEFORE MEASURING
Three denominators were scoped wrong in one day: 14 calendar days vs 10 sessions · fills-per-
placement vs fills-per-arm · all-positions vs the population the change reached.
**Each run's header states the POPULATION, the WINDOW and the ACCOUNT before any number.**

## R10 — WHEN THE ENGINE AND THE BROKER BOOK DISAGREE, THE ENGINE IS THE SUSPECT
The same rule has held four times against live queries. Every divergence in an R1 replay is
investigated as an **engine defect first**.

---

## Sequencing
Behind the ALL_DAY acceptance read and the ledger-erasure item. **Nothing here protects a position.**

**R1, R2, R4 are the three that would change what the engine says.** R1 is nearly free now that a
validated day exists; R2 is a pinning test; R4 is the real build.

⛔ **Interaction with the lot-attribution design.** The R1 golden-day gate needs a per-lot ground
truth for 08-17. The backfill measured **206/232 unambiguous (88.8%)** with **zero genuine
ambiguity** (PARTIAL = 0) — the shortfall is **18 real exits never written to `fills`** (the
native-OCO capture gap) plus 8 shared-book contaminations from a dropped our-orders join.
**Route: source the missing exits from the broker's transactions**, which matched our fills **28/28**
on 08-17.
