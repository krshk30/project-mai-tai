-- MOMORDER1 / O-1019-9: REVIEW DRAFT ONLY. Never executed by Codex.
-- Claude must review exact source/preimages and externally verify a continuous
-- stopped-OMS/exclusive maintenance window and a usable backup. Text attestations
-- below are audit inputs, NOT an independent process/backup verification.
-- Inputs: :'orders_before' JSON array of THREE full broker_orders rows,
-- :'parents_before' array of THREE full parent trade_intents rows,
-- :'fills_before' array of ALL fills for THREE episodes (not just target fills),
-- :'backup_ref', :'backup_sha256', :'maintenance_receipt'.
-- Capture these fresh inside the approved stopped window, not from old receipts.
-- REQUIRED :'gmex_spec' JSON object, reviewed against retained raw evidence:
-- {"order_id":"b1f9b7e2-3d0c-4100-8750-01eb91646a45",
--  "parent_id":"f4556f70-f833-5467-97ee-afe5b5b582a1","symbol":"GMEX",
--  "coid":"ml1-GMEX-p-f4556f70f833546797ee-1","account":"live:orb",
--  "provider":"webull","strategy":"momentum_warn_live","side":"sell",
--  "quantity":59,"price":1.76,"broker_id":REQUIRED_STRING,
--  "fill_time":REQUIRED_EXACT_UTC_MILLISECOND_STRING,
--  "raw_ref":REQUIRED_RETAINED_RECEIPT_REFERENCE,"raw_sha256":REQUIRED_SHA256}
-- This example is intentionally NOT valid JSON / not an execution default.
-- Full GMEX broker ID, exact fill milliseconds and raw receipt are NOT supplied.
-- The reported fill is approximately 2026-10-20 12:32:49Z; no exact time inferred.
-- A GMEX 417 cancel is UNKNOWN without its cancel trace. Existing close_root
-- explains the label; this repair neither asserts nor requires a 417 outcome.
-- Only three order status/updated_at fields and one durable audit row may change.
\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL SERIALIZABLE;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '10s';
SELECT pg_advisory_xact_lock(hashtext('momentum_warn_live:capacity'));
SELECT pg_advisory_xact_lock(hashtext('orb-v2:live:orb:YMAT'));
SELECT pg_advisory_xact_lock(hashtext('orb-v2:live:orb:AIXI'));
SELECT pg_advisory_xact_lock(hashtext('orb-v2:live:orb:GMEX'));
CREATE TEMP TABLE momorder1_inputs (k text PRIMARY KEY, v jsonb) ON COMMIT DROP;
INSERT INTO momorder1_inputs VALUES
 ('orders', :'orders_before'::jsonb), ('parents', :'parents_before'::jsonb),
 ('fills', :'fills_before'::jsonb), ('gmex_spec', :'gmex_spec'::jsonb),
 ('meta', jsonb_build_object('backup_ref', :'backup_ref',
   'backup_sha256', :'backup_sha256', 'maintenance_receipt', :'maintenance_receipt'));
CREATE TEMP TABLE momorder1_audit (phase text PRIMARY KEY, body jsonb) ON COMMIT DROP;
DO $repair$
DECLARE
  sid uuid; aid uuid; spec record; o broker_orders%ROWTYPE; p trade_intents%ROWTYPE;
  actual_orders jsonb; actual_parents jsonb; actual_fills jsonb; after_orders jsonb;
  m jsonb; g jsonb; gmex_time timestamptz; n integer;
  audit_id constant uuid := '79e54e32-6cf2-4d73-95d8-c469b2765f14';
  prior_two_row_audit_id constant uuid := '316d9531-24be-4b48-a1fb-1ac68ba9dc5c';
BEGIN
  IF current_database() IS DISTINCT FROM 'project_mai_tai' THEN
    RAISE EXCEPTION 'wrong database for MOMORDER1 repair';
  END IF;
  SELECT v INTO STRICT m FROM momorder1_inputs WHERE k='meta';
  IF coalesce(m->>'backup_ref','') !~ '[^[:space:]]'
     OR coalesce(m->>'backup_sha256','') !~ '^[0-9a-f]{64}$'
     OR coalesce(m->>'maintenance_receipt','') NOT LIKE '%oms=inactive%'
     OR coalesce(m->>'maintenance_receipt','') NOT LIKE '%exclusive=true%' THEN
    RAISE EXCEPTION 'reviewed backup and continuous stopped-writer attestation required';
  END IF;
  SELECT id INTO STRICT sid FROM strategies WHERE code='momentum_warn_live';
  SELECT id INTO STRICT aid FROM broker_accounts WHERE name='live:orb' AND provider='webull';
  IF EXISTS (SELECT FROM dashboard_snapshots
      WHERE id IN (audit_id,prior_two_row_audit_id)
        OR payload::jsonb->>'repair_id' IN
          ('O-1019-9:YMAT-AIXI-single-root-labels',
           'O-1019-9:YMAT-AIXI-GMEX-single-root-labels')) THEN
    RAISE EXCEPTION 'three-row or prior two-row repair already audited; never duplicate';
  END IF;
  SELECT v INTO STRICT g FROM momorder1_inputs WHERE k='gmex_spec';
  IF jsonb_typeof(g) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION 'required reviewed GMEX evidence object missing';
  END IF;
  IF g - ARRAY['broker_id','fill_time','raw_ref','raw_sha256']::text[] IS DISTINCT FROM
       jsonb_build_object('order_id','b1f9b7e2-3d0c-4100-8750-01eb91646a45',
         'parent_id','f4556f70-f833-5467-97ee-afe5b5b582a1','symbol','GMEX',
         'coid','ml1-GMEX-p-f4556f70f833546797ee-1','account','live:orb',
         'provider','webull','strategy','momentum_warn_live','side','sell',
         'quantity',59,'price',1.76)
     OR jsonb_typeof(g->'broker_id') IS DISTINCT FROM 'string'
     OR coalesce(g->>'broker_id','') !~ '^[A-Za-z0-9]+$'
     OR jsonb_typeof(g->'fill_time') IS DISTINCT FROM 'string'
     OR coalesce(g->>'fill_time','') !~ '^2026-10-20T12:32:[0-5][0-9]\.[0-9]{3}Z$'
     OR jsonb_typeof(g->'raw_ref') IS DISTINCT FROM 'string'
     OR coalesce(g->>'raw_ref','') !~ '[^[:space:]]'
     OR jsonb_typeof(g->'raw_sha256') IS DISTINCT FROM 'string'
     OR coalesce(g->>'raw_sha256','') !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'exact GMEX identity, typed full broker ID/time and retained raw reference/SHA required';
  END IF;
  gmex_time := (g->>'fill_time')::timestamptz;
  IF gmex_time < '2026-10-20T12:32:41Z'::timestamptz THEN
    RAISE EXCEPTION 'GMEX fill time precedes reported entry; review receipt';
  END IF;
  IF jsonb_typeof((SELECT v FROM momorder1_inputs WHERE k='orders')) IS DISTINCT FROM 'array'
     OR jsonb_typeof((SELECT v FROM momorder1_inputs WHERE k='parents')) IS DISTINCT FROM 'array'
     OR jsonb_typeof((SELECT v FROM momorder1_inputs WHERE k='fills')) IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION 'full fresh preimage arrays required';
  END IF;
  IF jsonb_array_length((SELECT v FROM momorder1_inputs WHERE k='orders')) <> 3
     OR jsonb_array_length((SELECT v FROM momorder1_inputs WHERE k='parents')) <> 3 THEN
    RAISE EXCEPTION 'exactly three target and parent preimages required';
  END IF;
  FOR spec IN SELECT * FROM (VALUES
    ('a4aaa36a-2888-4a4b-a676-e34d2d2f7083'::uuid,
     '6c313663-c277-5b20-b650-c37ed7324feb'::uuid, 'YMAT',
     'ml1-YMAT-p-6c313663c2775b20b650-1', '13HSQTC4V99767R6M646DARB8A',
     50::numeric, 2.10::numeric, '2026-10-19T12:30:15.901Z'::timestamptz),
    ('76732752-59e2-42e2-840a-dab58a1c4989'::uuid,
     'f9e48766-f9ad-581d-b014-7068e23d5c83'::uuid, 'AIXI',
     'ml1-AIXI-p-f9e48766f9ad581db014-1', '4KGOBHF6MNVAD7QGJ3EAHTTRVA',
     31::numeric, 3.27::numeric, '2026-10-20T09:11:09.571Z'::timestamptz),
    ('b1f9b7e2-3d0c-4100-8750-01eb91646a45'::uuid,
     'f4556f70-f833-5467-97ee-afe5b5b582a1'::uuid, 'GMEX',
     'ml1-GMEX-p-f4556f70f833546797ee-1', g->>'broker_id',
     59::numeric, 1.76::numeric, gmex_time)
  ) AS s(oid, parent_id, symbol, coid, broker_id, qty, price, fill_time)
  LOOP
    SELECT * INTO STRICT p FROM trade_intents WHERE id=spec.parent_id FOR UPDATE;
    SELECT * INTO STRICT o FROM broker_orders WHERE id=spec.oid FOR UPDATE;
    PERFORM id FROM broker_orders WHERE strategy_id=sid
      AND payload::jsonb->>'episode_id'=spec.parent_id::text FOR UPDATE;
    PERFORM f.id FROM fills f JOIN broker_orders b ON b.id=f.order_id
      WHERE b.strategy_id=sid AND b.payload::jsonb->>'episode_id'=spec.parent_id::text
      FOR UPDATE OF f;
    IF o.strategy_id IS DISTINCT FROM sid OR o.broker_account_id IS DISTINCT FROM aid
       OR o.symbol IS DISTINCT FROM spec.symbol OR o.client_order_id IS DISTINCT FROM spec.coid
       OR o.broker_order_id IS DISTINCT FROM spec.broker_id OR o.side IS DISTINCT FROM 'sell'
       OR o.order_type IS DISTINCT FROM 'limit' OR o.quantity IS DISTINCT FROM spec.qty
       OR o.status IS DISTINCT FROM 'cancelled'
       OR o.payload::jsonb->>'episode_id' IS DISTINCT FROM spec.parent_id::text
       OR o.payload::jsonb->>'session' IS DISTINCT FROM 'AM'
       OR o.payload::jsonb->>'momlive_terminal_broker_event' IS DISTINCT FROM 'filled'
       OR p.strategy_id IS DISTINCT FROM sid OR p.broker_account_id IS DISTINCT FROM aid
       OR p.symbol IS DISTINCT FROM spec.symbol
       OR p.payload::jsonb->'momlive_episode'->>'state' IS DISTINCT FROM 'closed'
       OR (p.payload::jsonb->'momlive_episode'->>'filled')::numeric IS DISTINCT FROM spec.qty
       OR (p.payload::jsonb->'momlive_episode'->>'exited')::numeric IS DISTINCT FROM spec.qty THEN
      RAISE EXCEPTION 'exact closed single-root identity/terminal evidence changed: %', spec.symbol;
    END IF;
    IF (SELECT count(*) FROM broker_orders b WHERE b.strategy_id=sid
          AND b.payload::jsonb->>'episode_id'=spec.parent_id::text) IS DISTINCT FROM 2
       OR (SELECT count(*) FROM fills WHERE order_id=o.id) IS DISTINCT FROM 1
       OR NOT EXISTS (SELECT FROM fills f WHERE f.order_id=o.id AND f.strategy_id=sid
          AND f.broker_account_id=aid AND f.symbol=spec.symbol AND f.side='sell'
          AND f.quantity=spec.qty AND f.price=spec.price AND f.filled_at=spec.fill_time
          AND f.broker_fill_id=spec.broker_id || ':' || spec.qty::integer::text)
       OR EXISTS (SELECT FROM fills f JOIN broker_orders b ON b.id=f.order_id
           WHERE b.strategy_id=sid AND b.payload::jsonb->>'episode_id'=spec.parent_id::text
             AND (f.strategy_id IS DISTINCT FROM b.strategy_id
               OR f.broker_account_id IS DISTINCT FROM b.broker_account_id
               OR f.symbol IS DISTINCT FROM b.symbol OR f.side IS DISTINCT FROM b.side
               OR f.side NOT IN ('buy','sell') OR f.quantity IS NULL OR f.quantity <= 0
               OR f.quantity::text IN ('NaN','Infinity','-Infinity')
               OR b.broker_account_id IS DISTINCT FROM aid OR b.symbol IS DISTINCT FROM spec.symbol))
       OR (SELECT sum(CASE WHEN f.side='buy' THEN f.quantity ELSE 0 END)
           FROM fills f JOIN broker_orders b ON b.id=f.order_id
           WHERE b.strategy_id=sid AND b.broker_account_id=aid
             AND b.payload::jsonb->>'episode_id'=spec.parent_id::text) IS DISTINCT FROM spec.qty
       OR (SELECT sum(CASE WHEN f.side='buy' THEN f.quantity ELSE -f.quantity END)
           FROM fills f JOIN broker_orders b ON b.id=f.order_id
           WHERE b.strategy_id=sid AND b.broker_account_id=aid
             AND b.payload::jsonb->>'episode_id'=spec.parent_id::text) IS DISTINCT FROM 0::numeric
       OR EXISTS (SELECT FROM broker_orders b WHERE b.broker_account_id=aid
          AND b.symbol=spec.symbol
          AND (b.status IS NULL OR b.status NOT IN ('filled','cancelled','rejected','expired','aborted')))
       OR EXISTS (SELECT FROM virtual_positions v WHERE v.broker_account_id=aid
          AND v.symbol=spec.symbol AND v.quantity IS DISTINCT FROM 0::numeric)
       OR EXISTS (SELECT FROM oms_managed_positions x WHERE x.broker_account_name='live:orb'
          AND x.symbol=spec.symbol AND x.current_quantity IS DISTINCT FROM 0::numeric) THEN
      RAISE EXCEPTION 'fill/exposure/working-order proof changed: %', spec.symbol;
    END IF;
  END LOOP;
  SELECT jsonb_agg(to_jsonb(b) ORDER BY b.id) INTO actual_orders FROM broker_orders b
    WHERE b.id IN ('a4aaa36a-2888-4a4b-a676-e34d2d2f7083','76732752-59e2-42e2-840a-dab58a1c4989',
      'b1f9b7e2-3d0c-4100-8750-01eb91646a45');
  SELECT jsonb_agg(to_jsonb(t) ORDER BY t.id) INTO actual_parents FROM trade_intents t
    WHERE t.id IN ('6c313663-c277-5b20-b650-c37ed7324feb','f9e48766-f9ad-581d-b014-7068e23d5c83',
      'f4556f70-f833-5467-97ee-afe5b5b582a1');
  SELECT jsonb_agg(to_jsonb(f) ORDER BY f.id) INTO actual_fills FROM fills f
    JOIN broker_orders b ON b.id=f.order_id WHERE b.strategy_id=sid
    AND b.payload::jsonb->>'episode_id' IN
      ('6c313663-c277-5b20-b650-c37ed7324feb','f9e48766-f9ad-581d-b014-7068e23d5c83',
       'f4556f70-f833-5467-97ee-afe5b5b582a1');
  -- Literal JSON equality requires every row field, including nulls. Input arrays
  -- must be ordered by row id; absent/extra keys or rows fail closed.
  IF actual_orders IS DISTINCT FROM (SELECT v FROM momorder1_inputs WHERE k='orders')
     OR actual_parents IS DISTINCT FROM (SELECT v FROM momorder1_inputs WHERE k='parents')
     OR actual_fills IS DISTINCT FROM (SELECT v FROM momorder1_inputs WHERE k='fills') THEN
    RAISE EXCEPTION 'full fresh preimages changed';
  END IF;
  INSERT INTO momorder1_audit VALUES ('before', jsonb_build_object(
    'orders',actual_orders,'parents',actual_parents,'fills',actual_fills,'meta',m,
    'reviewed_gmex_spec',g));
  UPDATE broker_orders SET status='filled', updated_at=clock_timestamp()
    WHERE id IN ('a4aaa36a-2888-4a4b-a676-e34d2d2f7083','76732752-59e2-42e2-840a-dab58a1c4989',
      'b1f9b7e2-3d0c-4100-8750-01eb91646a45')
      AND status='cancelled';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 3 THEN RAISE EXCEPTION 'expected exactly three label corrections'; END IF;
  SELECT jsonb_agg(to_jsonb(b) ORDER BY b.id) INTO after_orders FROM broker_orders b
    WHERE b.id IN ('a4aaa36a-2888-4a4b-a676-e34d2d2f7083','76732752-59e2-42e2-840a-dab58a1c4989',
      'b1f9b7e2-3d0c-4100-8750-01eb91646a45');
  IF EXISTS (SELECT FROM jsonb_array_elements(after_orders) a
    JOIN jsonb_array_elements(actual_orders) z ON a->>'id'=z->>'id'
    WHERE a->>'status' IS DISTINCT FROM 'filled'
      OR a - 'status' - 'updated_at' IS DISTINCT FROM z - 'status' - 'updated_at') THEN
    RAISE EXCEPTION 'unapproved order mutation';
  END IF;
  IF actual_parents IS DISTINCT FROM (SELECT jsonb_agg(to_jsonb(t) ORDER BY t.id)
      FROM trade_intents t WHERE t.id IN
        ('6c313663-c277-5b20-b650-c37ed7324feb','f9e48766-f9ad-581d-b014-7068e23d5c83',
         'f4556f70-f833-5467-97ee-afe5b5b582a1'))
     OR actual_fills IS DISTINCT FROM (SELECT jsonb_agg(to_jsonb(f) ORDER BY f.id)
      FROM fills f JOIN broker_orders b ON b.id=f.order_id WHERE b.strategy_id=sid
      AND b.payload::jsonb->>'episode_id' IN
        ('6c313663-c277-5b20-b650-c37ed7324feb','f9e48766-f9ad-581d-b014-7068e23d5c83',
         'f4556f70-f833-5467-97ee-afe5b5b582a1')) THEN
    RAISE EXCEPTION 'parent/fill rows changed';
  END IF;
  INSERT INTO momorder1_audit VALUES ('after', jsonb_build_object('orders',after_orders));
  INSERT INTO dashboard_snapshots(id,snapshot_type,payload,created_at)
    VALUES (audit_id,'momorder1_label_repair',jsonb_build_object(
      'repair_id','O-1019-9:YMAT-AIXI-GMEX-single-root-labels',
      'before',(SELECT body FROM momorder1_audit WHERE phase='before'),
      'after',(SELECT body FROM momorder1_audit WHERE phase='after')),clock_timestamp());
END
$repair$;
SELECT phase,jsonb_pretty(body) FROM momorder1_audit ORDER BY phase DESC;
ROLLBACK;
