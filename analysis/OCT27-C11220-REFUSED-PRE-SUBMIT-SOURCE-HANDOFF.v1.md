# C11220 refusal-marker ending SOURCE successor

Owner: Pascal, sole APP SOURCE. Main alone reviews/integrates/executes. SOURCE
controls below are UNRUN by author; no imports, collection, tests, providers,
PG, replay, deployment or live writes were performed. Frozen 2820 is unchanged.

## Intake / schedule

- C11220 (human / C1 12:20), priority ending fix: implement marker in the real
  UnsentEntryConflict handler; exact settlement only with no dispatch evidence.
- C11240 (C1 12:40 additive): require first-and-only episode attempt. Implemented
  as an atomic new-order preparation seal, not an inference from current refusal.
- C11219 stale-observation self-review: refresh after cancellation and prohibit
  stale saves from reopening the settled parent; preserve durable closed_ns.
- SOURCE authoring complete at this candidate; Main/C1 review and actual focused
  results are required before readiness. Original ETA acknowledgment was late
  (12:18 against 12:15); original 2820 head was 12:08 against 14:00. READY 15:30
  remains the target, not a SOURCE-only acceptance or deployment claim.

## Exact base / scope

Parent 2820af98941284bd145e6bfbdd61889e927ed853, itself on frozen APP
821a982d1338d30cae40145d8bfa7172e3c80961. Isolated ordinary clone:
/private/tmp/oct27-pascal-c11220-refused-entry-source
Branch codex/c11220-refused-entry-ending. No frozen donor edits. Head and whole
diff/source inventory hashes are emitted externally after the commit.

## First-and-only, not current-try-only

execution.py:173 requires a successful fresh durable claim. A duplicate/restored
episode returns/refuses before this prepare. execution.py:211 creates a UUID
attempt identity and string count=1 only after that claim. Both subsequent fresh
price checks preserve these exact fields, as does the native boundary validation.

persistence.py:550 prepare_order locks the parent for sealed entries. It refuses
an existing coid, any prior parent attempt seal, any existing order under that
parent, non-claimed state, or a prior first_submit_ns. The parent attempt seal and
new order are committed together in the existing _run_db unit. Thus count=1 is a
single newly prepared admit attempt, NOT a retrospectively guessed retry count.
admission.py:175-181 additionally permits begin_submit only from claimed with no
prior first_submit_ns. A first-refresh refusal may precede begin_submit; it still
belongs to this sole freshly prepared attempt and never reached submit.

execution.py:222-225 is the actual UnsentEntryConflict handler, before the only
submit await at :284. It awaits the existing DB unit to record refused_pre_submit
true and JSON attempt/episode/client/count/reason, then rethrows the ORIGINAL
conflict. Marker-write uncertainty logs UNKNOWN and does not convert it into
settlement authority. The marker writer refuses prior permits, ACK identity,
terminal events, broker events/fills, cleanup attempts and mismatched ownership
or attempt seals; such cases remain retained without an eligible new marker.

If the first refresh refuses while the durable episode is still claimed, that
same marker transaction changes it to cancel_requested, without a send or slot
release. RAM follows only after the unit returns. This makes the order visible
to MOMCANCEL1 and restart restoration rather than parking it as quote-only.

## EXACT pre-HTTP dispatch write / NONE qualification

Controlled normal entry: oms/bot_control.py:524 opens the transaction, :588
writes order.payload[bot_control_permit] with status MAY_HAVE_SENT, and :601
leaves the transaction before returning the permit. This is a committed
dispatch-intent record, not an ACK. bot_control_runtime.py:355 awaits that
transaction; :385 authorizes before :397 delegates the adapter submit.
service.py:881-884 installs that wrapper only when bot_control_enabled is true.

Actual transport: schwab.py:1491 calls before_transport immediately before
urllib.request.urlopen at :1492. webull.py:1058 calls it immediately before
client.get_response at :1059. entry_wire.py:72-75 sets dispatched=True IN MEMORY.
Webull's webull_wire_submitted_at_utc is report metadata at this point, not a DB
commit. store.update_order_from_report records submission after a response.

With bot control DISABLED, a durable normal-entry pre-HTTP dispatch write is
NONE in these paths. No absent permit, no current lexical refusal, and no null
submitted_at alone proves absence of an EARLIER dispatch. Settlement here is
restricted to the newly atomic first-and-only seal plus the actual pre-submit
handler marker; an existing/legacy/no-marker episode cannot acquire that seal
through re-admission. No retroactive old IPST marker or backfill is implemented.

For controlled-entry races, authorizing under the order lock now rejects a MOM
refused_pre_submit marker before writing a permit. If authorization wins first,
the marker writer and final settlement both see the permit and refuse. The new
guard is MOM-only; ATR and other strategies do not acquire a new refusal rule.
Cleanup journal dispatch is a DIFFERENT path: bot_control_journal.py:350 records
MAY_HAVE_SENT + dispatch_intent_at. Both new units conservatively reject any
BotControlAttempt bound via target/entry/existing order or client identity,
including prepared records, rather than infer those records mean no dispatch.

## Settlement, lookup and ownership

persistence.py:332 locks both own rows; requires typed current-pass/fresh lookup,
exact account/provider/symbol/coid/order-parent binding, the matching first-attempt
seal and refusal marker, no native ID/submitted timestamp/permit/ACK/terminal
marker, no cleanup attempt/event/fill/exposure/other episode orders. It does NOT
reuse/relax the failed reprice preimage predicate. Marker absence or uncertainty
cannot retire either row. Updates remain the own order aborted + parent cancelled
with terminal_no_fill/closed_ns. The successful runtime log includes
no_wire=true settlement=refused_pre_submit_proven. Slot release follows DB return.

Schwab: local proven pre-submit refusal is the positive; no broker list or HTTP
lookup is substituted. Without it, no canonical client-ID reader exists here,
so UNKNOWN stays retained. Webull: exact account/client-ID detail HTTP417 plus
native ORDER_NOT_FOUND is an EXTRA positive only WITH the marker. No list,
empty body, missing response, wrong HTTP, substring, throttle or transport error
means absent. This is client-ID endpoint semantics with local no-send proof,
not a generic broker-list completeness claim or live-provider attestation.

PRESENT no-ID Webull reports retain explicit native account, coid, symbol, side
and native-ID checks regardless of whether absence is allowed; a new separate
lookup_missing_entry flag preserves that identity check for NO-marker reads.
Normal wired reads/cancels use their unchanged defaults and original path.

Existing candidate2820 nonblocking retained task/actual sync-pass rate gate and
physical close join remain. Ceiling is one read attempt per client per actual
pass, with an additional literal two-start budget for the entire current pass
so even within-pass slot turnover cannot create a third read. This is NOT a
claimed single global read. Slow
tasks cannot overlap another read for that identity. No new sync timer exists.

_observe refreshes its DB snapshot after cancellation. It adopts durable closed_ns
when terminal zero-fill is observed. save locks the parent, refuses stale
nonterminal reopening of this new ending receipt, and preserves its closed_ns.
This also handles commit-success/result-uncertainty conservatively; no success
is inferred solely from an exception. A crash can still prevent emission of a
success log; this is not an exactly-once durable logging protocol.

## Main-only controls / three case groups (ALL AUTHOR UNRUN)

1. New future IPST-shaped conflict: real reprice guard -> actual exception
   handler -> committed marker -> cancellation through real LiveActor._service_owned
   twice -> exact two owned rows terminal / one slot freed / other slot retained /
   one ending log. PM/RTH x Schwab/Webull x first/second-refresh. The deterministic
   changed-preimage injection is a CONTROL, NOT a historical cause reproducer.
2. Real wired cancellation from existing harness: original Schwab POST / GET /
   DELETE / GET / GET, no new missing-ID lookup; PM/RTH. Existing BBCI/Webull,
   partial-fill, ambiguous-ACK and exit tests remain in the touched unit modules.
3. No marker / simulated uncertain send: UNKNOWN retained, bounded per-pass read,
   one alert. The actual original IPST6369 captured rows remain a NO-backfill
   negative; historical cause remains UNKNOWN, per accepted C11242.

Exact primary selector names in tests/unit/test_momcancel1_ack.py:
- test_c11220_real_conflict_marker_settles_through_actor_and_stays_closed
- test_c11145_wired_cancel_keeps_original_native_path
- test_c11145_unknown_retains_and_reads_at_most_once_per_actual_sync_pass
- test_c11145_actual_ipst_preimages_no_schwab_lookup_retains_unknown

Additional safety selectors in that same existing unit file:
- test_c11220_two_retained_clients_max_two_reads_per_pass
- test_c11220_slot_turnover_does_not_allow_third_read_in_same_pass
- test_c11220_conflict_marker_cannot_override_dispatch_or_attempt_uncertainty
- test_c11220_prior_permit_conflict_never_becomes_current_attempt_absence
- test_c11220_webull_order_not_found_without_marker_retains_unknown
- test_c11220_settled_owner_rejects_stale_save_and_preserves_closed_ns
- test_c11220_marker_persistence_failure_keeps_original_conflict_and_owner
- test_c11145_read_deadline_does_not_start_overlapping_read_and_close_joins

Other touched units:
- tests/unit/test_momorder1_reads.py::test_c11145_exact_client_lookup_never_infers_absence_from_none_or_list
- tests/unit/test_killsw1_commands.py::test_c11220_refused_pre_submit_cannot_later_acquire_wire_permit

Main should review the literal delta first, then execute the existing touched
unit files and focused existing reprice/HTTP/STOP controls as authorized. No
author PASS, native evidence, historical cause, baseline timing, full-suite
acceptance, integration, installation or operator GO is claimed by this packet.
