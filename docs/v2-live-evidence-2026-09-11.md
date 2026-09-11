# V2 live evidence: 2026-09-11

Captured read-only from the production database and the live `schwab-1m-v2.log` and
`oms.log` before log rotation. The box reflog shows the checkout fast-forwarded from
`a93fe95` to `1940d46e44d28a1e3e011d3299575656bcbc4e35` at 10:13:31 UTC. Running v2 PID
`646497` started nine seconds later at 10:13:40 UTC and had `NRestarts=0`, so both the
checkout and the process that produced this evidence were on `1940d46`.

## FTFT trade

| Account | Entry | Exit | Result |
| --- | --- | --- | --- |
| `live:schwab_1m_v2` | 2 shares at $2.78, 12:02:19 ET | 2 shares at $2.92, 12:12:55 ET | +5.04%, +$0.28 gross |
| `live:orb` | 1 share at $2.775, 12:02:19.808 ET | 1 share at $2.91, 12:12:52.649 ET | +4.86%, +$0.135 gross |

The combined gross P&L was +$0.415 before fees.

## Deployment grading

| Change | Denominator | Observed result | Grade |
| --- | ---: | --- | --- |
| #945 account-neutral confirmation discovery | 1 matured opportunity | `evaluated=1 denominator=1 missing=none` | Partial: discovery ran, but the source fill was Schwab. The Webull-only case remains unexercised. |
| Confirmation decision | 1 evaluation | `STATE_LONG`, `fired=0`, `long=1` | Correct hold. |
| Confirmation fan-out | 2 owned legs | `legs_total=2`, `legs_state_long=2`, no closes or refusals | Both accounts were recognized by one decision. |
| Account-neutral ATR SELL evaluation | 3 SELL observations x 2 accounts | All three reported `accounts_evaluated=2`, `not_owned=2`, `unanswerable=0` | Working for today's flat-at-flip cases. |
| #946 OCO reconciliation | 0 qualifying holds | Broker OCO exits filled cleanly | Unexercised. |
| #947 conditional reset | 0 false flips | No confirmation close occurred | Unexercised. |
| #948 stale-owner session roll | 0 qualifying rolls | No rollover with a stale owner | Unexercised; earliest qualifying evidence is a later session. |

## FTFT owner disposition

Opportunity `1789141202801` was filled on both accounts and bound at 12:03 ET. The target exits
made both accounts flat at 12:13 ET. The opportunity correctly remained active after the target,
so the completed trade continued to consume its ATR segment.

At 12:39:02 ET a genuine SELL flip occurred (`close=2.93`, `trail=3.122077`). The durable identity
record changed opportunity `1789141202801` to `active=false` with reason `sell_flip_flat`. A new
opportunity, `1789144922755`, was minted at 12:42:02 ET and only then was a new first resting order
allowed. The identities never overlapped.

The deployed build records that release only as `[V2-FANOUT-IDENTITY-PERSISTED]`. It does not emit
an owner-level release marker. #958 adds `[V2-FLIP-OWNER-RELEASED]` for this transition but was
merged after the live process started and had not been deployed when this evidence was captured.

## Exact live markers

Log timestamps below are UTC.

```text
2026-09-11 16:04:03,048 [V2-CONFIRMATION-EXIT-STATE_LONG] sym=FTFT ... evaluated=1 fired=0 long=1 denominator=1
2026-09-11 16:04:04,659 [V2-CONFIRMATION-EXIT-DISCOVERY] status=MEASURED evaluated=1 denominator=1 missing=none
2026-09-11 16:04:03,915 [OMS-V2-CONFIRMATION-EXIT-FANOUT] sym=FTFT ... legs_total=2 legs_state_long=2 legs_closed=0
2026-09-11 16:13:09,345 [V2-FLIP-OWNER-PRIMARY-CLOSE] FTFT union=2->0 held=2->0 account_neutral_close_pending=1 entry_released=0
2026-09-11 16:39:02,183 [V2-ATR-PROBE] sym=FTFT ... state=short flip=SELL
2026-09-11 16:39:02,209 [V2-FANOUT-IDENTITY-PERSISTED] FTFT segment_id=1789141202801 active=0 reason=sell_flip_flat persisted=1 could_not_tell=0
2026-09-11 16:42:02,755 [V2-FLIP-OWNER-ADMISSION] FTFT ... allowed=1 slot=first reason=allowed
2026-09-11 16:42:02,762 [V2-FANOUT-IDENTITY-PERSISTED] FTFT segment_id=1789144922755 active=1 reason=flip_owned_opportunity_v2_bind persisted=1 could_not_tell=0
```

The three ATR SELL evaluations were at 10:15:00, 11:37:01, and 12:39:02 ET. Each evaluated both
accounts and returned `owned=0`, `not_owned=2`, and `unanswerable=0`.
