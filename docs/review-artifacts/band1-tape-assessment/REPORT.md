# BAND1: 0.5% resting band and twelve unfilled candidates

Read-only assessment on 2026-09-28 after 16:35 ET. This is not a fill
simulation or a change to the entry band. The operator's 2026-09-21 ruling
keeps the 0.5% band until a second genuine missed winner is shown.

## Method

- Candidate timestamps, resting triggers, limits, and flip bars come from
  `/var/log/project-mai-tai/schwab-1m-v2.log-20260922.gz`,
  `-20260924.gz`, and `-20260925.gz`; the three pre-market refusal limits
  come from `oms.log-20260924.gz`, `oms.log-20260928`, and `oms.log`.
- Price, size, exchange, and event time come from the read-only production
  `market_capture_trades` table with `provider='massive'`. The nine RTH
  windows are the one-minute flip bars identified by `bar_ts`; the three
  pre-market windows are the minute containing `ASK_PAST_BAND`.
- `Above s` is a last-sale dwell estimate: for every above-cap print, the
  interval to the next print is counted, capped at one second per gap. It is
  not a measure of executable ask liquidity. `Above vol` sums sizes of
  above-cap prints. In-band prints after the first stop-triggering print
  are counted separately for RTH.
- Hypothetical 30-minute MFE/MAE uses the cap as an assumed buy price. It
  begins at the refusal for pre-market, or the first print reaching the
  resting trigger in the flip bar for RTH. The +5/-5 order is determined
  from event timestamps. These are raw tape paths, not actual strategy
  fills, exits, or executable quotes. No fees or spread are included.

| Candidate (ET) | Class from tape | Cap | Above s / vol | In-band after trigger | 30m MFE / MAE | Raw +5 vs -5 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| WHLR 09-23 08:45 | PM explicit band refusal | 5.1056 | 3.320 / 7,070 | n/a | +36.71% / -10.87% | -5 first |
| IPDN 09-23 09:21 | PM explicit band refusal | 6.9200 | 0.554 / 57 | n/a | +11.27% / -11.42% | -5 first |
| CLRO 09-28 08:51 | PM explicit band refusal | 5.5560 | 1.047 / 795 | n/a | +5.65% / -2.63% | raw +5 first; not quote-confirmed |
| LOBO 09-21 11:35 | RTH in-band prints, band cause unproven | 0.9233 | 39.137 / 644,562 | 76 / 34,616 shares | +3.97% / -15.20% | -5 first |
| LOBO 09-21 12:57 | RTH in-band prints, band cause unproven | 0.7890 | 1.413 / 62,132 | 18 / 6,127 shares | +11.53% / -2.28% | +5 first |
| LOBO 09-21 14:23 | RTH in-band prints, band cause unproven | 0.7904 | 16.074 / 46,197 | 62 / 41,334 shares | +3.50% / -3.21% | neither |
| LOBO 09-21 15:02 | RTH in-band prints, band cause unproven | 0.8068 | 2.575 / 7,628 | 20 / 5,955 shares | +1.64% / -3.90% | neither |
| IPDN 09-23 11:51 | RTH in-band prints, band cause unproven | 5.4089 | 38.349 / 18,307 | 59 / 4,484 shares | +1.68% / -7.19% | -5 first |
| GLND 09-24 12:07 | stop never triggered in flip bar | 4.4614 | 0 / 0 | 0 | n/a | n/a |
| NCPL 09-24 13:44 | triggered, no above-cap print | 1.4632 | 0 / 0 | 3 / 250 shares | -0.22% / -7.74% | -5 first |
| GLND 09-24 14:38 | stop never triggered in flip bar | 4.6184 | 0 / 0 | 0 | n/a | n/a |
| PMAX 09-24 15:49 | stop never triggered in flip bar | 1.6837 | 0 / 0 | 0 | n/a | n/a |

## Interpretation and remaining limits

Three RTH candidates never triggered. The six other RTH candidates traded
inside their stop-to-cap band after the stop triggered, so an unfilled broker
STOP_LIMIT cannot be attributed to the cap alone; the LOBO 12:57 raw +5
follow-through is real tape movement, but not a proven band miss. A trade
inside the band is not proof the broker could fill our order either.

Of the three explicit pre-market band refusals, WHLR and IPDN reached -5
before +5 on the raw 30-minute tape. CLRO's only +5 prints were 21
one-share Massive trades at 5.84-5.87; Schwab captured one one-share print
at 5.84. Massive and Schwab quotes reached a maximum bid of 5.78 during
the full 30 minutes, below the +5 threshold of 5.8338. CLRO therefore
does not establish an executable +5 exit. The actual Webull CLRO entry at
5.56 closed at 5.5528 by confirmation exit, not at +5.

**Proven second genuine band-miss winner: 0 of 12.** This does not prove
the 0.5% cap is optimal. It means these twelve cases do not yet satisfy the
operator's threshold for changing it. The separate Webull pre-market cap
alignment in #1055 is not evaluated as a live post-deployment outcome here.
