"""Required outside-payload evidence for C1 working-window equivalence.

SQL is a reviewable, unexecuted proposal. Python functions provide the same
metadata shape from Main's retained full-input FILE for the two-case proof.
No trading or native permission is inferred from an empty result.
"""

from collections import defaultdict
from decimal import Decimal

from atrsimlive1_working_windows import MINUTE, nanos


def full_census(rows):
    """Reference metadata from a bound, complete full-symbol input, no I/O."""
    grouped = defaultdict(list)
    for row in rows:
        clock = nanos(row["event_ts"])
        grouped[row["symbol"], clock // MINUTE * MINUTE].append(row)
    result = []
    for (symbol, minute), values in sorted(grouped.items()):
        values.sort(key=lambda row: (nanos(row["event_ts"]), row["id"]))
        clocks = [nanos(row["event_ts"]) for row in values]
        valid_clocks = defaultdict(int)
        bad_size, bad_price = 0, 0
        for row, clock in zip(values, clocks):
            if type(row["size"]) is not int or row["size"] < 0:
                bad_size += 1
                continue
            valid_clocks[clock] += 1
            price = Decimal(str(row["price"]))
            bad_price += int(not price.is_finite() or price <= 0)
        result.append(dict(symbol=symbol, minute_ns=minute, row_count=len(values),
            first_ns=clocks[0], last_ns=clocks[-1], first_id=values[0]["id"], last_id=values[-1]["id"],
            max_gap_ns=max((b - a for a, b in zip(clocks, clocks[1:])), default=0),
            invalid_size_count=bad_size, invalid_price_count=bad_price,
            same_sip_valid_groups=sum(count > 1 for count in valid_clocks.values()),
            invalid_size_ns=[clock for row, clock in zip(values, clocks)
                             if type(row['size']) is not int or row['size'] < 0],
            same_sip_ns=[clock for clock, count in valid_clocks.items() if count > 1]))
    return result


def condition_groups(rows):
    """Actual representative rows + weights, never fabricated market prints.

    Classification depends on original conditions/presence and the round-lot
    class; each representative is a real bound input row from that class.
    """
    grouped = {}
    for row in rows:
        if type(row["size"]) is not int or row["size"] < 0:
            continue
        raw = row.get('conditions')
        if raw is not None and not isinstance(raw, str):
            raise ValueError("stored_SQL_condition_text_shape_required")
        minute = nanos(row["event_ts"]) // MINUTE * MINUTE
        key = row["symbol"], minute, row["size"] >= 100, 'conditions' in row, raw
        if key not in grouped:
            grouped[key] = dict(symbol=row["symbol"], minute_ns=minute, count=0, representative=row)
        grouped[key]["count"] += 1
    return list(grouped.values())


def condition_totals(groups, *, start_ns, end_ns, policy, source, null_policy=None):
    from atrsimlive1_null_policy import classify_row_execution, require_null_policy
    from project_mai_tai.atr_simulation.types import EventKey, TradePrint

    require_null_policy(null_policy)
    total = unknown = blank = missing = 0
    for group in groups:
        if not start_ns <= group["minute_ns"] < end_ns:
            continue
        row, count = group["representative"], group["count"]
        if type(count) is not int or count <= 0:
            raise ValueError("positive_actual_condition_group_count_required")
        trade = TradePrint(EventKey(nanos(row["event_ts"]), row["id"],
            "market_capture_trades:" + str(row["id"]), source, nanos(row["received_at"])),
            row["symbol"], Decimal(str(row["price"])), row["size"])
        evidence = classify_row_execution(trade, row, policy, null_policy=null_policy)
        total += count
        unknown += count if evidence.eligible is None else 0
        missing += count if not evidence.conditions_present else 0
        blank += count if evidence.conditions_present and evidence.raw_conditions in (None, "", ()) else 0
    return dict(total_prints=total, blank_or_null_prints=blank,
                actual_blank_or_null_pct=None if not total else Decimal(blank) * 100 / total,
                missing_condition_evidence_prints=missing, unknown_eligibility_prints=unknown,
                note="ALL raw prints; CSV blank conflates empty/NULL. No X threshold invented.")


# Bound ALL original roster symbols and SAME snapshot/day/provider/asof. These
# are lightweight census reads, not payload substitutes or input permission.
MINUTE_CENSUS_SQL = """WITH raw AS (
 SELECT symbol,id,event_ts,size,price,
 (date_trunc('minute',event_ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS minute
 FROM market_capture_trades WHERE provider='massive'
 AND symbol=ANY(%(symbols)s::text[]) AND event_ts >= %(start)s AND event_ts < %(end)s
 AND received_at <= %(asof)s
), events AS (
 SELECT symbol,minute,event_ts,count(*) AS n,min(id) AS first_id,max(id) AS last_id,
 count(*) FILTER (WHERE size IS NULL OR size < 0) AS bad_size,
 count(*) FILTER (WHERE size >= 0 AND (price IS NULL OR price <= 0
   OR price::text IN ('NaN','Infinity','-Infinity'))) AS bad_price,
 count(*) FILTER (WHERE size >= 0) AS valid_n
 FROM raw GROUP BY symbol,minute,event_ts
), adjacent AS (
 SELECT *,lag(event_ts) OVER (PARTITION BY symbol,minute ORDER BY event_ts) AS previous
 FROM events
)
SELECT symbol,minute,count(*) AS event_groups,sum(n)::bigint AS row_count,
 min(event_ts) AS first_ts,max(event_ts) AS last_ts,
 (array_agg(first_id ORDER BY event_ts))[1] AS first_id,
 (array_agg(last_id ORDER BY event_ts DESC))[1] AS last_id,
 coalesce(max(extract(epoch FROM event_ts-previous)*1000000000),0)::bigint AS max_gap_ns,
 sum(bad_size)::bigint AS invalid_size_count,sum(bad_price)::bigint AS invalid_price_count,
 count(*) FILTER (WHERE valid_n > 1) AS same_sip_valid_groups,
 array_agg(event_ts ORDER BY event_ts) FILTER (WHERE bad_size > 0) AS invalid_size_times,
 array_agg(event_ts ORDER BY event_ts) FILTER (WHERE valid_n > 1) AS same_sip_times
FROM adjacent GROUP BY symbol,minute ORDER BY symbol,minute LIMIT 100001"""

CONDITION_GROUPS_SQL = """WITH grouped AS (
 SELECT symbol,(date_trunc('minute',event_ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS minute,
 conditions,(size >= 100) AS round_lot,count(*) AS row_count,min(id) AS representative_id
 FROM market_capture_trades WHERE provider='massive'
 AND symbol=ANY(%(symbols)s::text[]) AND event_ts >= %(start)s AND event_ts < %(end)s
 AND received_at <= %(asof)s AND size >= 0
 GROUP BY symbol,minute,conditions,(size >= 100)
)
SELECT g.minute,g.row_count,t.id,t.provider,t.symbol,t.event_ts,t.received_at,
 t.price,t.size,t.exchange,t.conditions,t.cumulative_volume
FROM grouped g JOIN market_capture_trades t ON t.id=g.representative_id
ORDER BY t.symbol,g.minute,g.representative_id LIMIT 100001"""

QUOTE_COUNTS_SQL = """SELECT symbol,count(*) AS row_count
FROM market_capture_quotes WHERE provider='massive' AND symbol=ANY(%(symbols)s::text[])
AND event_ts >= %(start)s AND event_ts < %(end)s AND received_at <= %(asof)s
GROUP BY symbol ORDER BY symbol LIMIT 257"""


def normalize_census(rows):
    """SQL exact event clocks, never widen a fault to its enclosing minute."""
    return [dict(symbol=row['symbol'], minute_ns=nanos(row['minute']),
        row_count=int(row['row_count']), first_ns=nanos(row['first_ts']),
        last_ns=nanos(row['last_ts']), first_id=row['first_id'], last_id=row['last_id'],
        max_gap_ns=int(row['max_gap_ns']), invalid_size_count=int(row['invalid_size_count']),
        invalid_price_count=int(row['invalid_price_count']),
        same_sip_valid_groups=int(row['same_sip_valid_groups']),
        invalid_size_ns=[nanos(at) for at in (row['invalid_size_times'] or ())],
        same_sip_ns=[nanos(at) for at in (row['same_sip_times'] or ())]) for row in rows]


def normalize_conditions(rows):
    return [dict(symbol=row['symbol'], minute_ns=nanos(row['minute']),
                 count=int(row['row_count']),
                 representative={key: value for key, value in row.items()
                                 if key not in ('minute', 'row_count')}) for row in rows]
