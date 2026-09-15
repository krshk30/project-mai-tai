# PRE07 shadow report

PRE07 is a read-only, after-close measurement of the v2 ATR blind window. The live bot and its
Schwab `CHART_EQUITY` input stay unchanged. The report asks what the canonical ATR oracle would
have seen if Massive trades available from 04:00 ET had supplied the seed, then checks that shadow
against the live Schwab evidence. It does not place orders, publish intents, change flags, or write
to PostgreSQL.

## Run it

Run only after 16:00 ET. The command refuses production queries during 07:00-16:00 ET.

```bash
python -m project_mai_tai.backtest.pre0700_shadow 2026-09-15
python -m project_mai_tai.backtest.pre0700_shadow --range 2026-09-01 2026-09-15
python -m project_mai_tai.backtest.pre0700_shadow --range 2026-09-01 2026-09-15 --json /tmp/pre07.json
```

On the VPS it reads `MAI_TAI_DATABASE_URL` and the production resting-band setting from the
process environment or `/etc/project-mai-tai/project-mai-tai.env` via `sudo -n`. PostgreSQL is
opened with `default_transaction_read_only=on`; every query is a `SELECT`. Current and rotated
`schwab-1m-v2.log*` files, including `.gz`, supply the live 07:08 probe.

## Population and buckets

The population is named on every row: each symbol with at least one `source='live'`
`schwab_1m_v2` bar in 07:00-07:08 ET for that day. Massive coverage is the number of minutes with
at least one trade from 04:00 through 07:07, out of 188 expected minutes.

- **Pile A**: every shadow BUY or SELL flip from 04:00-06:59. These are informational because the
  current entry window was not open.
- **Pile B**: every shadow BUY flip from 07:00-07:07. These are the blind-window opportunities the
  live ATR could not see.
- **Touch**: while the prior shadow state is short, the close reaches the prior trail without
  exceeding the production resting-order band. The report records the prior trail actually
  crossed, not the new post-flip trail.
- **07:08 state**: Massive shadow state/trail, live `[V2-ATR-PROBE]` state/trail, and the canonical
  oracle rerun over the nine live Schwab bars.

Excursions are measured from each Pile-B BUY close over Schwab bars beginning at 07:08, ending at
the next Massive-shadow SELL or 16:00. MFE and MAE are explicitly **excursions, not the live exit
rules**. They are not P&L and no dollars are reported.

## Evidence grades

| Grade | Meaning |
|---|---|
| `FIDELITY_OK` | At least 7 of the 9 overlap bars agree on close, high, and low within 0.1%. |
| `FIDELITY_LOW` | Fewer than 7 of 9 agree. Flips remain visible but are not treated as reliable. |
| `UNKNOWN` | No Massive trade minute exists in 04:00-07:07. This never becomes “zero flips.” |
| `INSTRUMENT_MISMATCH` | The live 07:08 probe is missing/conflicting or disagrees with the Schwab-oracle rerun. Nothing else on that row should be believed. |

Range flip/touch totals exclude `INSTRUMENT_MISMATCH` rows. The report also shows the Pile-B BUY
count restricted to `FIDELITY_OK` rows; that is the only count eligible to inform a later D2
decision. Excursion medians use that same trusted subset.

The overlap detail prints `|delta close| / Schwab close`, absolute high delta, and absolute low
delta for all nine minutes. Missing bars count as disagreement. The LEVELONE cross-check uses only
the last coalesced price update per minute versus the Massive close; LEVELONE volume is not treated
as time-and-sales volume.

## Honest limits

- A minute with no Massive trade has no shadow bar. The report does not synthesize one.
- Massive capture starts when the symbol enters the captured watchlist. Missing earlier minutes
  cannot be reconstructed and lower the stated coverage.
- `LEVELONE_EQUITIES` is coalesced quote/trade state, not a print stream. It is a price-only
  cross-check and cannot replace Massive aggregation.
- The canonical oracle does not model the live ATR bar-gap guard. A sparse shadow series can span
  gaps in its modified true range; coverage and nine-bar fidelity must be read with every result.
- A live probe retained in no current or rotated log produces `INSTRUMENT_MISMATCH`, not a guessed
  canonical state.
- D1 does not claim an executable fill or live-strategy P&L. A replay-engine pre-07 seed adapter
  remains deferred unless the ten-session report repeatedly finds Pile-B BUY flips on
  `FIDELITY_OK` rows and the operator asks for D2.
