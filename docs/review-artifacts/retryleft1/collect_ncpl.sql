BEGIN READ ONLY;
SET LOCAL statement_timeout = '10s';
SELECT json_build_object(
 'as_of', now(),
 'orders', (SELECT json_agg(json_build_object('id',o.id,'intent_id',o.intent_id,'account',a.name,
 'client_order_id',o.client_order_id,'broker_order_id',o.broker_order_id,'side',o.side,
 'quantity',o.quantity,'status',o.status,'submitted_at',o.submitted_at,'updated_at',o.updated_at,
 'segment',o.payload->>'fanout_segment_id','slot_id',o.payload->>'fanout_slot_id',
 'stop',o.payload->>'stop_price','limit',o.payload->>'limit_price'))
 FROM broker_orders o JOIN broker_accounts a ON a.id=o.broker_account_id
 JOIN strategies s ON s.id=o.strategy_id WHERE s.code='schwab_1m_v2'
 AND o.symbol='NCPL' AND o.submitted_at >= '2026-10-07T19:00Z' AND o.submitted_at < '2026-10-07T19:34Z'),
 'fills', (SELECT json_agg(json_build_object('id',f.id,'order_id',f.order_id,'account',a.name,
 'side',f.side,'quantity',f.quantity,'price',f.price,'filled_at',f.filled_at))
 FROM fills f JOIN broker_accounts a ON a.id=f.broker_account_id JOIN strategies s ON s.id=f.strategy_id
 WHERE s.code='schwab_1m_v2' AND f.symbol='NCPL' AND f.filled_at >= '2026-10-07T19:00Z'
 AND f.filled_at < '2026-10-07T19:34Z'),
 'owners', (SELECT json_agg(json_build_object('at',created_at,'payload',payload))
 FROM dashboard_snapshots WHERE snapshot_type='v2_flip_entry_ownership'
 AND payload->>'symbol'='NCPL' AND created_at >= '2026-10-07T19:00Z'
 AND created_at < '2026-10-07T19:34Z'));
ROLLBACK;
