-- Main executor ONLY; no DDL, helper calls, provider IO, or worker execution.
-- Main caller must stop on SQL errors. A parent mismatch must STOP installation.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '120s';

SELECT version_num FROM public.alembic_version ORDER BY version_num;
DO $guard$
DECLARE versions text[];
BEGIN
    SELECT array_agg(version_num::text ORDER BY version_num)
      INTO versions FROM public.alembic_version;
    IF versions IS DISTINCT FROM ARRAY['20261023_atr_aggregate']::text[] THEN
        RAISE EXCEPTION 'L12_PARENT_MISMATCH_STOP actual=% expected=20261023_atr_aggregate', versions;
    END IF;
END
$guard$;

SELECT 'dashboard_snapshots' AS table_name, count(*) AS exact_rows,
       pg_table_size('public.dashboard_snapshots'::regclass) AS table_bytes,
       pg_indexes_size('public.dashboard_snapshots'::regclass) AS current_index_bytes,
       pg_total_relation_size('public.dashboard_snapshots'::regclass) AS total_bytes
FROM public.dashboard_snapshots
UNION ALL
SELECT 'broker_orders', count(*),
       pg_table_size('public.broker_orders'::regclass),
       pg_indexes_size('public.broker_orders'::regclass),
       pg_total_relation_size('public.broker_orders'::regclass)
FROM public.broker_orders
UNION ALL
SELECT 'bot_control_attempts', count(*),
       pg_table_size('public.bot_control_attempts'::regclass),
       pg_indexes_size('public.bot_control_attempts'::regclass),
       pg_total_relation_size('public.bot_control_attempts'::regclass)
FROM public.bot_control_attempts
ORDER BY table_name;

ROLLBACK;
