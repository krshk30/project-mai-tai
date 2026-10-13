BEGIN READ ONLY; SET LOCAL statement_timeout='2500ms';
WITH cases AS (
 SELECT id,opened_at,payload,payload->>'finding_type' AS kind,
 split_part(payload->>'fingerprint',':',2)::uuid AS entity_id
 FROM system_incidents WHERE service_name='reconciler'
 AND payload->>'finding_type' IN ('stuck_order','stuck_intent')
 AND opened_at >= TIMESTAMPTZ '2026-10-05 00:00:00 America/New_York'
 AND opened_at < TIMESTAMPTZ '2026-10-13 00:00:00 America/New_York'
), rows AS (
 SELECT c.id AS case_id,c.kind,c.entity_id,c.opened_at AS replay_at,
 c.payload->>'status' AS alert_status,c.payload->>'updated_at' AS alert_updated_at,
 c.payload->>'symbol' AS symbol,c.payload->>'account_name' AS account_name,
 c.payload->>'strategy_code' AS strategy_code,a.provider,
 o.id AS order_id,o.intent_id AS order_intent_id,o.order_type,o.side,o.quantity,
 o.client_order_id,e.payload->>'broker_order_id' AS broker_order_id,
 e.event_type AS order_status_asof,e.event_source,e.event_at,
 json_build_object('resting_entry',e.payload->'metadata'->>'resting_entry',
                   'fanout_source',e.payload->'metadata'->>'fanout_source') AS metadata
 FROM cases c LEFT JOIN LATERAL (
 SELECT b.* FROM broker_orders b WHERE
 (c.kind='stuck_order' AND b.id=c.entity_id)
 OR (c.kind='stuck_intent' AND b.intent_id=c.entity_id AND b.submitted_at <= c.opened_at)
 ORDER BY b.submitted_at DESC NULLS LAST,b.id DESC LIMIT 1
 ) o ON true LEFT JOIN broker_accounts a ON a.id=o.broker_account_id
 LEFT JOIN LATERAL (SELECT event_type,event_source,event_at,payload FROM broker_order_events
 WHERE order_id=o.id AND event_at<=c.opened_at ORDER BY event_at DESC,id DESC LIMIT 1) e ON true
)
SELECT json_build_object('captured_at',clock_timestamp(),'base','860fba4c7d59edff18eb23468ba576dcb83f4d57',
 'scope','242 recorded incident snapshots Oct05-Oct12 ET; retained event_at<=first_opened reconstruction, software arrival times UNMEASURED',
 'rows',json_agg(rows ORDER BY replay_at,case_id)) FROM rows; ROLLBACK;
