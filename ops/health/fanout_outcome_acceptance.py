#!/usr/bin/env python3
"""One-session D6 acceptance report for the Section 82 outcome consumer.

The report deliberately grades external outcomes, not consumer markers.  One run prints four
independent populations: shared-identity pairing, matched resting fill rate, venue-local duplicate
fills, and post-exit refused sells.  A zero is a result only when that metric's denominator is
non-zero.

Before reading the requested window, the SQL must reproduce the measured known-bad baseline.  A
schema, join, or retention change that moves the baseline makes the entire run COULD_NOT_TELL rather
than silently answering a different question.

Exit codes:
  0  PASS                 applicable metrics clean; comparisons N/A only with sealed route proof
  1  FAIL                 at least one exercised metric remains at/below the known-bad outcome
  2  COULD_NOT_TELL       query/control/identity evidence is incomplete
  3  UNEXERCISED          at least one metric has a zero denominator
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import io
import json
import math
import statistics
import subprocess
from typing import Sequence

from project_mai_tai.fanout_identity import ATR_COUNTRY_ROUTE_KEYS, fanout_slot_id


PASS = 0
FAIL = 1
COULD_NOT_TELL = 2
UNEXERCISED = 3

BASE_PAIR_TOTAL = 53
BASE_PAIR_USABLE = 16
BASE_PAIR_COULD_NOT_TELL = 37
BASE_PAIR_PAIRED = 7
BASE_PAIR_WEBULL_ONLY = 9
BASE_MIRROR_ORDERS = 292
BASE_MIRROR_FILLS = 18
BASE_SCHWAB_ORDERS = 368
BASE_SCHWAB_FILLS = 34
BASE_DUPLICATE_LEGS = 22
BASE_DUPLICATE_WORSE = 22
BASE_DUPLICATE_MEDIAN_PCT = 4.58
BASE_REFUSED = {
    # Closed-day durable rows. The reported 2026-08-27 count of 2 was an intraday snapshot; it
    # reached 38 before this report was built and therefore cannot be an immutable control.
    "2026-08-24": (37, 9, 2, 28),
    "2026-08-25": (25, 9, 2, 16),
    "2026-08-26": (49, 49, 11, 0),
}


@dataclass(frozen=True)
class RawRow:
    kind: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class FillLeg:
    account: str
    order_id: str
    symbol: str
    filled_at: str
    price: float
    segment_id: str
    slot: str
    slot_id: str
    source: str


@dataclass(frozen=True)
class Metric:
    name: str
    verdict: str
    line: str


@dataclass(frozen=True)
class Report:
    exit_code: int
    verdict: str
    lines: tuple[str, ...]


class EvidenceFailure(RuntimeError):
    """The read-only evidence source did not answer the declared question."""


SQL = r"""
BEGIN READ ONLY;
COPY (
WITH
target_bounds AS (
    SELECT :'window_since'::timestamptz AS since_at,
           :'window_until'::timestamptz AS until_at
),
fill_by_order AS (
    SELECT
        f.order_id,
        min(f.filled_at) AS first_fill_at,
        sum(f.price * f.quantity) / nullif(sum(f.quantity), 0) AS average_price
    FROM fills f
    WHERE f.side = 'buy'
    GROUP BY f.order_id
),
sell_fill_by_order AS (
    SELECT f.order_id, min(f.filled_at) AS first_fill_at
    FROM fills f
    WHERE f.side = 'sell'
    GROUP BY f.order_id
),

-- Fixed measured control: 53 Webull fan-out fills, 16 usable legacy arm joins, 37 CTT.
base_pair_buys AS (
    SELECT
        bo.id,
        ba.name AS account,
        bo.symbol,
        coalesce(nullif(bo.payload::jsonb->>'cw_arm_bar_ts', ''), '0') AS arm_id,
        bo.payload::jsonb AS payload
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    JOIN fill_by_order fbo ON fbo.order_id = bo.id
    WHERE bo.side = 'buy'
      AND fbo.first_fill_at >= timestamptz '2026-08-21 00:00 America/New_York'
      AND fbo.first_fill_at <  timestamptz '2026-08-27 00:00 America/New_York'
      AND ba.name IN ('live:orb', 'live:schwab_1m_v2')
),
base_pair_webull AS (
    SELECT * FROM base_pair_buys
    WHERE account = 'live:orb' AND payload ? 'fanout_source'
),
base_pair_control AS (
    SELECT
        count(*) AS total,
        count(*) FILTER (WHERE arm_id <> '0') AS usable,
        count(*) FILTER (WHERE arm_id = '0') AS ctt,
        count(*) FILTER (
            WHERE arm_id <> '0' AND EXISTS (
                SELECT 1 FROM base_pair_buys s
                WHERE s.account = 'live:schwab_1m_v2'
                  AND s.symbol = w.symbol AND s.arm_id = w.arm_id
            )
        ) AS paired,
        count(*) FILTER (
            WHERE arm_id <> '0' AND NOT EXISTS (
                SELECT 1 FROM base_pair_buys s
                WHERE s.account = 'live:schwab_1m_v2'
                  AND s.symbol = w.symbol AND s.arm_id = w.arm_id
            )
        ) AS webull_only
    FROM base_pair_webull w
),

-- Fixed matched-shape control: identical window and 12 symbols on both brokers.
base_fill_bounds AS (
    SELECT timestamptz '2026-08-21 00:00 America/New_York' AS since_at,
           timestamptz '2026-08-26 00:00 America/New_York' AS until_at
),
base_mirror_symbols AS (
    SELECT DISTINCT bo.symbol
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    CROSS JOIN base_fill_bounds b
    WHERE ba.name = 'live:orb'
      AND bo.side = 'buy'
      AND upper(bo.order_type::text) = 'STOP_LIMIT'
      AND bo.payload::jsonb->>'fanout_source' = 'rth_resting_mirror'
      AND bo.submitted_at >= b.since_at AND bo.submitted_at < b.until_at
),
base_matched_orders AS (
    SELECT
        ba.name AS account,
        bo.id,
        bo.payload::jsonb->>'fanout_source' AS fanout_source,
        EXISTS (SELECT 1 FROM fills f WHERE f.order_id = bo.id AND f.side = 'buy') AS filled
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    CROSS JOIN base_fill_bounds b
    WHERE bo.side = 'buy'
      AND upper(bo.order_type::text) = 'STOP_LIMIT'
      AND bo.symbol IN (SELECT symbol FROM base_mirror_symbols)
      AND ba.name IN ('live:orb', 'live:schwab_1m_v2')
      AND bo.submitted_at >= b.since_at AND bo.submitted_at < b.until_at
),
base_fill_control AS (
    SELECT
        count(*) FILTER (
            WHERE account = 'live:orb' AND fanout_source = 'rth_resting_mirror'
        ) AS mirror_orders,
        count(*) FILTER (
            WHERE account = 'live:orb' AND fanout_source = 'rth_resting_mirror' AND filled
        ) AS mirror_fills,
        count(*) FILTER (WHERE account = 'live:schwab_1m_v2') AS schwab_orders,
        count(*) FILTER (WHERE account = 'live:schwab_1m_v2' AND filled) AS schwab_fills,
        (SELECT count(*) FROM base_mirror_symbols) AS matched_symbols
    FROM base_matched_orders
),

-- Fixed §82 duplicate-cost control. Its legacy grouping is kept only to prove the instrument can
-- still reproduce the published 22/all-worse/4.58% known-bad result.
base_dup_legs AS (
    SELECT
        bo.id,
        bo.symbol,
        bo.submitted_at,
        coalesce(nullif(bo.payload::jsonb->>'fanout_segment_id', ''),
                 nullif(bo.payload::jsonb->>'cw_arm_bar_ts', ''), '0') AS segment_id,
        coalesce(bo.payload::jsonb->>'cw_entry_n', '') AS entry_n,
        fbo.average_price AS price
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    JOIN fill_by_order fbo ON fbo.order_id = bo.id
    WHERE ba.name = 'live:orb'
      AND bo.status = 'filled'
      AND bo.side = 'buy'
      AND bo.payload::jsonb ? 'fanout_source'
      AND bo.submitted_at >= timestamptz '2026-08-01 00:00:00+00'
      AND bo.submitted_at <  timestamptz '2026-08-19 17:19:55+00'
),
base_duplicate_segments AS (
    SELECT symbol, segment_id
    FROM (
        SELECT symbol, segment_id, entry_n, count(*) AS leg_count
        FROM base_dup_legs
        WHERE segment_id <> '0'
        GROUP BY symbol, segment_id, entry_n
    ) grouped_slots
    GROUP BY symbol, segment_id
    HAVING max(leg_count) > 1
),
base_dup_ranked AS (
    SELECT
        l.*,
        row_number() OVER (
            PARTITION BY l.symbol, l.segment_id ORDER BY l.submitted_at, l.id
        ) AS leg_number,
        first_value(l.price) OVER (
            PARTITION BY l.symbol, l.segment_id ORDER BY l.submitted_at, l.id
        ) AS first_price
    FROM base_dup_legs l
    JOIN base_duplicate_segments d USING (symbol, segment_id)
),
base_dup_control AS (
    SELECT
        count(*) AS extra_legs,
        count(*) FILTER (WHERE price > first_price) AS worse_legs,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY 100.0 * (price / first_price - 1.0))
            AS median_worse_pct
    FROM base_dup_ranked
    WHERE leg_number > 1
),

-- Fixed C3 baseline. `episodes` retains the original distinct preceding-sell-fill denominator.
base_refused AS (
    SELECT
        e.id,
        e.event_at,
        bo.broker_account_id,
        bo.symbol,
        (e.event_at AT TIME ZONE 'America/New_York')::date AS session_date_et
    FROM broker_order_events e
    JOIN broker_orders bo ON bo.id = e.order_id
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    WHERE ba.provider = 'webull'
      AND bo.side = 'sell'
      AND e.event_type = 'rejected'
      AND upper(coalesce(e.payload::jsonb->>'reason', '')) LIKE
          '%NEW_NO_POSITION%CAN_NOT_SELL_SHORT%'
      AND e.event_at >= timestamptz '2026-08-24 00:00 America/New_York'
      AND e.event_at <  timestamptz '2026-08-27 00:00 America/New_York'
),
base_refused_classified AS (
    SELECT r.*, prior_fill.id AS exit_fill_id
    FROM base_refused r
    LEFT JOIN LATERAL (
        SELECT f.id
        FROM fills f
        WHERE f.broker_account_id = r.broker_account_id
          AND f.symbol = r.symbol
          AND f.side = 'sell'
          AND f.filled_at <= r.event_at
          AND (f.filled_at AT TIME ZONE 'America/New_York')::date = r.session_date_et
        ORDER BY f.filled_at DESC, f.id DESC
        LIMIT 1
    ) prior_fill ON TRUE
),
base_refused_control AS (
    SELECT
        session_date_et::text AS day,
        count(*) AS refused,
        count(*) FILTER (WHERE exit_fill_id IS NOT NULL) AS classified,
        count(DISTINCT exit_fill_id) FILTER (WHERE exit_fill_id IS NOT NULL) AS episodes,
        count(*) FILTER (WHERE exit_fill_id IS NULL) AS no_prior_fill
    FROM base_refused_classified
    GROUP BY session_date_et
),

-- Requested-window filled BUY legs. One row is one filled broker order, not one partial fill row.
target_buy_legs AS (
    SELECT
        ba.name AS account,
        bo.id::text AS order_id,
        upper(bo.symbol) AS symbol,
        fbo.first_fill_at,
        fbo.average_price,
        coalesce(bo.payload::jsonb->>'fanout_segment_id', '') AS segment_id,
        coalesce(bo.payload::jsonb->>'fanout_slot', '') AS slot,
        coalesce(bo.payload::jsonb->>'fanout_slot_id', '') AS slot_id,
        coalesce(bo.payload::jsonb->>'fanout_source', '') AS source
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    JOIN fill_by_order fbo ON fbo.order_id = bo.id
    CROSS JOIN target_bounds b
    WHERE bo.side = 'buy'
      AND ba.name IN ('live:orb', 'live:schwab_1m_v2')
      AND fbo.first_fill_at >= b.since_at AND fbo.first_fill_at < b.until_at
      AND (
          (ba.name = 'live:orb' AND bo.payload::jsonb ? 'fanout_source')
          OR nullif(bo.payload::jsonb->>'fanout_segment_id', '') IS NOT NULL
          OR bo.payload::jsonb ? 'atr_country_schema'
      )
),
target_mirror_symbols AS (
    SELECT DISTINCT bo.symbol
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    CROSS JOIN target_bounds b
    WHERE ba.name = 'live:orb'
      AND bo.side = 'buy'
      AND upper(bo.order_type::text) = 'STOP_LIMIT'
      AND bo.payload::jsonb->>'fanout_source' = 'rth_resting_mirror'
      AND bo.submitted_at >= b.since_at AND bo.submitted_at < b.until_at
),
target_matched_orders AS (
    SELECT
        ba.name AS account,
        bo.id::text AS order_id,
        upper(bo.symbol) AS symbol,
        bo.status::text AS status,
        EXISTS (SELECT 1 FROM fills f WHERE f.order_id = bo.id AND f.side = 'buy') AS filled,
        coalesce(bo.payload::jsonb->>'fanout_source', '') AS source
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    CROSS JOIN target_bounds b
    WHERE bo.side = 'buy'
      AND upper(bo.order_type::text) = 'STOP_LIMIT'
      AND bo.symbol IN (SELECT symbol FROM target_mirror_symbols)
      AND ba.name IN ('live:orb', 'live:schwab_1m_v2')
      AND bo.submitted_at >= b.since_at AND bo.submitted_at < b.until_at
),
-- Routing evidence includes unfilled submitted legs: an absent opposite fill must
-- not erase a genuine two-venue expectation. Country seals are never inferred by date.
target_route_orders AS (
    SELECT ba.name AS account, bo.id::text AS order_id, upper(bo.symbol) AS symbol,
           bo.payload::text AS payload, ba.provider::text AS provider,
           coalesce(bo.submitted_at::text, '') AS submitted_at,
           coalesce(fbo.first_fill_at::text, '') AS first_fill_at
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    LEFT JOIN fill_by_order fbo ON fbo.order_id = bo.id
    CROSS JOIN target_bounds b
    WHERE bo.side = 'buy'
      AND ba.name IN ('live:orb', 'live:schwab_1m_v2')
      AND (bo.id::text IN (SELECT order_id FROM target_buy_legs)
           OR bo.id::text IN (SELECT order_id FROM target_matched_orders)
           OR (bo.submitted_at >= b.since_at AND bo.submitted_at < b.until_at
               AND (bo.payload::jsonb ? 'fanout_source'
                    OR bo.payload::jsonb ? 'fanout_segment_id'
                    OR bo.payload::jsonb ? 'atr_country_schema')))
),
target_refused AS (
    SELECT
        e.id::text AS event_id,
        e.event_at,
        bo.broker_account_id,
        bo.symbol,
        (e.event_at AT TIME ZONE 'America/New_York')::date AS session_date_et
    FROM broker_order_events e
    JOIN broker_orders bo ON bo.id = e.order_id
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    CROSS JOIN target_bounds b
    WHERE ba.provider = 'webull'
      AND bo.side = 'sell'
      AND e.event_type = 'rejected'
      AND upper(coalesce(e.payload::jsonb->>'reason', '')) LIKE
          '%NEW_NO_POSITION%CAN_NOT_SELL_SHORT%'
      AND e.event_at >= b.since_at AND e.event_at < b.until_at
),
target_refused_classified AS (
    SELECT r.*, prior_fill.id::text AS exit_fill_id
    FROM target_refused r
    LEFT JOIN LATERAL (
        SELECT f.id
        FROM fills f
        WHERE f.broker_account_id = r.broker_account_id
          AND f.symbol = r.symbol
          AND f.side = 'sell'
          AND f.filled_at <= r.event_at
          AND (f.filled_at AT TIME ZONE 'America/New_York')::date = r.session_date_et
        ORDER BY f.filled_at DESC, f.id DESC
        LIMIT 1
    ) prior_fill ON TRUE
),
target_exit_episodes AS (
    SELECT
        bo.id::text AS order_id,
        (sfbo.first_fill_at AT TIME ZONE 'America/New_York')::date::text AS session_date_et
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    JOIN sell_fill_by_order sfbo ON sfbo.order_id = bo.id
    CROSS JOIN target_bounds b
    WHERE ba.name = 'live:orb'
      AND bo.side = 'sell'
      AND sfbo.first_fill_at >= b.since_at AND sfbo.first_fill_at < b.until_at
),
rows AS (
    SELECT 'CONTROL_PAIR'::text kind,
           total::text c1, usable::text c2, ctt::text c3, paired::text c4,
           webull_only::text c5, ''::text c6, ''::text c7, ''::text c8, ''::text c9
    FROM base_pair_control
    UNION ALL
    SELECT 'CONTROL_FILL', mirror_orders::text, mirror_fills::text,
           schwab_orders::text, schwab_fills::text, matched_symbols::text,
           '', '', '', ''
    FROM base_fill_control
    UNION ALL
    SELECT 'CONTROL_DUP', extra_legs::text, worse_legs::text,
           round(median_worse_pct::numeric, 2)::text, '', '', '', '', '', ''
    FROM base_dup_control
    UNION ALL
    SELECT 'CONTROL_REFUSED', day, refused::text, classified::text, episodes::text,
           no_prior_fill::text, '', '', '', ''
    FROM base_refused_control
    UNION ALL
    SELECT 'FANOUT_FILL', account, order_id, symbol, first_fill_at::text,
           average_price::text, segment_id, slot, slot_id, source
    FROM target_buy_legs
    UNION ALL
    SELECT 'MATCHED_ORDER', account, order_id, symbol, status, filled::text,
           source, '', '', ''
    FROM target_matched_orders
    UNION ALL
    SELECT 'ROUTE_ORDER', account, order_id, symbol, payload, provider,
           submitted_at, first_fill_at, '', ''
    FROM target_route_orders
    UNION ALL
    SELECT 'REFUSAL', event_id, session_date_et::text, coalesce(exit_fill_id, ''),
           '', '', '', '', '', ''
    FROM target_refused_classified
    UNION ALL
    SELECT 'EXIT_EPISODE', order_id, session_date_et, '', '', '', '', '', '', ''
    FROM target_exit_episodes
)
SELECT kind, c1, c2, c3, c4, c5, c6, c7, c8, c9
FROM rows
ORDER BY kind, c1, c2, c3
) TO STDOUT WITH (FORMAT CSV, HEADER TRUE);
COMMIT;
""".strip()


def parse_instant(raw: str, label: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO instant: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must carry Z or an explicit UTC offset")
    return value.astimezone(UTC)


def parse_rows(raw: str) -> tuple[RawRow, ...]:
    reader = csv.DictReader(io.StringIO(raw))
    expected = {"kind", *(f"c{index}" for index in range(1, 10))}
    if reader.fieldnames is None or set(reader.fieldnames) != expected:
        raise EvidenceFailure("database output header does not match D6 schema")
    rows: list[RawRow] = []
    for row in reader:
        kind = str(row["kind"] or "").strip()
        if not kind:
            raise EvidenceFailure("database output contains a row with no kind")
        rows.append(RawRow(kind, tuple(str(row[f"c{i}"] or "") for i in range(1, 10))))
    return tuple(rows)


def _int(raw: str, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise EvidenceFailure(f"{label} is not an integer") from exc
    if value < 0:
        raise EvidenceFailure(f"{label} is negative")
    return value


def _one(rows: Sequence[RawRow], kind: str) -> RawRow:
    matches = [row for row in rows if row.kind == kind]
    if len(matches) != 1:
        raise EvidenceFailure(f"expected exactly one {kind} row, received {len(matches)}")
    return matches[0]


def validate_controls(rows: Sequence[RawRow]) -> None:
    pair = _one(rows, "CONTROL_PAIR").values
    actual_pair = tuple(_int(pair[i], f"CONTROL_PAIR c{i + 1}") for i in range(5))
    expected_pair = (
        BASE_PAIR_TOTAL,
        BASE_PAIR_USABLE,
        BASE_PAIR_COULD_NOT_TELL,
        BASE_PAIR_PAIRED,
        BASE_PAIR_WEBULL_ONLY,
    )
    if actual_pair != expected_pair:
        raise EvidenceFailure(f"paired-leg baseline moved: expected {expected_pair}, got {actual_pair}")

    fills = _one(rows, "CONTROL_FILL").values
    actual_fills = tuple(_int(fills[i], f"CONTROL_FILL c{i + 1}") for i in range(5))
    expected_fills = (
        BASE_MIRROR_ORDERS,
        BASE_MIRROR_FILLS,
        BASE_SCHWAB_ORDERS,
        BASE_SCHWAB_FILLS,
        12,
    )
    if actual_fills != expected_fills:
        raise EvidenceFailure(f"matched fill-rate baseline moved: expected {expected_fills}, got {actual_fills}")

    duplicate = _one(rows, "CONTROL_DUP").values
    actual_duplicate = (
        _int(duplicate[0], "CONTROL_DUP extras"),
        _int(duplicate[1], "CONTROL_DUP worse"),
        float(duplicate[2]),
    )
    expected_duplicate = (
        BASE_DUPLICATE_LEGS,
        BASE_DUPLICATE_WORSE,
        BASE_DUPLICATE_MEDIAN_PCT,
    )
    if actual_duplicate != expected_duplicate:
        raise EvidenceFailure(
            f"duplicate-cost baseline moved: expected {expected_duplicate}, got {actual_duplicate}"
        )

    refusal_rows = [row for row in rows if row.kind == "CONTROL_REFUSED"]
    actual_refused = {
        row.values[0]: tuple(
            _int(row.values[index], f"CONTROL_REFUSED c{index + 1}")
            for index in range(1, 5)
        )
        for row in refusal_rows
    }
    if actual_refused != BASE_REFUSED:
        raise EvidenceFailure(
            f"refused-exit baseline moved: expected {BASE_REFUSED}, got {actual_refused}"
        )
    for row in refusal_rows:
        refused = _int(row.values[1], "CONTROL_REFUSED count")
        classified = _int(row.values[2], "CONTROL_REFUSED classified")
        no_prior = _int(row.values[4], "CONTROL_REFUSED no-prior")
        if classified + no_prior != refused:
            raise EvidenceFailure("refused-exit baseline buckets do not reconcile")


def _parse_fill(row: RawRow) -> FillLeg:
    try:
        price = float(row.values[4])
    except ValueError as exc:
        raise EvidenceFailure("fan-out fill price is not numeric") from exc
    if not math.isfinite(price) or price <= 0:
        raise EvidenceFailure("fan-out fill price is not positive and finite")
    return FillLeg(
        account=row.values[0],
        order_id=row.values[1],
        symbol=row.values[2].upper(),
        filled_at=row.values[3],
        price=price,
        segment_id=row.values[5],
        slot=row.values[6].lower(),
        slot_id=row.values[7],
        source=row.values[8],
    )


def _identity_key(fill: FillLeg) -> tuple[str, str, str, str] | None:
    try:
        expected = fanout_slot_id(
            strategy_code="schwab_1m_v2",
            symbol=fill.symbol,
            segment_id=fill.segment_id,
            slot=fill.slot,
        )
    except ValueError:
        return None
    if fill.slot_id != expected:
        return None
    return fill.symbol, fill.segment_id, fill.slot, fill.slot_id


def _route_identity(row: RawRow, payload: dict) -> tuple[str, str, str, str] | None:
    return _identity_key(FillLeg(
        account=row.values[0], order_id=row.values[1], symbol=row.values[2],
        filled_at="", price=1.0, segment_id=str(payload.get("fanout_segment_id", "")),
        slot=str(payload.get("fanout_slot", "")),
        slot_id=str(payload.get("fanout_slot_id", "")), source="",
    ))


def _orb_order_marker(payload: dict) -> bool:
    """Positive producer markers only; absent ATR metadata proves nothing."""
    if payload.get("orb_entry") == "true" or payload.get("orb_country_route") == "true":
        return True
    client_ids = [payload.get("client_order_id")]
    permit = payload.get("bot_control_permit")
    if isinstance(permit, str):
        try:
            permit = json.loads(permit)
        except ValueError:
            permit = None
    if isinstance(permit, dict):
        client_ids.append(permit.get("client_order_id"))
    return any(isinstance(value, str) and value.startswith("orb_schwab-")
               for value in client_ids)


def route_scopes(rows: Sequence[RawRow], fills: Sequence[FillLeg]) -> dict[tuple[str, str], str]:
    """N/A requires a complete causal single-route seal, never an absent peer fill."""
    accounts = {"live:orb": "webull", "live:schwab_1m_v2": "schwab"}
    evidence: dict[tuple[str, str], list[RawRow]] = {}
    parsed: dict[tuple[str, str], tuple[RawRow, dict, tuple]] = {}
    scopes: dict[tuple[str, str], str] = {}
    for row in rows:
        if row.kind == "ROUTE_ORDER":
            evidence.setdefault((row.values[0], row.values[1]), []).append(row)
    for identity, candidates in evidence.items():
        scopes[identity] = "UNKNOWN"
        if len(candidates) != 1:
            continue
        row = candidates[0]
        if (identity[0] not in accounts or not identity[1] or not row.values[2]
                or row.values[2] != row.values[2].strip().upper()):
            continue
        try:
            payload = json.loads(row.values[3])
            if not isinstance(payload, dict) or row.values[4] != accounts.get(identity[0]):
                continue
            clocks = [parse_instant(raw, "route order clock") for raw in row.values[5:7] if raw]
            if not clocks or any(clock <= datetime(1970, 1, 1, tzinfo=UTC) for clock in clocks):
                continue
            # C12124: native first fills may carry whole seconds while DB submit
            # carries microseconds. Only this comparison floors the DB clock.
            if row.values[5] and row.values[6] and clocks[0].replace(microsecond=0) > clocks[1]:
                continue
            matching_fills = [fill for fill in fills
                              if (fill.account, fill.order_id) == identity]
            if _orb_order_marker(payload):
                atr_markers = (payload.get("source") == "schwab_1m_v2"
                               or payload.get("path") == "ATR Flip"
                               or any(name.startswith(("atr_", "fanout_")) for name in payload))
                if not atr_markers and not matching_fills:
                    scopes[identity] = "EXCLUDED_NON_ATR"
                continue  # Conflicting ATR evidence stays UNKNOWN, never excluded.
            key = _route_identity(row, payload)
            if key is None:
                continue
            if any(_identity_key(fill) != key or fill.symbol != row.values[2]
                   or not row.values[6]
                   or parse_instant(fill.filled_at, "filled leg clock") !=
                   parse_instant(row.values[6], "route fill clock")
                   for fill in matching_fills):
                continue
            parsed[identity] = row, payload, key
            if any(name in payload for name in ATR_COUNTRY_ROUTE_KEYS):
                if any(not isinstance(payload.get(name), str) for name in ATR_COUNTRY_ROUTE_KEYS):
                    continue
                broker = accounts[identity[0]]
                sealed = parse_instant(payload["atr_country_sealed_at"], "country seal")
                as_of = payload["atr_country_as_of"]
                status = payload["atr_country_status"]
                if (payload["atr_country_schema"] != "1"
                        or payload["atr_country_symbol"] != row.values[2]
                        or payload["atr_country_segment_id"] != key[1]
                        or payload["atr_country_account"] != identity[0]
                        or payload["atr_country_broker"] != broker
                        or payload["atr_country_notional_usd"] != ("300" if broker == "webull" else "600")
                        or not payload["atr_country_reason"] or not status
                        or any(sealed > clock for clock in clocks)):
                    continue
                if as_of and parse_instant(as_of, "country source") > sealed and status != "FUTURE":
                    continue
                if broker == "schwab" and (
                        payload["atr_country_reason"] != "sec_us" or status != "READY" or not as_of):
                    continue
                scopes[identity] = "NOT_APPLICABLE"
        except (ValueError, TypeError, KeyError):
            continue

    # Retained filled legs themselves prove placement, but cannot prove a missing
    # opposite leg was expected. New query rows also include unfilled submissions.
    keys_by_account: dict[tuple, set[str]] = {}
    for fill in fills:
        identity = fill.account, fill.order_id
        key = _identity_key(fill)
        if key is not None and identity not in evidence:
            keys_by_account.setdefault(key, set()).add(fill.account)
    for identity, (_, payload, key) in parsed.items():
        if not any(name in payload for name in ATR_COUNTRY_ROUTE_KEYS):
            keys_by_account.setdefault(key, set()).add(identity[0])
    for fill in fills:
        identity = fill.account, fill.order_id
        if identity not in evidence:
            scopes[identity] = (
                "EXPECTED" if keys_by_account.get(_identity_key(fill)) == set(accounts) else "UNKNOWN"
            )
    for identity, (_, payload, key) in parsed.items():
        peers = [other for other, (_, _, peer_key) in parsed.items() if peer_key == key]
        country_peers = [other for other in peers if any(
            name in parsed[other][1] for name in ATR_COUNTRY_ROUTE_KEYS)]
        if country_peers:
            signatures = {json.dumps([parsed[other][1].get(name) for name in ATR_COUNTRY_ROUTE_KEYS],
                                     sort_keys=True) for other in country_peers}
            conflicting = (len(signatures) != 1 or len(country_peers) != len(peers)
                           or any(scopes[other] != "NOT_APPLICABLE" for other in peers)
                           or len({other[0] for other in peers}) != 1
                           or any(fill.account != identity[0] and _identity_key(fill) == key
                                  for fill in fills))
            if conflicting:
                for other in peers:
                    scopes[other] = "UNKNOWN"
        elif keys_by_account.get(key) == set(accounts):
            scopes[identity] = "EXPECTED"
    return scopes


def evaluate(rows: Sequence[RawRow], *, since: datetime, until: datetime) -> Report:
    lines = [
        "### D6 FAN-OUT OUTCOME ACCEPTANCE",
        f"window=[{since.isoformat()}, {until.isoformat()})",
        "scope=one session; external outcomes only; consumer markers are not acceptance",
        "baseline=paired usable 16/53 with 37 CTT; mirror 18/292 (6.2%) vs Schwab "
        "34/368 (9.2%); duplicate extras 22/22 worse median 4.58%; refused exits "
        "37/25/49/2-as-of-snapshot with episode denominators",
    ]
    if since >= until:
        lines.append("verdict=COULD_NOT_TELL reason=window start is not before end")
        return Report(COULD_NOT_TELL, "COULD_NOT_TELL", tuple(lines))
    try:
        validate_controls(rows)
    except EvidenceFailure as exc:
        lines.append(f"control=FAILED reason={exc}")
        lines.append("verdict=COULD_NOT_TELL reason=known-bad control did not reproduce")
        return Report(COULD_NOT_TELL, "COULD_NOT_TELL", tuple(lines))
    lines.append("control=PASS known-bad populations reproduced from durable rows")

    fills = [_parse_fill(row) for row in rows if row.kind == "FANOUT_FILL"]
    scopes = route_scopes(rows, fills)
    webull = [fill for fill in fills if fill.account == "live:orb"]
    expected_webull = [fill for fill in webull
                       if scopes.get((fill.account, fill.order_id)) == "EXPECTED"]
    expected_schwab = [fill for fill in fills if fill.account == "live:schwab_1m_v2"
                      and scopes.get((fill.account, fill.order_id)) == "EXPECTED"]
    single = [fill for fill in fills
              if scopes.get((fill.account, fill.order_id)) == "NOT_APPLICABLE"]
    route_unknown = sum(scope == "UNKNOWN" for scope in scopes.values())
    non_atr = sum(scope == "EXCLUDED_NON_ATR" for scope in scopes.values())
    lines.append(
        f"route_scope single_venue_fills={len(single)} expected_dual_webull_fills={len(expected_webull)} "
        f"unknown_order_routes={route_unknown} excluded_non_atr_orders={non_atr}; "
        "NOT_APPLICABLE is not a filled pair"
    )
    schwab_keys = {
        key for fill in fills if fill.account == "live:schwab_1m_v2"
        if (key := _identity_key(fill)) is not None
    }
    webull_valid = [(fill, key) for fill in webull if (key := _identity_key(fill)) is not None]
    pair_ctt = len(webull) - len(webull_valid)
    paired = sum(1 for fill, key in webull_valid
                 if fill in expected_webull and key in schwab_keys)
    webull_only = len(expected_webull) - paired
    webull_keys = {key for _, key in webull_valid}
    schwab_only = sum(_identity_key(fill) not in webull_keys for fill in expected_schwab)
    usable_rate = (100.0 * len(webull_valid) / len(webull)) if webull else 0.0
    if route_unknown:
        pair_metric = Metric(
            "paired_legs", "COULD_NOT_TELL",
            f"paired_legs={paired} usable={len(webull_valid)} of {len(webull)} "
            f"route_unknown={route_unknown} single_venue={len(single)} "
            "outcome=actual routing expectation unproven or conflicting",
        )
    elif single and not expected_webull and not expected_schwab:
        pair_metric = Metric(
            "paired_legs", "NOT_APPLICABLE",
            f"paired_legs=0 usable={len(webull_valid)} of {len(webull)} "
            f"single_venue={len(single)} expected_dual=0 "
            "proof=sealed account/symbol/segment/venue/time-bound country routing",
        )
    elif webull_only or schwab_only:
        pair_metric = Metric(
            "paired_legs", "FAIL",
            f"paired_legs={paired} usable={len(webull_valid)} of {len(webull)} "
            f"webull_only={webull_only} schwab_only={schwab_only} could_not_tell=0 "
            f"expected_dual_schwab_fills={len(expected_schwab)} "
            "outcome=expected cross-venue filled counterpart missing",
        )
    elif not webull:
        pair_metric = Metric(
            "paired_legs",
            "UNEXERCISED",
            "paired_legs=0 usable=0 of 0 could_not_tell=0 denominator=filled Webull fan-out legs",
        )
    elif pair_ctt:
        pair_metric = Metric(
            "paired_legs",
            "COULD_NOT_TELL",
            f"paired_legs={paired} usable={len(webull_valid)} of {len(webull)} "
            f"webull_only={webull_only} could_not_tell={pair_ctt} coverage={usable_rate:.1f}% "
            "baseline=16/53 usable with 37 CTT",
        )
    elif usable_rate <= (100.0 * BASE_PAIR_USABLE / BASE_PAIR_TOTAL):
        pair_metric = Metric(
            "paired_legs",
            "FAIL",
            f"paired_legs={paired} usable={len(webull_valid)} of {len(webull)} "
            f"webull_only={webull_only} could_not_tell=0 coverage={usable_rate:.1f}% "
            "outcome=not above 16/53 known-bad coverage",
        )
    else:
        pair_metric = Metric(
            "paired_legs",
            "PASS",
            f"paired_legs={paired} usable={len(webull_valid)} of {len(webull)} "
            f"webull_only={webull_only} could_not_tell=0 coverage={usable_rate:.1f}%",
        )

    all_order_rows = [row for row in rows if row.kind == "MATCHED_ORDER"]
    order_rows = [row for row in all_order_rows
                  if scopes.get((row.values[0], row.values[1]))
                  not in {"NOT_APPLICABLE", "EXCLUDED_NON_ATR"}]
    mirror_orders = [
        row for row in order_rows
        if row.values[0] == "live:orb" and row.values[5] == "rth_resting_mirror"
    ]
    schwab_orders = [row for row in order_rows if row.values[0] == "live:schwab_1m_v2"]
    mirror_fills = sum(row.values[4].lower() == "true" for row in mirror_orders)
    schwab_fills = sum(row.values[4].lower() == "true" for row in schwab_orders)
    matched_symbols = len({row.values[2] for row in mirror_orders})
    proven_single_cohort = bool(single) and all(
        scope in {"NOT_APPLICABLE", "EXCLUDED_NON_ATR"} for scope in scopes.values())
    if proven_single_cohort and not order_rows:
        fill_metric = Metric(
            "fill_rate", "NOT_APPLICABLE",
            "fill_rate mirror=0/0 schwab=0/0 matched_symbols=0 "
            f"single_venue={len(single)} expected_dual=0 proof=sealed routing; no paired bake-off",
        )
    elif not mirror_orders or not schwab_orders:
        fill_metric = Metric(
            "fill_rate",
            "UNEXERCISED",
            f"fill_rate mirror={mirror_fills}/{len(mirror_orders)} "
            f"schwab={schwab_fills}/{len(schwab_orders)} matched_symbols={matched_symbols} "
            "denominator=matched STOP_LIMIT BUY orders",
        )
    else:
        mirror_rate = 100.0 * mirror_fills / len(mirror_orders)
        schwab_rate = 100.0 * schwab_fills / len(schwab_orders)
        gap = schwab_rate - mirror_rate
        baseline_mirror_rate = 100.0 * BASE_MIRROR_FILLS / BASE_MIRROR_ORDERS
        baseline_gap = (
            100.0 * BASE_SCHWAB_FILLS / BASE_SCHWAB_ORDERS - baseline_mirror_rate
        )
        # A higher mirror rate with an even wider broker gap is movement, but not acceptance. The
        # paired bake-off needs both: the mirror converts more often than the known-bad baseline and
        # its absolute gap to the matched Schwab population narrows.
        improved = (
            mirror_rate > baseline_mirror_rate and abs(gap) < abs(baseline_gap)
        )
        fill_metric = Metric(
            "fill_rate",
            "PASS" if improved else "FAIL",
            f"fill_rate mirror={mirror_fills}/{len(mirror_orders)}={mirror_rate:.1f}% "
            f"schwab={schwab_fills}/{len(schwab_orders)}={schwab_rate:.1f}% "
            f"gap_pp={gap:.1f} matched_symbols={matched_symbols} "
            f"baseline=18/292=6.2% vs 34/368=9.2% gap_pp={baseline_gap:.1f}",
        )

    duplicate_ctt = pair_ctt
    grouped: dict[tuple[str, str, str, str], list[FillLeg]] = {}
    for fill, key in webull_valid:
        grouped.setdefault(key, []).append(fill)
    extras: list[tuple[FillLeg, float]] = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: (item.filled_at, item.order_id))
        first_price = ordered[0].price
        extras.extend((item, 100.0 * (item.price / first_price - 1.0)) for item in ordered[1:])
    worse = sum(pct > 0 for _, pct in extras)
    median_pct = statistics.median(pct for _, pct in extras) if extras else 0.0
    if not webull:
        duplicate_metric = Metric(
            "duplicate_legs",
            "UNEXERCISED",
            "duplicate_legs=0 of 0 filled Webull fan-out legs; denominator=0",
        )
    elif duplicate_ctt:
        duplicate_metric = Metric(
            "duplicate_legs",
            "COULD_NOT_TELL",
            f"duplicate_legs={len(extras)} attributed={len(webull_valid)} of {len(webull)} "
            f"could_not_tell={duplicate_ctt} worse={worse} median_worse_pct={median_pct:.2f} "
            "scope=venue-local (account,symbol,segment,slot)",
        )
    elif extras:
        duplicate_metric = Metric(
            "duplicate_legs",
            "FAIL",
            f"duplicate_legs={len(extras)} of {len(webull)} filled Webull fan-out legs "
            f"worse={worse} median_worse_pct={median_pct:.2f} baseline=22/22 worse median=4.58% "
            "scope=venue-local (account,symbol,segment,slot)",
        )
    else:
        duplicate_metric = Metric(
            "duplicate_legs",
            "PASS",
            f"duplicate_legs=0 of {len(webull)} filled Webull fan-out legs "
            "could_not_tell=0 scope=venue-local (account,symbol,segment,slot)",
        )

    refusals = [row for row in rows if row.kind == "REFUSAL"]
    exit_episodes = {row.values[0] for row in rows if row.kind == "EXIT_EPISODE"}
    classified = sum(bool(row.values[2]) for row in refusals)
    no_prior_fill = len(refusals) - classified
    if not exit_episodes:
        refusal_metric = Metric(
            "refused_exits",
            "UNEXERCISED",
            f"refused_exits={len(refusals)} post_exit_episodes=0 no_preceding_sell_fill={no_prior_fill} "
            "denominator=confirmed Webull SELL fill orders",
        )
    elif refusals:
        refusal_metric = Metric(
            "refused_exits",
            "FAIL",
            f"refused_exits={len(refusals)} post_exit_episodes={len(exit_episodes)} "
            f"classified_post_exit={classified} no_preceding_sell_fill={no_prior_fill} "
            "baseline_per_day=2-as-of-snapshot/49/25/37",
        )
    else:
        refusal_metric = Metric(
            "refused_exits",
            "PASS",
            f"refused_exits=0 post_exit_episodes={len(exit_episodes)} "
            "denominator=confirmed Webull SELL fill orders; zero is exercised",
        )

    metrics = (pair_metric, fill_metric, duplicate_metric, refusal_metric)
    lines.extend(f"metric={metric.name} verdict={metric.verdict} {metric.line}" for metric in metrics)
    verdicts = {metric.verdict for metric in metrics}
    if "COULD_NOT_TELL" in verdicts:
        verdict, code = "COULD_NOT_TELL", COULD_NOT_TELL
    elif "UNEXERCISED" in verdicts:
        verdict, code = "UNEXERCISED", UNEXERCISED
    elif "FAIL" in verdicts:
        verdict, code = "FAIL", FAIL
    else:
        verdict, code = "PASS", PASS
    lines.append(f"verdict={verdict}")
    return Report(code, verdict, tuple(lines))


def query_database(since: datetime, until: datetime) -> tuple[RawRow, ...]:
    command = [
        "sudo", "-n", "-u", "postgres", "psql", "-X", "-qAt",
        "-v", "ON_ERROR_STOP=1",
        "-v", f"window_since={since.isoformat()}",
        "-v", f"window_until={until.isoformat()}",
        "-d", "project_mai_tai", "-f", "-",
    ]
    try:
        result = subprocess.run(
            command,
            input=SQL,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceFailure(f"could not execute the read-only database query: {exc}") from exc
    if result.returncode != 0:
        raise EvidenceFailure(result.stderr.strip() or "psql exited non-zero without an error")
    if result.stderr.strip():
        raise EvidenceFailure(f"psql wrote unexpected stderr: {result.stderr.strip()}")
    return parse_rows(result.stdout)


def run_report(*, since: datetime, until: datetime) -> Report:
    if since >= until:
        return evaluate((), since=since, until=until)
    try:
        rows = query_database(since, until)
    except EvidenceFailure as exc:
        return Report(
            COULD_NOT_TELL,
            "COULD_NOT_TELL",
            (
                "### D6 FAN-OUT OUTCOME ACCEPTANCE",
                f"window=[{since.isoformat()}, {until.isoformat()})",
                f"verdict=COULD_NOT_TELL reason=read-only evidence failed: {exc}",
            ),
        )
    return evaluate(rows, since=since, until=until)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="inclusive ISO instant with Z/offset")
    parser.add_argument("--until", required=True, help="exclusive ISO instant with Z/offset")
    args = parser.parse_args(argv)
    try:
        since = parse_instant(args.since, "--since")
        until = parse_instant(args.until, "--until")
    except ValueError as exc:
        print(f"COULD_NOT_TELL: {exc}")
        return COULD_NOT_TELL
    report = run_report(since=since, until=until)
    print("\n".join(report.lines))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
