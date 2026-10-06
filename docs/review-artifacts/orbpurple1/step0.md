# ORBPURPLE1 lane B: independent Step 0

Assessment at 2026-10-06 11:10 ET. Base: `3ebde364d4634fdad45992e2ab1cbdf43ffeb221`.
Branch: `codex/orbpurple1-prev-bar-atr-entry-gate`. ETA: 2026-10-06 13:00 ET.
Blockers: 0. Only lane B; no shared C-row or handoff changes.

## Verdict

AGREE: the live ORB producer currently calls only the completed-Schwab MACD
gate before its 09:28 resting buy. It has no ATR entry check. Add a separate
`orb_schwab_atr_entry_gate_enabled` default-ON rollback switch using
`schwab_completed_atr_bars` and `compute_paper_atr_trail`, exactly the reader,
07:00 daily history boundary, modified-TR Wilder 5/3.5 calculation, provenance,
and gap treatment used by ORB-Schwab exits. Missing evidence must withhold.

The placement decision is at 09:28, after the 09:27 bar completes. A 09:29
bar is not yet available at placement. Both are shown below to distinguish
placement evidence from the bar immediately before the 09:30 broker fill.
This is an entry-placement check; existing later reprices, cancellations and
exits retain their semantics. A withheld initial order cannot be placed later
because the current opening-order state machine permits placement only on its
third range bar. RTH broker orders only by design, despite pre-open submission.

AGREE with the requested rule; improvement to realized results is UNMEASURED.
Neither recorded filled entry would have been skipped. In particular, JAGX
is a measured above-line case, not evidence that this rule prevents its loss.

## Every filled ORB-Schwab entry since September 30

| Date ET | Symbol / entry ET | 09:27 close / ATR line | 09:29 close / ATR line | Would skip at placement | Actual realized outcome, 2 shares |
| --- | --- | --- | --- | --- | --- |
| 2026-10-05 | MI 09:30:22 | 2.7700 / 2.4676440006 LONG | 2.9800 / 2.4676440006 LONG | No | 3.0498 -> 2.8000, -$0.4996 (-8.1907%), native OCO stop at 09:30:29 |
| 2026-10-06 | JAGX 09:30:14 | 6.5500 / 6.3625379808 LONG | 6.5500 / 6.3625379808 LONG | No | 6.6700 -> 6.6001, -$0.1398 (-1.0480%), break-bar body exit at 09:31:07 |

Coverage: 2/2 filled buy entries, 2/2 matching sells and quantities, 2/2
placement minutes, 2/2 pre-fill minutes, 2/2 computed ATR states. No filled
entry is missing ATR or realized outcome. P&L is execution price times quantity,
before fees. Aggregate actual loss -$0.6394; this placement rule removes $0.

## Full open-order / proposed-order enumeration

| Date ET | Symbol | 09:27 close / ATR line | Would withhold | Recorded outcome |
| --- | --- | --- | --- | --- |
| 2026-10-01 | NXL | 8.8000 / 7.2152594908 LONG | No | Open intent rejected; no broker order/fill; realized outcome UNMEASURED |
| 2026-10-02 | AMOD | 3.4700 / 3.1206823753 LONG | No | Broker buy cancelled on 09:28 completed negative MACD; no fill; realized outcome UNMEASURED |
| 2026-10-05 | APUS | 5.0700 / 5.4219865585 SHORT | Yes | Producer log proposes place; no stored open intent or broker buy; later cancel intent rejected; realized outcome UNMEASURED |
| 2026-10-05 | MI | 2.7700 / 2.4676440006 LONG | No | Filled, table above |
| 2026-10-06 | XHG | 3.1700 / 2.7464345871 LONG | No | Open intent rejected; no broker order/fill; realized outcome UNMEASURED |
| 2026-10-06 | JAGX | 6.5500 / 6.3625379808 LONG | No | Filled, table above |

5 stored buy-open intents, 3 broker buy rows (one cancelled, two filled),
6 log/intent placement candidates including APUS. No entries on 09-30;
09-30 observation mode logged no broker sends. 10-01 live mode; NXL refused,
VEEA late MACD cutoff. 10-03/04 weekend, no entries. The stored-bar fixture has
559 bars: AMOD 150, APUS 85, JAGX 149, MI 75, NXL 55, XHG 45. Sparse earlier
minutes use the exit calculation's existing gap rule; not synthetic bars.
All six placement minutes exist; no modeled profit is assigned to unfilled
or refused candidates. Historical as-of persistence/revision timing is
UNMEASURED: retained rows establish final completed-bar values, not the exact
original process read at 09:28. Prospective logs will expose the gate evidence.

## Evidence and scope

Own read-only SSH to `mai-tai-vps`, PostgreSQL `BEGIN READ ONLY`, per-query
15/20-second statement timeouts. Orders/fills/intents bounded to
2026-09-30 00:00 ET through 2026-10-07 00:00 ET, with capture before today's
session completes; candidate bars bounded to their 07:00-09:29 sessions.
Fixture: `tests/fixtures/orbpurple1_production_20261006.json`.
Logs: named ORB-Schwab rotations 20261002/03/06 and current file. The journal
window returned no entries; file logs supplied the decisions. No Redis reads.
Two exploratory SQL errors were corrected and the final fixture query succeeded;
failed queries are not treated as zero populations.

Read PR #940: paper ATR is a dark live arm/pull gate and has separate paper
state/re-arm semantics. Its `orb_paper_atr_entry_gate_enabled` is not reused.
The new switch only gates this live producer's initial opening order. Paper
ORB code, paper defaults and paper entry behavior remain unchanged.
Preserve bracket prices/body exit/ATR flip/15:55 close, nonnegative MACD,
10:00 cancellation and OWNMIX1 ownership bindings. No production write,
restart, environment edit, ledger change, deployment or merge is authorized.
