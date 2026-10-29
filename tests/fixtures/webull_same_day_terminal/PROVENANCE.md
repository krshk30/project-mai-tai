# Literal retained PCLA events, not live qualification

`pcla_original_events_20261029.jsonl` is the exact first19 lines (all PCLA)
of Main's retained25-event packet:
`OCT29-C6-ORIGINAL-CASE-RECEIPTS/P1-OWNER-BOUND/ncpl_pcla_order_events.jsonl`.
Original whole packet SHA256:
`cd4e6b229a75c856efce7f60ffb2179e4a10f75dc66ff948c9fa03aa57b287bc`.
Exact19-line prefix SHA256:
`585db761bc2947b9e0830d655c7c0be85e284e1d110aded50fa904d270301002`.
No fields, strings, types, IDs, statuses or timestamps were synthesized in this
fixture. The filled event and rejected cancellation audits remain present.

The partial original capture does not contain full typed account/order rows.
Unit tests explicitly construct CONTROLLED typed DB bindings; they do not turn
those bindings into independently witnessed historical/live DB or native facts.
The one positive stored detail belongs to event67733a2f; the four other cancelled
orders have no native ID/detail proof here and MUST stay UNKNOWN. Controlled
clock shifts and additional audit/fault mutations are named as controls only.
