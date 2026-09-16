-- G5 population: every v2 symbol-day with a stored live Schwab 1-minute bar.
-- ET dates are derived with the database time-zone conversion, never from UTC dates.
SELECT
  (bar_time AT TIME ZONE 'America/New_York')::date AS session_day_et,
  symbol,
  COUNT(*) AS schwab_bars,
  MIN(bar_time) AS first_schwab_bar,
  MAX(bar_time) AS last_schwab_bar
FROM strategy_bar_history
WHERE strategy_code = 'schwab_1m_v2'
  AND interval_secs = 60
  AND source = 'live'
  AND bar_time >= TIMESTAMPTZ '2026-08-05 04:00:00-04'
  AND bar_time <  TIMESTAMPTZ '2026-09-17 04:00:00-04'
GROUP BY 1, 2
ORDER BY 1, 2;
