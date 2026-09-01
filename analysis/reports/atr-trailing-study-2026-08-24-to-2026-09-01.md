# ATR Profit-Lock and Trailing Study

Window: seven trading sessions, 2026-08-24 through 2026-09-01, 07:00-16:00 ET. Entries use captured
MACD scanner CONFIRM windows and the corrected V2 two-slot contract. Exits use executable bids.
A stop touch fills on the next captured bid rather than assuming the stop price is guaranteed.

## Mechanic

The practical ladder separates initial risk from earned profit protection:

- Initial floor: -1% or -2% from executable entry.
- Activation: bid reaches +1% or +2%.
- Lock: +1% activation raises the floor to break-even; +2% activation locks +1%.
- Trail: after activation, the stop ratchets at 1% or 2% below the highest observed bid.
- There is no fixed profit-target exit. The trade can continue until the trail, initial floor, or
  16:00 ET closes it.

## Results

| Initial floor | Activation | Profit lock | Trail | Trades | Wins | Losses | Scratches | Total return | Mean |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| -1% | +1% | 0% | 2% | 119 | 9 | 99 | 11 | -91.9212 | -0.7724% |
| -1% | +1% | 0% | 1% | 119 | 17 | 96 | 6 | -96.7586 | -0.8131% |
| -1% | +2% | +1% | 2% | 119 | 12 | 107 | 0 | -102.7047 | -0.8631% |
| -1% | +2% | +1% | 1% | 119 | 12 | 107 | 0 | -108.0994 | -0.9084% |
| -2% | +1% | 0% | 2% | 118 | 21 | 81 | 16 | -116.6805 | -0.9888% |
| -2% | +1% | 0% | 1% | 118 | 35 | 75 | 8 | -117.8369 | -0.9986% |
| -2% | +2% | +1% | 1% | 118 | 32 | 86 | 0 | -126.2603 | -1.0700% |
| -2% | +2% | +1% | 2% | 118 | 31 | 87 | 0 | -127.5536 | -1.0810% |

No tested trailing policy was profitable. The best total-return row still lost -91.92 percentage
points. Trailing improves some winners, but the losing-entry population is too large for exit logic
alone to repair.

For comparison, the corrected fixed +1% / -2% bracket produced 119 trades, 44 wins, 75 losses,
36.98% wins, -122.1084 total percentage points, and -1.0261% mean return. The best trailing row
improved aggregate return by 30.19 points, but remained decisively negative.

## Zero Floor

An immediate 0% floor is mechanically incompatible with ask-side entry on this tape. All 120 trades
exited before the +1% or +2% trailing activation; median holding time was 0.6 seconds. The first
executable bid is normally below the ask fill because of spread. Break-even works only as an earned
floor after a favorable move, not as an immediate hard stop.

## Verified Runner

AEHL on 2026-08-31 demonstrates why trailing is still worth retaining as an exit candidate:

| Slot | Entry | Activation | Bid high | 2% trail exit | Return | Hold |
|---|---:|---:|---:|---:|---:|---:|
| reclaim/reactive | 6.16 | 6.23 | 7.40 | 7.25 | +17.6948% | 109.7s |

The underlying 130-second window contains 2,847 sane captured quotes, with bids moving from 6.05 to
7.40. This is a sustained tape move, not a single crossed or absurd-spread quote.

## Conclusion

Trailing is useful for harvesting the rare runner, but it does not select good entries. The next
research step should apply volume, VWAP, MACD acceleration, and spread filters before entry, then
re-grade the trailing ladder on the reduced population. Using trailing alone preserves too many
losers to meet the goal of roughly five good trades and one bad trade per day.
