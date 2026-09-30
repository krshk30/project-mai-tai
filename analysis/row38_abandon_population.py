"""Generate read-only indexed SQL for Webull mirror stale-market decisions."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path


LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}).*"
    r"\[OMS-FANOUT-MIRROR-LAG\] sym=([A-Z0-9.]+) .*"
    r"slot_id=([0-9a-f-]+) .*"
    r"shape=abandoned_no_fresh_quote$"
)


def cases(path: Path) -> list[tuple[int, str, str, datetime]]:
    result = []
    for line in path.read_text().splitlines():
        match = LINE.match(line)
        if match:
            at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
            result.append((len(result) + 1, match.group(2), match.group(3),
                           at.replace(tzinfo=timezone.utc)))
    return result


def sql(rows: list[tuple[int, str, str, datetime]]) -> str:
    values = ",\n".join(
        f"({index}, '{symbol}', '{slot_id}', '{stamp.isoformat()}'::timestamptz)"
        for index, symbol, slot_id, stamp in rows
    )
    return f"""COPY (
WITH cases(case_id, symbol, slot_id, reject_at) AS (VALUES
{values}
)
SELECT c.case_id, c.symbol, c.slot_id, c.reject_at, i.created_at AS intent_created_at,
  (SELECT count(*) FROM market_capture_trades t
   WHERE t.symbol=c.symbol AND t.event_ts>i.created_at-interval '2 seconds'
     AND t.event_ts<=i.created_at) AS exchange_prints_2s,
  (SELECT count(*) FROM market_capture_trades t
   WHERE t.symbol=c.symbol AND t.event_ts>i.created_at-interval '10 seconds'
     AND t.event_ts<=i.created_at) AS exchange_prints_10s,
  (SELECT max(t.event_ts) FROM market_capture_trades t
   WHERE t.symbol=c.symbol AND t.event_ts<=i.created_at) AS last_exchange_print,
  (SELECT count(*) FROM market_capture_quotes q
   WHERE q.symbol=c.symbol AND q.event_ts>i.created_at-interval '2 seconds'
     AND q.event_ts<=i.created_at) AS gateway_quotes_2s,
  (SELECT count(DISTINCT (q.bid_price,q.ask_price)) FROM market_capture_quotes q
   WHERE q.symbol=c.symbol AND q.event_ts>i.created_at-interval '10 seconds'
     AND q.event_ts<=i.created_at) AS nbbo_states_10s,
  (SELECT max(q.event_ts) FROM market_capture_quotes q
   WHERE q.symbol=c.symbol AND q.event_ts<=i.created_at) AS last_gateway_quote
FROM cases c LEFT JOIN LATERAL (
  SELECT ti.created_at FROM trade_intents ti
  WHERE ti.symbol=c.symbol AND ti.status='rejected'
    AND ti.created_at BETWEEN c.reject_at-interval '5 seconds' AND c.reject_at
    AND ti.payload::jsonb #>> '{{metadata,fanout_slot_id}}'=c.slot_id
    AND ti.payload::jsonb #>> '{{metadata,fanout_source}}'='rth_resting_mirror'
  ORDER BY ti.created_at DESC LIMIT 1
) i ON true ORDER BY c.case_id
) TO STDOUT WITH CSV HEADER;
"""


if __name__ == "__main__":
    rows = cases(Path(sys.argv[1]))
    print(sql(rows))
