"""Literal, bounded TODAY source reads; no execution or provider imports here.

The runner owns one READ ONLY snapshot and binds every value. Rows establish
availability, not physical-print, execution-eligibility or coverage permission.
"""

BEGIN = (
    "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
    "SET LOCAL statement_timeout = '20000ms'",
    "SET LOCAL lock_timeout = '250ms'",
    "SET LOCAL idle_in_transaction_session_timeout = '30000ms'",
)

IDENTITY = """SELECT statement_timestamp() AS source_asof,
 pg_current_snapshot()::text AS snapshot_id,
 current_setting('transaction_read_only') AS read_only,
 current_setting('transaction_isolation') AS isolation"""

ROSTER = """SELECT symbol FROM (
 SELECT DISTINCT symbol FROM strategy_bar_history
 WHERE strategy_code = 'schwab_1m_v2' AND interval_secs = 60
 AND bar_time >= %(start)s AND bar_time < %(end)s
 UNION SELECT DISTINCT symbol FROM scanner_confirmed_events
 WHERE trade_date = %(day)s AND event_at >= %(start)s AND event_at < %(end)s
 UNION SELECT unnest(%(required_symbols)s::text[])
) AS candidates ORDER BY symbol LIMIT 257"""

SOURCES = {
    "capture_bars": """SELECT id, provider, symbol, event_ts, interval_secs,
      received_at, open, high, low, close, volume, vwap, transactions
      FROM market_capture_bars WHERE provider = 'massive' AND interval_secs = 60
      AND symbol = ANY(%(symbols)s::text[]) AND event_ts >= %(start)s
      AND event_ts + interval '1 minute' <= %(end)s AND received_at <= %(asof)s
      ORDER BY symbol, event_ts, id LIMIT 200001""",
    "bars": """SELECT id::text, symbol, bar_time, open_price, high_price,
      low_price, close_price, volume, source, created_at, indicators,
      decision_status, decision_reason, decision_path
      FROM strategy_bar_history WHERE strategy_code = 'schwab_1m_v2'
      AND interval_secs = 60 AND symbol = ANY(%(symbols)s::text[])
      AND bar_time >= %(start)s AND bar_time + interval '1 minute' <= %(end)s
      AND created_at <= %(asof)s ORDER BY symbol, bar_time, id LIMIT 100001""",
    "scanner": """SELECT id, symbol, event_type, event_at, force_watchlist,
      reconfirm_seq FROM scanner_confirmed_events WHERE trade_date = %(day)s
      AND symbol = ANY(%(symbols)s::text[]) AND event_at >= %(start)s
      AND event_at < %(end)s ORDER BY symbol, event_at, id LIMIT 20001""",
    "fills": """SELECT f.id::text AS fill_id, f.order_id::text,
      f.broker_fill_id, f.symbol, f.side, f.quantity, f.price, f.filled_at,
      f.broker_account_id::text AS fill_account_id,
      f.strategy_id::text AS fill_strategy_id,
      bo.broker_account_id::text AS order_account_id,
      bo.strategy_id::text AS order_strategy_id, bo.intent_id::text,
      bo.client_order_id, bo.broker_order_id, bo.payload AS order_payload,
      bo.quantity AS order_quantity, bo.status AS order_status,
      bo.order_type AS recorded_order_type, bo.submitted_at,
      ti.quantity AS intent_quantity,
      ti.reason, ti.payload AS intent_payload,
      ti.broker_account_id::text AS intent_account_id,
      ti.strategy_id::text AS intent_strategy_id,
      ba.name AS account_name, ba.provider AS broker, s.code AS strategy_code
      , ba.id::text AS physical_account_id, s.id::text AS physical_strategy_id
      FROM fills f JOIN broker_orders bo ON bo.id = f.order_id
      LEFT JOIN trade_intents ti ON ti.id = bo.intent_id
      JOIN broker_accounts ba ON ba.id = f.broker_account_id
      JOIN strategies s ON s.id = f.strategy_id
      WHERE s.code = 'schwab_1m_v2' AND ba.name IN ('live:orb','live:schwab_1m_v2')
      AND f.symbol = ANY(%(symbols)s::text[]) AND f.filled_at >= %(start)s
      AND f.filled_at < %(end)s ORDER BY f.filled_at, f.id LIMIT 20001""",
}

LIMITS = {"capture_bars": 200000, "bars": 100000, "scanner": 20000, "fills": 20000}

# C1 13:59: exact existing Massive capture schema. NULL conditions remain
# distinct from an actually stored empty string; do not COALESCE either.
# Whole symbol scope is paged, never an arbitrary first N print subset.
CAPTURE_PAGES = {
    "trades": """SELECT id, provider, symbol, event_ts, received_at,
      price, size, exchange, conditions, cumulative_volume
      FROM market_capture_trades WHERE provider = 'massive'
      AND symbol = %(symbol)s AND event_ts >= %(start)s AND event_ts < %(end)s
      AND received_at <= %(asof)s
      AND (%(after_ts)s::timestamptz IS NULL
           OR (event_ts, id) > (%(after_ts)s::timestamptz, %(after_id)s::bigint))
      ORDER BY event_ts, id LIMIT 5001""",
    "quotes": """SELECT id, provider, symbol, event_ts, received_at,
      bid_price, ask_price, bid_size, ask_size
      FROM market_capture_quotes WHERE provider = 'massive'
      AND symbol = %(symbol)s AND event_ts >= %(start)s AND event_ts < %(end)s
      AND received_at <= %(asof)s
      AND (%(after_ts)s::timestamptz IS NULL
           OR (event_ts, id) > (%(after_ts)s::timestamptz, %(after_id)s::bigint))
      ORDER BY event_ts, id LIMIT 5001""",
}
PAGE_SIZE = 5000
MAX_SYMBOL_CAPTURE_ROWS = 1000000
MAX_CAPTURE_PAGES = 4096
