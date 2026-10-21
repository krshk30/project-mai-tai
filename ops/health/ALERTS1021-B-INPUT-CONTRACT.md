# ALERTS1021-B Actual24h Input Request

Main/Claude existing export lane ONLY; worker requests no collection connection.
No worker polling ntfy, provider/SSH/DB/SQL, application import or replay execution.
Do not include credentials, connection strings or raw env files. Sanitized source
identities/provenance and the relevant enum/flag values suffice.

Existing `analysis/alerts1021-inputs` is the old08:26 packet:22 actual MESSAGE
records across two topics, not the new14:06 rolling24h population. The approved
99fc replay remains independent/unchanged. Do not reuse22,18, card26h class totals,
or topic delivery counters as the new24h denominator. Missing data stays UNKNOWN.

## Required Packet

Provide `ALERTS1021-B-EXPORT-MANIFEST-20261021.json` plus raw complete MESSAGE
JSONL exports for BOTH existing preopen/routine phone topics. Keep all raw event
types, message IDs, topics, original title/body, priority, original epoch time,
source order/offset. Explicit timezone-aware since/until with exactly86400seconds,
and cutoff/asof. Retain open/keepalive events in source counts, not MESSAGE count.
No filtering by known class, title, severity, 'harmless', or favorable outcome.
Supply complete pagination/retention/censoring proof. If a full24h window cannot
be established, declare partial coverage and exact missing interval; never set
complete=true or synthesize missing messages to execute a nominal replay.

Schema `ALERTS1021-B-PAGES-1`: `since`, `until`, `topics` (both names),
`complete` boolean, `coverage_provenance`, `message_denominator`, `messages`
(each `{path,sha256,topic}`; relative paths inside packet), optional `evidence`
`{path,sha256}`. One evidence JSONL row per `(topic,message_id)`, with original
source pointers/hashes and retained-evidence description. No generic overrides
such as 'safe=true' or 'would_drop=true' are accepted.

## Orphan Evidence

For each orphan MESSAGE, need EXACT original all-findings->native order IDs and
account binding. Symbol/time proximity is insufficient; oversell totals may cover
multiple orders. Include configured logical aliases for the SAME physical account
without exposing account secrets, coverage proof, and complete successful bot
broker_orders lookup keyed by those account aliases + native IDs across strategies.
Include bot row UUID/account/nativeID/strategy when present, explicit proven-empty
result when absent, query asof/transaction/cursor completeness/errors. No row in
a failed/partial/different-account lookup is NOT manual-order proof.

Evidence fields consumed offline: `binding=EXACT_RETAINED`,
`all_page_findings_bound=true`, `account_alias_coverage_complete=true`,
`lookup_status=COMPLETE_SUCCESS`, `account`, `native_ids`, `lookup_native_ids`,
`bot_native_ids`, plus exact retained source references. ANY bot row keeps the
page. Missing original identity or lookup provenance => UNKNOWN/KEEP PHONE.
Current after-the-fact absence cannot silently stand for the original successful
lookup; label any explicit reviewer-authorized counterfactual separately.

## STOP Event Evidence

Need original durable OptionA safety-stop latch create/clear history and original
page/audit association, not a25second coalescing window. Exact retained latch
`at_utc/session_date/reason/clear_policy` identifies one human-cleared safety STOP.
Different STOP after explicit clear/new latch remains a new event even within25s.
Unknown/failed STOP and owner-release/audit UNKNOWN pages stay on phone.
Include first successful delivery evidence and any previously delivered event
before the24h window. If missing, offline replay conservatively starts with no
previous delivered event, keeps the first observed one, and cannot claim exact
historical dedupe counts. Never infer same event from same reason/title/date.

Evidence fields consumed: `binding=EXACT_RETAINED`,
`latch_present_at_notification=true`, `safety_stop_latch` object, original source
references and hashes. The offline deterministic tie-break for equal-second
message times is topic/id, not a claim of subsecond broker/phone ordering.

## Execution Contract

`replay_alerts1021_b.py <manifest>` is MAIN-only after16/review/serial claim.
No network, DB, service or notification call. It imports only the pure policy
file, reads approved mirror values by AST, requires exact hashes/denominator,
keeps every raw MESSAGE before/after with source pointer and evidence grade.
UNKNOWN lookup/event proof keeps the risk page. Source-bound15:45 mirror-only
old notices are separately labelled approved99fc counterfactual, not a runtime
drift mute. Other real drift is always kept. It does NOT rerun live health checks,
maintenance, native guard behavior or prove successful installation.

Required table: every topic/id/time/title/body, before=actually delivered,
after_phone/rule/evidence_grade/source pointer. Summary includes raw event counts,
MESSAGE denominator, all kept/suppressed IDs and unknown evidence counts. Keep
all logs, daily spool observations and scans; not one unknown row omitted as PASS.
Author has not run this driver or claimed a reduction/24h result.
