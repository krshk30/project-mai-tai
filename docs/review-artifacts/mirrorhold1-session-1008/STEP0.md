# Lane G independent Step0

Sole writer: Codex. Branch `codex/mirrorhold1-session-duplicate-scan-1008`.
Exact base: `1e15adb03c3758e647d333828bbe3090e24e832e`.
Worktree: `/Users/velkris/.codex/worktrees/mirrorhold1-session-duplicate-scan-1008/project-mai-tai`.
Codex-only worktree commit hook installed. No AGENTS.md found in the repository
or parent project tree. Read README, architecture, deployment operating model,
test layout, MIRRORHOLD1 plan/report, and commit hook instructions.

## Own read-only evidence

Capture: `capture_readonly.py` over SSH stdin; PostgreSQL READ ONLY, statement
timeout 15s, lock timeout 2s; whitelisted metadata only; no remote file writes,
broker calls, tick database, HTTP, settings outputs, or credentials.
Raw initial path: `/tmp/mirrorholdG-live-complete-1008.json`.
Final expanded capture: `/tmp/mirrorholdG-live-final-1008.json`.
Recorded projection: `tests/fixtures/mirrorhold1_session_1008_recorded.json`.
All captured groups have limit+1 overflow detection. LEFT JOIN strategies
retains absent strategy identity and reports null counts. Complete means only
the stated account/symbol/time scopes, not an unrestricted broker census.

At 2026-10-08 14:13:23.585469 UTC initial capture, 135 AIXI/FLYE buy orders,
25 bounded intents, 237 audits; no truncation and no absent strategy identities.
Expanded final capture at 14:22:58.362159 UTC retains 135 linked historical
intents and 21 actual Fill rows, with limit+1 overflow checks (200/300).
Only fills/audits preceding each replay intent are loaded, with actual IDs and
timestamps. All orders are DAY.
Both accounts (`live:orb`, `live:schwab_1m_v2`) have zero prior-session
nonterminal buys or NULL submitted timestamps in the bounded census.
This is DB evidence, not a new broker-book read.

AIXI intent `34af52a6-b13d-44b7-80ca-dafa699863a6`, event
`29cfb0e9-2f0e-4acc-a131-eb94e9a264dc`, created 13:35:04.955411Z,
quantity 127, stop 2.3600, limit 2.3700, observed shape ask 2.27 at
13:35:05.075008Z. No corresponding BrokerOrder. The OMS file has no
MIRRORHOLD1 refusal line for this intent. Its historical Webull buys include
47 prior-session rows (24 cancelled, 13 rejected, 10 filled), plus one
same-session earlier unrelated fill. The original all-history gate can stop
on an August 25 cancellation with unknown-source audit; October 2 includes
client rejections. Do not collapse the full history into the smaller blocker
count reported by the user.

FLYE has 38 prior-session Webull rejected buys. Actual retry intent
`5446a17f-af09-4d49-a91b-80c4d066a82f`, quantity 98, created
13:55:49.341617Z, stop 3.0300, limit 3.0500, ask 2.8 at
13:55:49.237768Z. BrokerOrder submitted 13:55:49.493083Z; observed fill
14:00:40.195Z, logged at 14:00:50.997Z. Local replay will stop before the
real dispatch, and its broker acknowledgement is controlled, not an observed
additional trade. Intent creation time is a replay clock proxy, not a captured
original event produced_at.

## Scope discrepancy and conservative treatment

A literal timestamp-only restriction could discard a prior-session working GTC
order. None was found in this capture. Preserve every nonterminal/working buy
across age and TIF rather than infer expiry from DAY. Unproven aborted orders
remain subject to the unchanged exact RPG abort proof.

The August 25 AIXI legacy rows have BOTH identity fields absent (not explicit
JSON null). Keeping all absent legacy identity forever would prevent the
required AIXI dispatch replay. Distinguish this structural legacy absence:
only dated prior-session terminal rows without either identity field can age
out. Explicit null/malformed segment, orphan slot, malformed payload, or
unreadable/NULL timestamp stays conservative. Do not fall back to updated_at.
Same segment is retained across age regardless of slot. Retirement requires
exact account/symbol/segment AND slot fill evidence, preserving the existing
slot mechanics. Different-slot filled rows do not retire this slot; absent,
null, or empty same-segment slot identity stays uncertain. Current pending
remains refused.

A current legacy filled row without either identity field only clears when
an actual retained Fill and its submitted timestamp both predate formation of
the current segment. This is an explicit conservative proof policy added to
avoid refusing a proven unrelated legacy fill solely for absent metadata; it
does not invent a segment/slot or use updated_at. The recorded Oct8 AIXI
07:50 fill already has a different, readable segment and needs no such fallback.

The gate is shared by dispatch, queue preparation, and restore. Session narrowing
must be opt-in ONLY at per-intent dispatch; existing tick/queue/restore calls
and eligibility/cache code stay byte-for-byte unchanged. No changes to 8%,
hold/resubmit budget, RPG proof, ineligible-today cache, strategy, bot, service,
parent C-row, shared handoff, other lanes, merge/pin/activation, or production.

Residual: a later AIXI deferred hold can STILL fail the unchanged all-history
queue/restore gate. This lane fixes direct per-intent dispatch only; it does
not promise recovery of every later hold/resubmit. A controlled test proves
the all-history queue refusal and successful session-bound direct dispatch on
the same history. Any queue/restore scope change requires a separate ruling.
