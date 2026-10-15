-- First executed16-case page of181 retainedcases; replaceVALUES andoffset for
-- pages16..176 (last5). READONLY typed-key failure beforecapture was corrected
-- by casting payload metadata tojsonb for the ? operator. No rawpayload writes.
BEGIN READ ONLY;
SET LOCAL statement_timeout='2500ms';
WITH cases(case_id,order_id,replay_at) AS (VALUES
('0bf945ac-7c4b-4bf8-b1b6-574fc1c5fa58'::uuid,'2e516e3c-deef-418a-a5da-9425162e1121'::uuid,'2026-10-05T13:37:25.217431+00:00'::timestamptz),
('574ceebf-bb16-4b11-b33d-e9896f1434b1'::uuid,'2e516e3c-deef-418a-a5da-9425162e1121'::uuid,'2026-10-05T13:37:25.217431+00:00'::timestamptz),
('01df460d-5a05-4dc1-955f-e3147e911905'::uuid,'768aca73-1804-4692-ac29-02e59833225e'::uuid,'2026-10-05T16:44:30.796083+00:00'::timestamptz),
('8f3700c5-a471-459a-b64f-624772cfbd2f'::uuid,'768aca73-1804-4692-ac29-02e59833225e'::uuid,'2026-10-05T16:44:30.796083+00:00'::timestamptz),
('5f3a6926-f96d-4f30-ba4a-5ece42048e02'::uuid,'d5227349-a40c-4cbb-b381-7cc3a7c4919c'::uuid,'2026-10-05T16:50:32.576118+00:00'::timestamptz),
('fa3cf1c1-577a-4ca0-91a8-e442eaa995b4'::uuid,'d5227349-a40c-4cbb-b381-7cc3a7c4919c'::uuid,'2026-10-05T16:50:32.576118+00:00'::timestamptz),
('33cd09e6-83dc-40e1-aab3-0508b5f8341e'::uuid,'1903601e-8336-43ec-a7eb-03b0756c0190'::uuid,'2026-10-06T13:37:24.131826+00:00'::timestamptz),
('7c501ff7-3e4a-49b6-a6a8-6fdf7c1a8259'::uuid,'1903601e-8336-43ec-a7eb-03b0756c0190'::uuid,'2026-10-06T13:37:24.131826+00:00'::timestamptz),
('e362f6c3-7720-4e17-9b67-bcfbffeaf0fc'::uuid,'3ac59657-9c96-4fe1-b661-a95b336e879a'::uuid,'2026-10-06T13:37:24.131826+00:00'::timestamptz),
('eab5b716-87a0-4f9b-9905-e6fa70740b62'::uuid,'3ac59657-9c96-4fe1-b661-a95b336e879a'::uuid,'2026-10-06T13:37:24.131826+00:00'::timestamptz),
('ab124c9a-2489-478f-9f38-4033c2f777cc'::uuid,'8271449a-be0a-4325-9216-c8ead327376b'::uuid,'2026-10-06T14:00:58.885668+00:00'::timestamptz),
('fc211d7c-6165-47c2-83e8-914e3c786068'::uuid,'8271449a-be0a-4325-9216-c8ead327376b'::uuid,'2026-10-06T14:00:58.885668+00:00'::timestamptz),
('22350af3-9a0c-4f20-8cb1-4be639593ec7'::uuid,'a6638ab3-df1e-40c7-b963-08f6fed2072b'::uuid,'2026-10-06T14:07:29.983009+00:00'::timestamptz),
('b5cc19f1-e001-4ae0-b861-431249e701a4'::uuid,'2ca7e00f-79f5-447b-9d31-d2bcf659f5c8'::uuid,'2026-10-06T14:07:29.983009+00:00'::timestamptz),
('beaebef7-4c83-49c0-abd5-effc03b92417'::uuid,'a6638ab3-df1e-40c7-b963-08f6fed2072b'::uuid,'2026-10-06T14:07:29.983009+00:00'::timestamptz),
('e90539fc-7b21-4595-9423-b8bc2aa3b188'::uuid,'2ca7e00f-79f5-447b-9d31-d2bcf659f5c8'::uuid,'2026-10-06T14:07:29.983009+00:00'::timestamptz)
), evidence AS (
 SELECT c.case_id,c.order_id,c.replay_at,counts.accepted_rows,counts.accepted_origin_counts,
 CASE WHEN ack.id IS NOT NULL THEN row_to_json(ack) END AS latest_accepted,
 CASE WHEN last_event.id IS NOT NULL THEN row_to_json(last_event) END AS latest_any
 FROM cases c
 LEFT JOIN LATERAL (
 SELECT count(*) AS accepted_rows,
 json_build_object('broker',count(*) FILTER(WHERE event_source='broker'),
                   'unknown',count(*) FILTER(WHERE event_source='unknown'),
                   'client',count(*) FILTER(WHERE event_source='client'),
                   'other',count(*) FILTER(WHERE event_source NOT IN('broker','unknown','client') OR event_source IS NULL)) AS accepted_origin_counts
 FROM broker_order_events WHERE order_id=c.order_id AND event_at<=c.replay_at AND event_type='accepted'
 ) counts ON true
 LEFT JOIN LATERAL (
 SELECT id,event_type,event_source,event_at,
 (SELECT json_agg(k ORDER BY k) FROM jsonb_object_keys(payload::jsonb) k) AS payload_keys,
 CASE WHEN jsonb_typeof(payload::jsonb->'metadata')='object' THEN
 (SELECT json_agg(k ORDER BY k) FROM jsonb_object_keys(payload::jsonb->'metadata') k) END AS metadata_keys,
 CASE WHEN payload->>'reason' ~* 'cancel|unconfirmed|unknown|working|sync|status' THEN
 regexp_replace(regexp_replace(left(payload->>'reason',300),'[[:alnum:]._%+-]+@[[:alnum:].-]+','[EMAIL]','g'),'[0-9]{8,}','[LONG_ID]','g') ELSE NULL END AS relevant_reason,
 md5(coalesce(payload->>'reason','')) AS reason_digest,
 payload->'metadata'->>'source' AS metadata_source,
 payload->'metadata'->>'status_source' AS status_source,
 payload->'metadata'->>'oms_broker_sync' AS oms_broker_sync,
 payload->'metadata'->>'broker_status' AS broker_status,
 payload->'metadata'->>'webull_broker_place_time' AS webull_broker_place_time,
 payload->'metadata'->>'webull_broker_filled_time' AS webull_broker_filled_time,
 payload->'metadata'->>'webull_wire_submitted_at_utc' AS webull_wire_submitted_at_utc,
 payload->'metadata'->>'webull_local_no_wire' AS webull_local_no_wire,
 CASE WHEN (payload::jsonb->'metadata') ? '_webull_list_evidence' THEN true ELSE false END AS has_webull_list_evidence,
 CASE WHEN (payload::jsonb->'metadata') ? '_webull_terminal_proof' THEN true ELSE false END AS has_webull_terminal_proof
 FROM broker_order_events WHERE order_id=c.order_id AND event_at<=c.replay_at AND event_type='accepted'
 ORDER BY event_at DESC,id DESC LIMIT 1
 ) ack ON true
 LEFT JOIN LATERAL (
 SELECT id,event_type,event_source,event_at FROM broker_order_events
 WHERE order_id=c.order_id AND event_at<=c.replay_at
 ORDER BY event_at DESC,(event_type NOT IN('accepted','partially_filled')) DESC,id DESC LIMIT 1
 ) last_event ON true
)
SELECT json_build_object('captured_at',clock_timestamp(),'offset',0,'count',count(*),
 'rows',json_agg(evidence ORDER BY case_id)) FROM evidence;
ROLLBACK;
