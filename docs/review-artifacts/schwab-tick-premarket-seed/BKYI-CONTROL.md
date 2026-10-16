# BKYI 2026-09-29: bounded ATR signal control

Run after `PROTOCOL.md` was committed at `c0692237`. This is a one-symbol,
read-only calculation during market hours, not the full impact study or a
trading recommendation.

## Sources and bounds

- Production `market_trade_ticks`: provider `schwab`, service
  `LEVELONE_EQUITIES`, symbol `BKYI`, event time 04:00-07:00 ET, raw field
  `35` present, and receive time before 07:01:06 ET. All 8,347 qualifying
  records met the receive-time bound. Their first genuine trade timestamp was
  04:06:32 ET and their last was 06:59:58 ET.
- Minute OHLC was built from observed trade prices, ordered by trade time,
  receive time, and row id. There were 173 printed minute bars in the
  04:00-07:00 interval and 60/60 printed bars in 06:00-07:00. Empty minutes
  were not manufactured.
- Production `strategy_bar_history`: source `live`, strategy
  `schwab_1m_v2`, one-minute BKYI bars 07:00-08:29 ET (90 bars). Schwab owns
  the 07:00 minute and all later minutes. No Massive bars were used.
- The ATR calculation used modified true range, Wilders period 5 with SMA5
  seed, factor 3.5, the 90-second bar-gap guard, and the strategy's
  long-at-first-defined-bar initialization. The 07:00-only control matched
  the production `[V2-ATR-PROBE]` state and trail to six decimals at both
  07:08 and 07:47 ET.

| Bars before 07:00 | 07:08 state / trail | 07:47 state / flip / trail |
|---|---|---|
| None (live control) | long / 2.044100 | long / none / 2.300887 |
| Schwab ticks from 06:00 | short / 2.578636 | long / BUY / 2.286273 |
| Schwab ticks from 04:00 | short / 2.578636 | long / BUY / 2.286273 |

The recorded 07:47 Schwab bar closed at 2.45. Thus the same 07:00-onward
Schwab bars produce the chart-time BUY flip only when the earlier observed
Schwab trade updates supply the starting ATR state. The 06:00 and 04:00
results agree for this symbol; this does not establish the shortest safe seed
window across other stocks.

## Limits

This control proves a signal-path difference for BKYI. It does not replay the
three-short-bar rest, the pre-market streamed cross, the OMS ask/band decision,
the Webull fan-out, a broker fill, or an exit. LEVELONE is a coalesced feed:
minute highs and lows may miss prints. Provider overlap and all-stock-day
coverage remain to be measured under the frozen protocol. No service, flag,
subscription, or order was changed.
