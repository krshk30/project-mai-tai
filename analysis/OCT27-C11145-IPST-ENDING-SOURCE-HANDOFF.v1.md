# C11145 IPST ending: SOURCE candidate, not acceptance or deployment

Owner Pascal; Main alone executes tests/runtime. Arendt/Gibbs/Cicero sources
are not edited. Base is literal APP821a982d1338d30cae40145d8bfa7172e3c80961.
The reviewed e772 boot freeze remains integrated unchanged. Bohr owns the
separate historical double-reprice cause work; no reprice fix is included.

Root: `/private/tmp/oct27-pascal-c11145-ipst-ending-source`
Branch: `codex/c11145-ipst-noid-ending`.
Source author has NOT imported, compiled, collected, tested, replayed or called
any provider/PG/runtime. Only source reading/editing and Git metadata/diff
checks were performed. No new rig, fence, reader package or capability envelope.

## Source contract

- Only original one-shot momentum BUY/open entries, no native order ID and
  submitted_at NULL, enter the new shared path. SELL/exits, ATR, ORB and known
  wired identities keep their original cancellation paths.
- The existing service `_reserve1_sync_pass_id` is the rate operand, not an
  elapsed-time guess. At most one no-ID lookup is started per client ID per
  actual sync pass; an unfinished physical read prevents overlap across passes.
  With two retained entries this is a per-client-ID ceiling, NOT a global
  one-call ceiling. C1/Main must confirm that interpretation before runtime.
- Broker I/O is started as a retained task OUTSIDE an await under the shared
  owner lock. Later owned observations consume the result. A two-second
  pending-read diagnostic never cancels or restarts the native read. Runtime
  close awaits retained tasks, including their actual to_thread broker calls.
  No physical completion or latency result is claimed from source alone.
- Results bind account, client ID, symbol, provider, actual sync pass and
  observation clock. A result from a different pass or stale observation is
  UNKNOWN, not carried forward as fresh absence. A read with no actual pass
  stays UNKNOWN. Owner cadence/ticks do not grant a fresh query.
- PRESENT requires actual native ID and validated account/coid/symbol/side,
  status and execution quantities, then uses normal record/cancel handling.
  Existing execution-time normalization is reused without another lookup.
- ABSENT is a distinct result, never None, an empty list, a truncated list,
  an unknown status, an HTTP failure, or a substring match in error text.
  The candidate Webull branch requires exact-client/account SDK detail with
  explicit HTTP417 plus ORDER_NOT_FOUND code. Existing wired-reader defaults
  remain unchanged; the new semantics require allow_absent=True.
- Settlement locks the exact parent and order. Rechecks own strategy/account,
  entry identity/quantity/metadata, pending/no-ID/no-submit, zero fills/events,
  zero own VP/managed exposure and one episode order. Atomic updates are one
  order aborted + one parent cancelled/terminal_no_fill with closed_ns and a
  bound receipt. Commit precedes RAM slot release and the single ending log.
- An active same-symbol entry admission/prewire/POST await prevents the new
  lookup/settlement path. UNKNOWN returns without recurring exceptions; it
  retains the slot and emits one alert, respecting an existing identity alert.

## Material blocker, not a substituted absence proof

Schwab's existing order payload does not transmit the local ml1 client ID,
and fetch_order_update requires the native order ID. No canonical complete
client-ID query is available in the reviewed adapter. This candidate therefore
returns UNKNOWN for Schwab, with no list query or automatic settlement.
The attended C11039/C11102 window3000 recipe is NOT reused as a generic rule.
No provider probe was made. C1/Main disposition is required; the original
positive IPST-on-Schwab absence/slot-free criterion is NOT MET by this source.

Webull's exact ORDER_NOT_FOUND vocabulary is already documented by the
repository's existing `_is_order_not_found`; this proposal uses a narrower
typed HTTP+code check, not its permissive string matching. Canonical provider
completeness/retention-window sufficiency still needs Main/C1 source review;
controlled test responses are not native absence attestation.

## Three case groups and focused controls, ALL AUTHOR UNRUN

1. ABSENT: existing harness, controlled IPST-shaped32 Webull one-shot entry,
   explicit canonical-response-shaped417, actual reader/mapper and real
   persistence. PM/RTH, exactly two UPDATE targets, one slot freed while the
   other remains, one log, no submit/cancel wire. This is a controlled Webull
   positive, NOT the actual Schwab incident or native historical proof.
2. WIRED/PRESENT: original Schwab native-ID cancel trace in PM/RTH unchanged;
   existing Webull FUFU control now supplies a bound native-ID detail response
   before normal cancel (no longer treats missing detail as cancellation
   authority). Existing wired/exit/BBCI-focused tests remain relevant.
3. UNKNOWN: PM/RTH x Schwab/Webull, repeated owner calls do not grant queries;
   one per actual sync pass, no settlement or slot release, one alert. An
   additional real-IPST typed-preimage control explicitly stays UNKNOWN on
   unsupported Schwab lookup; it is NOT relabelled a positive ending case.

Tests are added/updated only in existing test files/harnesses:
`tests/unit/test_momcancel1_ack.py` and `tests/unit/test_momorder1_reads.py`.
New main-case function selectors:
- `test_c11145_explicit_absent_webull_commits_two_rows_and_frees_one_slot`
- `test_c11145_wired_cancel_keeps_original_native_path`
- `test_c11145_unknown_retains_and_reads_at_most_once_per_actual_sync_pass`

Additional focused selectors in those same files:
- `test_c11145_read_deadline_does_not_start_overlapping_read_and_close_joins`
- `test_c11145_actual_ipst_preimages_no_schwab_lookup_retains_unknown`
- `test_c11145_exact_client_lookup_never_infers_absence_from_none_or_list`

The actual preimage control pins the supplied private preimages.json SHA256
6369dc5f25c4b51ded65c6c1f958222df654e52ca0c9733028f8b6aff5df4cc9.
It preserves captured payloads, order/parent IDs and business fields, maps only
fixture strategy/account foreign keys, and uses an explicitly controlled
current clock. It is not Bohr's historical request/cause replay.

Existing assertions are changed only where C11145 intentionally replaces
repeated no-ID/no-submit Refusal/error with retained bounded UNKNOWN. Initial
ambiguous ACK refusal, original reservation/no-wire checks and existing
submitted/wired safety remain. All changed assertion deltas require review;
no failed receipt has been retagged. Old files remain frozen in APP821.

Focused touched-file units for Main to select/review include momcancel1_ack,
momorder1_reads, momorder1_runtime_db, momlive1_persistence, momlive1_lifecycle
and momlive1_oms_isolation. No full suite/heavy world/new benchmark is proposed.
Actual PM/RTH route behavior, absence-contract adequacy, joins, latency,
sync duration and no-interference with BBCI/ATR remain UNPROVEN until reviewed
Main execution; neither controls nor source establish native completeness.

## Schedule and next action

Fixed source packet target12:30, head13:00, all unreviewed by14:00, READY15:30.
Source can be sealed for review independently of the historical cause. The
Schwab positive case remains a material acceptance gap: report it to C1 now,
not silently waive it or invent native identity/completeness. No integration,
runtime, installation, flags or production changes follow from this packet.
