# MOMBUFFER1 landed input census (author-only)

Source: `/Users/velkris/.codex/task-queues/analysis/MOMBUFFER1-EXPORT-MANIFEST-20261021.md`
and `MOMBUFFER1-EXPORT-SIGNALS-20261021.csv` (SHA256
`3f283ba2ba040f8525392130f2855dd369a867b26a6c4003bf556b5b633dab69`).
Read-only local standard-library CSV aggregation, NOT application/replay execution.
No exporter scripts run. No providers or database opened. Runtime is main-only after16.

## Complete denominator

94 unique signal_logical_id: 50 refused_at_handoff + 17 merged_into_episode +
27 primary episodes. Day signal/episode/filled counts: Oct15 21/6/3;
Oct16 18/7/5; Oct19 29/9/5; Oct20 20/4/4; Oct21 6/1/0.
Oct17/18 no DETECTED rows per exporter declaration, not independently queried here.
50 handoff reasons: volume<50000 26, window9, pending/held8, volumeunknown4, cooldown3.

27 = filled17 + brokerreject1 + noquote4 + cap2 + equalityrefusal3.
Raw 62 ORDER records: buy21 (filled17/rejected1/aborted2/pending1), sell41
(filled14/cancelled26/aborted1). Exactly 21 distinct entry client IDs, each
associated with one episode; six primary episodes have no entry order.
Manifest prose 20 durable/22 buys/40 sells conflicts with these raw identities;
retain discrepancy for main/reviewer, do not silently omit a row. The extra
sell beyond the stated40 is an aborted EYPT exit, not a second buy.
Static uniqueness does NOT establish counterfactual no-double-episode behavior.

## Header-Bound Episode Table

`record` = one-based SIGNALS CSV DATA-record ordinal, not physical line number.
Fields bound by exact headers: row_kind, session_date, symbol, episode_id,
signal_logical_id, outcome, reason, limit_saved, qty_saved, cap, source/source_line.
Quotes join by exact episode_id, not symbol alone. Their headers include
signal_observed_utc, window_start_utc, window_end_utc, row_type, sip_timestamp_ns,
participant_timestamp_ns, bid, ask, sizes/exchanges, conditions, indicators,
tape, sequence_number and ms_from_signal_observed. No received_at/cache clock.
All files are `MOMBUFFER1-EXPORT-QUOTES-<symbol>-<date>.csv` under analysis/;
each episode below has ONE prevailing-before-window row plus the listed window rows.
Multiple episodes in one symbol/date file are kept distinct. IDs shown in full.

| record | date | symbol | episode_id | actual class | in-window quotes |
|---|---|---|---|---|---:|
|9|2026-10-15|ZBAO|7e3bb11a-3c1c-53b1-aa3e-fb38f73b6628|noquote|0|
|13|2026-10-15|BBCI|55e84cc9-ba92-5b88-a42f-766a80f4ef12|filled|54|
|40|2026-10-15|MLEC|55a16350-89ac-5c4f-87a9-6d4334ea9e98|filled|159|
|48|2026-10-15|SPRC|c4df63d4-ba39-5d41-8954-f64541cc37b1|filled|81|
|55|2026-10-15|ZBAO|9174a004-eafd-5be9-8f61-de2571256326|noquote|57|
|58|2026-10-15|RPGL|fffa5363-807c-5c66-b12a-208100fbc015|noquote|20|
|71|2026-10-16|JBDI|326566bf-3e3f-5d57-8663-5a682479d6bb|filled|23|
|78|2026-10-16|JBDI|8ba371de-634e-5c3e-9542-52138b665650|filled|442|
|88|2026-10-16|WORX|130c57c9-d542-57d3-844b-a277664763a5|filled|160|
|110|2026-10-16|XLAB|a5d20f9b-d099-5f7d-a79a-7b3319976773|filled|6170|
|128|2026-10-16|RBNE|383a3322-5a95-510a-bac6-32cbf50bbd1e|noquote/timeout|0|
|153|2026-10-16|YXT|ec0cbc98-9a20-5773-a967-11285e35afc9|cap|45|
|171|2026-10-16|FUFU|54485123-0884-51b9-b8b5-1eb3c51d33a9|filled|33|
|209|2026-10-19|EYPT|2d658060-26c5-5850-9794-95761e128755|cap|23|
|213|2026-10-19|EYPT|3d4fb31d-b4fa-5865-a489-52bd336803ae|filled|36|
|225|2026-10-19|EYPT|83221e5f-6a8a-5032-bbcb-e03b8d61561f|filled|25|
|235|2026-10-19|YMAT|6c313663-c277-5b20-b650-c37ed7324feb|filled|953|
|254|2026-10-19|FRGT|ab447ed3-765f-50c6-9c10-366066ca3c0f|equality/prewire|1060|
|262|2026-10-19|YHC|80afc500-7b72-5cc8-8bca-c9878d6d1be0|brokerreject|601|
|276|2026-10-19|WORX|ae7931be-95fd-5f54-99d5-eff1808ec880|filled|141|
|289|2026-10-19|WORX|f70625b5-f1d5-5ddd-bcee-515332fc77a4|filled|903|
|295|2026-10-19|WORX|f723bbd2-5477-5a0b-b3ca-bc9ab023c5a7|equality/postdurable|1624|
|342|2026-10-20|AIXI|f9e48766-f9ad-581d-b014-7068e23d5c83|filled|239|
|358|2026-10-20|GMEX|f4556f70-f833-5467-97ee-afe5b5b582a1|filled|142|
|382|2026-10-20|XCH|b7798c4f-ad88-5bb2-b3f1-ec431bacfcb4|filled|75|
|442|2026-10-20|CRMT|7c9460f7-25cf-59a4-a4b8-8d888a031c28|filled|21|
|496|2026-10-21|SPHL|9ba4a2c0-aacc-5dba-a444-55a5e290ed2f|equality/postdurable|1957|

## Historical Vs Controlled Clock

Actual recheck ask/limit/qty, first quote_ns, feed/cache identity, receipt and
callback clocks are UNKNOWN, not inferred from SIP or saved first limit.
SPHL saved3.75/P3.87/cap3.92805; FRGT2.11/P2.16/cap2.19240;
WORX3.08/P3.17/cap3.21755 are legal saved prices under the cap-only rule.
Raw in-window tape availability does not refute the bot's missing-cache refusal;
zero-window prevailing rows do not establish local staleness either.

Author driver binding keeps all94 rows, merged membership and all27 windows.
The strict retained-history executor driver intentionally returns UNKNOWN when
owner/control/receipt/schedule inputs are absent. Any tape-driven scenario must
label each selected actual quote pointer, controlled clock and assumed receipt;
clock offsets/DB waits are explicit unmeasured assumptions, not observations.
Before/after3 SENDs, no new stale/cap sends, joint two-slot/merge/no-double episode,
production CAS and native adapter races are UNRUN acceptance gates, not PASS.
No invented cache facts or synthetic incident substitutions are supplied.
