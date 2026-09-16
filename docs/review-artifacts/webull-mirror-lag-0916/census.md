# Webull resting-mirror lag census — 2026-09-16

Read-only production census, run at 2026-09-16 11:08 ET. Times below are UTC unless
explicitly labelled ET. The Webull resting-mirror flag went live at **2026-08-20 20:16 UTC**. PR
#800's durable segment/slot identity reached production at **2026-08-26 20:10:26 UTC**. PR #976's
exact Webull wire timestamp reached the running OMS at **2026-09-14 21:37:08 UTC**.

## Result

| Population | Count | Meaning |
|---|---:|---|
| All Webull `rth_resting_mirror` open intents since flag-live | 1,388 | Full post-enable population |
| Before durable pair identity | 566 | Kept separate; no honest segment/slot pairing is possible |
| After durable pair identity | 822 | Attributable Webull intents |
| Webull intents with a same-segment, same-slot preceding Schwab primary within 30 s | 726 / 822 | Latency/outcome denominator |
| No attributable Schwab primary | 96 / 822 | Excluded from latency, not guessed; PPCB=50 and MIMI=19 account for most |
| Exact Webull wire timestamp present | 70 / 726 | #976-instrumented denominator |
| Both exact Webull wire and Schwab accepted/entered evidence present | 47 / 726 | Exact lag denominator |

For the 47 exactly timed pairs, Schwab broker-entered time is preferred when its terminal event
retained `enteredTime`; otherwise the accepted-event timestamp is used.

| Lag from Schwab primary entered/accepted to Webull wire | Value |
|---|---:|
| Median | **12.249 s** |
| p90 | **13.038 s** |
| Maximum | **14.560 s** |
| Greater than 2 s | **46 / 47** |
| Greater than 10 s | **44 / 47** |
| Drop MEDS: median | **12.129 s** (30 exactly timed pairs) |
| Drop MEDS: greater than 10 s | **27 / 30** |

The lag therefore survives drop-one MEDS. It is the normal serial-path shape, not one slow symbol.

### Webull outcomes for the 726 attributable pairs

| Outcome | Count |
|---|---:|
| Filled | 91 |
| Working then cancelled | 512 |
| Still working at census | 1 |
| Rejected: `ORDER_RISK_RULE_PRICE_AGGRESSIVE` | 14 |
| Rejected: stop not greater than market | 3 |
| Other rejection | 105 |

The three stop-below-market rows are the same failure class. Only MEDS occurred after #976 began
preserving the exact Webull error code and wire prices, so only that row can be correlated without
reason-string inference:

| Symbol | Schwab accepted | Schwab filled | Webull wire | Lag | Wire stop / limit | Result |
|---|---|---|---|---:|---|---|
| MEDS | 2026-09-16 09:30:04.441 ET | 09:30:12 ET | 09:30:16.477 ET | 12.036 s | 4.31 / 4.33 | HTTP 417 `STOP_PRICE_MUST_BE_GREAT_THAN_MARKET_PRICE` |

## Mechanism trace

1. The strategy creates the Schwab primary and Webull mirror together; MEDS published them 4 ms
   apart.
2. OMS has one serial strategy-intent consumer (`oms/service.py::_run_control_loop`) and awaits each
   message before reading the next. There is no fan-out priority or second consumer.
3. Schwab accepted the primary, but `SchwabBrokerAdapter.submit_order` then polled it to terminal for
   up to 10 s (`_wait_for_terminal_order`, 0.5 s cadence).
4. OMS then awaited post-intent broker reconciliation before returning to the stream. That path runs
   order sync, native-OCO refresh, OCO-exit polling, and position sync.
5. Webull itself was not the delay: after OMS first touched the MEDS sibling it stamped the exact
   wire time one millisecond later.

The fix keeps one consumer. Only a mirror-enabled Schwab resting primary returns after acceptance;
its fill is picked up by the existing periodic broker sync. Its inline reconcile is skipped so the
already-queued Webull sibling runs next. The Webull adapter then revalidates the stop shape against
a fresh OMS ask/last snapshot at the final wire boundary.

## Reproducible SQL

The census ran with `statement_timeout='12s'` inside `BEGIN READ ONLY`. This query shows the durable
population, pairs each Webull intent to the nearest preceding same-segment/same-slot Schwab primary,
and exposes the timestamps used by the aggregate above. Outcome grouping is a direct `GROUP BY` over
the final CTE.

```sql
WITH webull_intents AS (
  SELECT ti.id, ti.symbol, ti.created_at, ti.status,
         ti.payload::jsonb->'metadata' AS md
  FROM trade_intents ti
  JOIN broker_accounts ba ON ba.id = ti.broker_account_id
  WHERE ba.provider = 'webull'
    AND ti.intent_type = 'open'
    AND ti.side = 'buy'
    AND ti.created_at >= '2026-08-26T20:10:26Z'
    AND ti.payload::jsonb->'metadata'->>'fanout_source' = 'rth_resting_mirror'
), pairs AS (
  SELECT w.*,
         s.id AS schwab_intent_id,
         s.created_at AS schwab_created_at
  FROM webull_intents w
  LEFT JOIN LATERAL (
    SELECT ti.id, ti.created_at
    FROM trade_intents ti
    JOIN broker_accounts ba ON ba.id = ti.broker_account_id
    WHERE ba.provider = 'schwab'
      AND ti.intent_type = 'open'
      AND ti.side = 'buy'
      AND ti.symbol = w.symbol
      AND ti.payload::jsonb->'metadata'->>'fanout_segment_id'
          = w.md->>'fanout_segment_id'
      AND ti.payload::jsonb->'metadata'->>'fanout_slot_id'
          = w.md->>'fanout_slot_id'
      AND ti.created_at <= w.created_at
    ORDER BY ti.created_at DESC
    LIMIT 1
  ) s ON true
), orders AS (
  SELECT p.*,
         wo.id AS webull_order_id,
         wo.status AS webull_order_status,
         so.id AS schwab_order_id
  FROM pairs p
  LEFT JOIN LATERAL (
    SELECT bo.id, bo.status
    FROM broker_orders bo
    WHERE bo.intent_id = p.id
    ORDER BY bo.submitted_at, bo.id
    LIMIT 1
  ) wo ON true
  LEFT JOIN LATERAL (
    SELECT bo.id
    FROM broker_orders bo
    WHERE bo.intent_id = p.schwab_intent_id
    ORDER BY bo.submitted_at, bo.id
    LIMIT 1
  ) so ON true
), measured AS (
  SELECT o.*,
    (SELECT min(e.event_at)
       FROM broker_order_events e
      WHERE e.order_id = o.schwab_order_id AND e.event_type = 'accepted') AS accepted_at,
    (SELECT min(e.event_at)
       FROM broker_order_events e
      WHERE e.order_id = o.schwab_order_id AND e.event_type = 'filled') AS schwab_filled_at,
    (SELECT min((substring(e.payload::jsonb->>'reason'
                 FROM '''enteredTime'': ''([^'']+)'''))::timestamptz)
       FROM broker_order_events e
      WHERE e.order_id = o.schwab_order_id
        AND e.payload::jsonb->>'reason' LIKE '%enteredTime%') AS primary_entered_at,
    (SELECT min((e.payload::jsonb->'metadata'
                 ->>'webull_wire_submitted_at_utc')::timestamptz)
       FROM broker_order_events e
      WHERE e.order_id = o.webull_order_id
        AND coalesce(e.payload::jsonb->'metadata'
                     ->>'webull_wire_submitted_at_utc', '') <> '') AS webull_wire_at,
    (SELECT min(e.event_at)
       FROM broker_order_events e
      WHERE e.order_id = o.webull_order_id AND e.event_type = 'filled') AS webull_filled_at,
    (SELECT coalesce(e.payload::jsonb->'metadata'->>'webull_error_code',
                     e.payload::jsonb->>'reason')
       FROM broker_order_events e
      WHERE e.order_id = o.webull_order_id AND e.event_type = 'rejected'
      ORDER BY e.event_at
      LIMIT 1) AS reject_reason
  FROM orders o
)
SELECT *,
       extract(epoch FROM (
         webull_wire_at - coalesce(primary_entered_at, accepted_at)
       )) AS lag_seconds
FROM measured
WHERE schwab_intent_id IS NOT NULL
  AND created_at - schwab_created_at <= interval '30 seconds';
```
