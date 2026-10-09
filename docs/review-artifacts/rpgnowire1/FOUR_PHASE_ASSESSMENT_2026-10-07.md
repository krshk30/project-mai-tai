# RPGNOWIRE1 clarified four-phase assessment

AGREE before edits on top of d255fcf59b68ce11dfeb92ddd161a4184fe1baf2.
The latest explicit instruction supersedes the prior terminal-only narrowing:
allowed phases are exactly expired/refused/clear/held_unknown. Clear and
held_unknown are ACTIVE coordinator phases, not falsely called terminal.
Price_wait is excluded until a legitimate phase transition.

Own code read confirms safe admission with the existing exclusive durable proof:
both old opening and any saved replacement require independent exact positive
pre-submit intent evidence; entire-generation/exact-client BrokerOrders, wire
hints, Fill rows, no-rebuy/fill latches and uncertain dispatch veto transfer.
Locked phase/revision CAS and the exact retained token fence stay. No metadata,
reads=0, missing order row or log line alone proves no wire. Nothing submits the
saved BUY. Terminal-zero feedback updates only the matching v2 account/generation.

Expired/refused are already in the d255 gate; no expiry-specific source fix is
needed. The source delta required by the clarified rule adds clear/held_unknown,
not price_wait. Tests must prove positive and missing-proof cases for all four
phases on both recorded SXTC accounts, and preserve price_wait's safe protocol.
The concrete 14:28 liquidity-floor replay must distinguish exact retained
positive intent proof from the observed no-wire/forgotten log messages.

Ruff independently reports F401: unused sqlalchemy.select in the new test file.
The inspected d255 push Validate job is still in unit tests; its Ruff step has
not executed yet, so this is an independently reproduced lint failure, not a
claim that GitHub's completed log confirms it. Same PR/branch, no rebase/amend/
force, no production action, no HOTFIX or staging edits.

Own bounded read-only follow-up at approximately 17:52 UTC confirms the exact
13:46:06 forgotten/no-wire, 14:28:02 expired/liquidity_floor and 15:12 rpg_owned
log lines in raw/sxtc-expired-log-20261007.txt. Those messages are not the positive
proof: replay still requires the separately captured exact old and saved intents.
This applies a recorded later phase transition to the earlier mutable capture,
not a claim that we captured the 14:28 database revision. The normal 15:12 draft
uses the recorded short trail, while the original next-bar tests use the actual
14:12 completed candle. No invented fill or Schwab eligibility is claimed.
