# B11 resting-buy provenance: first read

Read-only extraction cutoff: **2026-09-28 23:38:31 UTC (19:38:31 ET)**. The
population is every `schwab_1m_v2` managed row opened on `live:schwab_1m_v2`
or `live:orb` from 2026-09-01 04:00 ET through that cutoff. The protocol and
resting-buy amendment were committed before any return was read. This is an
observational trip study, not an ATR replay and not an entry-rule decision.

## Verdict

**INCONCLUSIVE for a rebuilt-entry rule.** Of 286 first-slot filled account
legs, 207 have positive LIVE provenance, 2 have positive REBUILT provenance,
and 77 are UNKNOWN. The two REBUILT legs are Webull same-session re-adds before
#993 was active (MYSZ 09-15 and FTFT 09-16); both had a fill-derived gain of
about +5%. There are **zero attributable REBUILT filled legs after #993** and
zero filled legs of any class after the #1049 restart in this extraction. This
does not prove that rebuilt entries are good, bad, or that #1049 has passed a
live first read. Do not change an entry rule from this table.

## Activation and stage denominators

The #993 split uses the observed v2 boot at **2026-09-16 23:43:44 UTC**,
corroborated by `[V2-BOOT-HOLD]` and `[V2-CW-SEED-CAP]` in the rotated v2 log,
not its merge time. All 09-16 trading fills predate that restart. The #1049
split is the observed v2 PID 1329729 start at **2026-09-28 22:42:25 UTC** in
`/home/trader/fleet_health/deployments-20260928.md`. Counts below are stage
counts, not a conversion funnel: one arm can precede several repriced rests,
and a first rest can precede the later BUY ARM.

| Era | Account leg | Arms (opportunities, shared) | Rest placements (shared) | First intents | First broker orders | First buy Fills |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Pre-#993 | Schwab | 607 | 807 | 673 | 442 | 54 |
| Pre-#993 | Webull | 607 | 807 | 682 | 698 | 85 |
| #993 to #1049 | Schwab | 694 | 680 | 587 | 598 | 65 |
| #993 to #1049 | Webull | 694 | 680 | 641 | 500 | 82 |
| Post-#1049 | either | 0 | 0 | 0 | 0 | 0 |

The shared arm/rest columns are displayed twice to align eras with account
legs; they **must not be summed across accounts**. Reclaim legs are separate:
pre-#993 Schwab 89 intents / 53 orders / 24 buy Fills, and Webull 36 / 42 /
17. No post-#993 reclaim fills occurred. Orders can exceed intents because a
single intent may produce more than one broker order attempt.

There are 327 closed managed rows and 327 buy Fills across both legs. Nine buys
have no complete sell-Fill pair. In addition, BNC on Schwab (09-08) and YMAT
on Webull (09-09) had a new buy while an earlier buy still had unmatched
quantity. All nine buys in those two account-symbol sessions are excluded from
sell attribution rather than guessed with FIFO. This leaves **309 safely paired
sell-Fill outcomes**. Exact account/symbol, price, and entry-time checks
identify 323 managed rows. The intersection is **305 scored rows**; four
unjoined managed rows have no attributed row-level outcome. No current-profit
field, bar high, or hypothetical exit was used.

## Fill-only results

The numerator is completed positive-return rows; the denominator is scored
rows with both buy and sell Fills. Median comes before the unweighted sum of
per-leg percentages. Fees are excluded. These are **not dollar P&L** and the
two account legs are not independent strategy opportunities.

| Era | Account | Provenance | Filled legs / scored | Winners / scored | Median % | Sum of % |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| Pre-#993 | Webull | LIVE | 42 / 37 | 18 / 37 | -0.17 | +2.34 |
| Pre-#993 | Webull | REBUILT | 2 / 2 | 2 / 2 | +5.02 | +10.04 |
| Pre-#993 | Webull | UNKNOWN | 41 / 40 | 23 / 40 | +1.86 | -15.04 |
| Pre-#993 | Schwab | LIVE | 24 / 24 | 10 / 24 | -1.26 | -8.26 |
| Pre-#993 | Schwab | REBUILT | 0 / 0 | 0 / 0 | N/A | N/A |
| Pre-#993 | Schwab | UNKNOWN | 30 / 25 | 15 / 25 | +1.59 | +0.24 |
| #993 to #1049 | Webull | LIVE | 78 / 75 | 31 / 75 | -1.49 | -61.35 |
| #993 to #1049 | Webull | REBUILT | 0 / 0 | 0 / 0 | N/A | N/A |
| #993 to #1049 | Webull | UNKNOWN | 4 / 4 | 1 / 4 | -1.87 | -3.26 |
| #993 to #1049 | Schwab | LIVE | 63 / 63 | 22 / 63 | -1.59 | -42.97 |
| #993 to #1049 | Schwab | REBUILT | 0 / 0 | 0 / 0 | N/A | N/A |
| #993 to #1049 | Schwab | UNKNOWN | 2 / 2 | 1 / 2 | -0.21 | -0.42 |

The 41 reclaim Fills are `NOT_FIRST` in the trip list, not silently mixed with
the first-slot provenance comparison. Their 33 scored rows have separate
descriptive results in `RESULTS.json`.

The 09-22 through 09-26 holdout contains 43 Webull LIVE first fills (42
scored, 13 winners, median -2.41%) and 35 Schwab LIVE first fills (35 scored,
13 winners, median -1.75%). It contains no proven REBUILT fills; two Webull
first fills remain UNKNOWN. Per-hour ET and subclass breakdowns are in
`RESULTS.json` and the individual trips in `TRIPS.csv`.

For the two proven REBUILT legs, removing MYSZ leaves FTFT +5.04%; removing
FTFT leaves MYSZ +5.00%. That is a two-observation sensitivity, not robust
evidence. Pre-#993 Webull LIVE's +2.34 sum changes sign after dropping QCLS
(-11.07), so its sum is fragile. Post-#993 LIVE medians remain negative after
dropping any one name (Webull -1.67 to -1.44; Schwab -1.73 to -1.51), but
the absence of a rebuilt comparison prevents a causal verdict. The full
drop-one-by-name median and sum ranges, with each omitted name, are in
`RESULTS.json`.

## Attribution and limits

The classifier joins a buy Fill through its intent's first-slot trigger,
fan-out segment and slot IDs when present, and the matching prior rest log.
It takes a unique near placement first; a delayed Webull mirror is accepted
only if the 30-minute trigger/identity window has a unique candidate. 281 of
286 first-slot fills have a unique logged rest; 5 have no unique rest. A
later `[V2-CW-ARM]` never causes an earlier fill.

LIVE requires the SELL-flip short bar, short ages 1 and 2, and the trail bar
behind the matched rest, all emitted at live cadence after scanner membership
began and after a symbol-specific REST/streamer warmup, plus a v2 watchlist
sample. REBUILT requires positive historical-bar or pre-current-watch evidence;
same-session re-add is assigned only when a preceding FADE and new CONFIRM are
observed. The scanner event is a reconstruction of the bot's in-memory watch
start, so otherwise plausible cases without direct warmup/watch evidence stay
UNKNOWN. The 77 UNKNOWN first legs break down as 48 missing causal short-flip
probe coverage, 22 unverified warmup ordering, 5 unmatched rests, and 2
ambiguous scanner windows. Historical log formats were parsed for joins but
not treated as proof of live bar provenance.

MYSZ's short flip was 13:59 UTC, followed by a scanner FADE at 14:15 and
re-CONFIRM at 15:17; its 15:18 rest and 15:19:52 Webull fill therefore used
pre-re-add short context. FTFT's 14:15 short flip preceded its 14:49 re-add;
its 15:01 rest and 15:18:57 Webull fill likewise used old context. Their
exact row, intent, order and Fill IDs are in `TRIPS.csv`.

## Audit trail

- Raw logs: `/var/log/project-mai-tai/schwab-1m-v2.log-202609*.gz`,
  `schwab-1m-v2.log-20260928`, and `schwab-1m-v2.log`, copied read-only at
  19:38 ET. Log order is rotation/file order, never alphabetical event text.
- Raw database tables: `oms_managed_positions`, `trade_intents`,
  `broker_orders`, `fills`, `broker_accounts`, `strategies`, and
  `scanner_confirmed_events`, read-only at the fixed cutoff.
- Extraction snapshot SHA-256: Fills
  `3fc65b596a29112665198abd6549c948c162a5d6f89b402dac215fcbb613d297`;
  managed rows
  `87a1e066dacf618230eeda130648089522b1df18d0123a28b90953eda1b24bef`;
  scanner events
  `49623285f97990f5089f3117ba681373bb22ce333c5c83cc372dde9ce08ca983`;
  stage census
  `d5ece50716e371a5a01d382a21f678806b47bbcae3fb43d91adda82279cb5ad2`.
- Reproducible analysis: `scripts/b11_rest_provenance_study.py`; trip-level
  output `TRIPS.csv`, complete machine-readable groups in `RESULTS.json`.
  None of these artifacts changes trading code, flags, services, or orders.
