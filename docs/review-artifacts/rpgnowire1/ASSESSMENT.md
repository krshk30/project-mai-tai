# RPGNOWIRE1 independent assessment - 2026-10-07

CLAIM: codex/rpgnowire1-proof-release, base 1a70da19176d2cad486e8518e1c9030f1cabb968.

## Step 0 (before source edits)

AGREE on the issue and a proof-only return to v2. No production changes were
made. Read-only PostgreSQL pulls used a 5-second statement timeout, READ ONLY
transactions, symbol SXTC and a bounded 13:40-13:49 UTC interval. Tickets were
captured at approximately 14:12 UTC; their mutable revision reflects that time,
not a claim that every field existed at 09:46 ET.

Own evidence:

- `raw/sxtc-oms-20261007.txt`: Webull explicitly `distance_proven_no_wire`
  at 13:46:06.170 UTC, then repeated `price_wait/replacement_refused`; the
  same slot is forgotten on each RPG reauthorization. The first Schwab
  opening at 13:42:04.989 was `schwab_ineligible_cached`, not a wired buy.
- `raw/sxtc-tickets-20261007.jsonl`: two tickets. Schwab 8258b37f has
  `held_unknown/exact_old_order_unproven`, reads=0, local_no_wire=false.
  Webull 262ccac4 has `price_wait/replacement_refused`, reads=0,
  local_no_wire=true, attempt=12 and a replacement saved in the journal.
- `raw/sxtc-intents-20261007.jsonl`: 15 opening intents: one Schwab
  `skipped_before_submit/schwab_ineligible_cached`; the original Webull and
  13 replacement intents all `skipped_before_submit/webull_mirror_no_fresh_quote_held`.
- `raw/sxtc-orders-20261007.jsonl`: zero broker-order rows in that interval.
  This is database absence combined with positive pre-wire refusal records,
  NOT a broker read and NOT absence alone as ownership proof.

Own code read: `_rpg_begin_cancel` recognizes a local NFQ/distance hold but
prepares it as `clear`, so the coordinator subsequently submits a replacement
and can enter `price_wait`. `_rpg_persisted_local_open` only recognizes
`webull_mirror_precheck_deferred`; it cannot recover the two recorded refusal
codes above. `rpg_buy_owned` owns every nonterminal phase. v2's existing
`rpg_handoff_authorization` already consumes terminal-zero feedback per account,
slot and generation and clears only the matching resting latch.

## Proposed fix and safety

Return a positively proven, never-wired old order to v2 through terminal
journal feedback, with no saved buy submitted. Durable opening evidence must
match account, strategy, symbol, slot, segment and generation; any broker-order
candidate or unclassified intent blocks no-wire inference. `reads=0`, elapsed
time, missing broker id and a phase name alone are never proof.

A saved replacement needs its OWN positive no-wire evidence before it can be
retired; proof about the old order does not prove the replacement. Invalidate
the exact held/queued NFQ or retained-mirror claim before releasing ownership,
so a stale serial copy cannot buy after v2 is told. Dispatching/uncertain holds,
fills, wrong generations and wired orders remain blocked or use the unchanged
cancel/readback path. New normal v2 placement still runs all current gates.

Coordination boundary: service.py and mirror_retained_hold.py belong to HOTFIX1
and will not be edited here. Existing mirror hold read/write/project methods
are reused from the serial RPG lane. No new flag, timeout, broker action,
merge or deployment is authorized by this assessment.

The recorded Schwab leg remains broker-ineligible even after ownership releases;
this fix cannot claim it will be accepted at Schwab.

## Later bounded captures (not the initial denominator)

At approximately 14:24 UTC the active-type query for row
136db58b-4394-5999-a255-8434c21434ae returned no row. The exact-ID query
then found `oms_webull_mirror_retained_hold__archived_20261007`, with
phase=retired, reason=operator_rollback_20261007. This independent mutation
by another actor is preserved in `raw/sxtc-retained-after-rollback-20261007.jsonl`.
`raw/sxtc-retained-before-rollback-20261007.txt` is the bounded read of
`/root/mirrorhold-held-rows-backup-20261007-140210.txt`, phase=held.

Replays label the archived payload, controlled active retired state, and
controlled queued-copy race separately; none is asserted to be the current
active production owner. `raw/sxtc-next-bar-20261007.jsonl` and the v2 extract
provide the actual next completed bar, ask, and probe for normal-draft tests.

Build coordination: runtime reuses existing `_mirrorhold_read/write/project`
methods, fencing the exact queued attempt as prepared. Existing admission/upsert
allows a fresh ordinary v2 generation to promote it without resetting its wire
budget. HOTFIX-owned service/hold modules remain untouched. The shared
`atr_reprice_handoff.PREWIRE_NO_WIRE_CODES` needs the two positively recorded
refusal codes so the existing identity-bearing terminal feedback validator can
recognize them; no phase/status-only waiver is introduced.
