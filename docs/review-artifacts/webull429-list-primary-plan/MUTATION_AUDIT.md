# WEBULL429 mutation trace audit

[codex] 2026-10-06 ET. Pytest return1 alone is NOT semantic-kill proof.
The harness's `killed=1` field records only that return code. This manual
trace classification is authoritative; no Mirror/control-lane claim is made.

Raw logs are preserved, unedited, in
`/Users/velkris/.codex/worktrees/webull429-list-primary-plan/project-mai-tai`.
For each name below, original filename is `.webull429-mutation-NAME.log`;
replacement-tree filename is `.webull429-mutation-v2-NAME.log`.
All selected methods were extracted and executed successfully. No missing
class-cell, import/setup, NameError, or extraction failure was observed.
Ordinary `super()` calls displayed in fake-client class source are not
extracted production methods and did not fail with a class-cell error.

## Original Fourteen At Superseded cfe81117

These logs ran against runtime37b56335; cfe81117 added only documentation and
controls. cfe81117 is NOT ready: subsequent real OMS lifecycle tests proved
the terminal-partial remainder defect. Its green CI is superseded, not repair proof.

| Name | Direct semantic failure | SHA256 |
| --- | --- | --- |
| detail_first_restored | 18 assertions + 2 DID NOT RAISE; 9 secondary runtime failures excluded | `6fd3f94d329c8dc032292330b19c05abb6f40ab37364e8b354cdc9cd8bb89f57` |
| nested_foreign_client_adopted | 1 assertion: foreign identity released | `929889fdc5baa76a3fd09cffd3925415ff900dc84032931426a2f37b820a2812` |
| stale_page_served | 2 assertions: stale row/page accepted | `7dc943a0b00aa71862d092f58324df31db2dfdaa5b58a910788bb3d0c2bbe953` |
| budget_not_counted | 2 assertions + 1 DID NOT RAISE QueryBudgetUnavailable | `eaac18772210fb6325d55fab7b8c6fecf42f0b51bc7dbcf2491f33c9c7872724` |
| page20_claimed_complete | 1 assertion: truncated scan not failed | `fd8aff12f24df61f770618be13f8c20d3d02992ea1fdd8860c4e6ac44529b3cd` |
| missing_child_claimed_readable | 2 DID NOT RAISE ValueError/UNKNOWN | `fea699a40bf426c1be3396df22fef23099dd1fe76a08b2ea56ab2246aca99af6` |
| first_filled_lost | NOT an assertion; behavioral ValueError/UNKNOWN, explained below | `72be514278c536269caa37a05c2b49af1399b9e24a17a9bbacfc6154cd8c33f3` |
| durable_proof_ignored | 3 assertions: repeated detail calls after proof | `31073134b61df81954c10d6ea4fa2a19e6d21f331d1caf6dec6348c5634fd7a2` |
| forever_attempted | 1 assertion: failed version never retried | `6ad3c4f6443942c692f48ee1ef7afa1b3755dcdcbabf8babde86a64defccc686` |
| strict_read_uses_list | 2 assertions: actual OLOX identities use list instead of fresh detail | `d34c6cf8186eb406c4e67163eed26427f348d9befc483a215e4ccf54fc53f032` |
| quantity_alias_conflict_accepted | 2 assertions: conflicting/bad quantity accepted | `31575d27ca6fb4d24d943a4f6f7b868a0413ba3ce579f0bb891f27dca7a3d14a` |
| price_alias_conflict_accepted | 2 assertions: conflicting/NaN price accepted | `6552c72c39ca7f069e02831c6e3c688edbe1743d5855c97eb439a945ce694f94` |
| unknown_status_alias_accepted | 1 assertion: unknown alias accepted | `29e050540354c465812f16b714dbcd3a568742998473ae2398c44a5a58fb8f4a` |
| list_not_found_swallowed | 1 DID NOT RAISE server exception | `d1f8ae8ffbf005758683aeedf7eb9c7be671f42d992995591d70f12e03f726eb` |

Correct narrow claim:13 mutants have direct assertion/DID NOT RAISE kills;
one has a causally traced behavioral exception. NOT14 assertion-red mutants.

For `first_filled_lost`, the only replacement is the OCO first-winning-child
`return {` to `candidate = {`. The real decoder finds T filled205@1.53 but
falls through to missing S; its own `if unknown_child` raises
`ValueError: Webull OCO child read is UNKNOWN`. The failing test is
`test_first_filled_target_does_not_request_sibling`, at the real decoder call,
before its quantity/call-count assertions. There is no harness exception.
The displayed webull.py:63 line is an extraction-relative compiled line, not
the original physical location; the exception is the decoder's explicit
UNKNOWN branch. This is not counted as a direct assertion/DID NOT RAISE kill.

`detail_first_restored` has29 failures:20 direct semantic failures plus9
secondary runtime failures (four NoneType AttributeErrors, one Decimal
InvalidOperation, three explicit fake broker429 exceptions, one concurrency
TimeoutError). The nine are excluded as independent semantic proof. The AIFA
call-count assertions alone prove restoration of the wrong read path.

## Replacement Runtime Tree

65 unmutated Webull controls pass within the437-test focused suite. Every
mutant runs in its own process against those65 controls. Original fourteen
re-run; six new mutations target real-store admission, remainder retirement,
execution deduplication, and exact-entry managed quantity.

| Name | Trace classification | SHA256 |
| --- | --- | --- |
| detail_first_restored | 22 assertions + 2 DID NOT RAISE; 9 secondary runtime failures excluded | `808d6afb8a3089c7146cf63c5504c852e56f50973aab5ddd6c28e9dca58498da` |
| nested_foreign_client_adopted | 1 assertion | `cb150f5d5d8997a9468f2ebecea23fcedbe7f5d64c0871b810d801b12c8c4be9` |
| stale_page_served | 6 assertions | `29e246074d20dec96c69e738d5419125f5a259afafe5286e0ccf719bb71be19b` |
| budget_not_counted | 2 assertions + 1 DID NOT RAISE | `85187fb8d82ceac83b77e98f609e5890999460fc1525657afc42402c7c3a75b6` |
| page20_claimed_complete | 1 assertion | `a807279e3bebc4660c0bf0ab1fa2642ea74775a07ac2e606112479e643b00325` |
| missing_child_claimed_readable | 2 DID NOT RAISE | `2e5559ba9b8d98de2c594511727bf75331e6e0e916363fa495c3bfbd52007487` |
| first_filled_lost | 1 behavioral UNKNOWN exception, NOT an assertion | `c4ce2e1203ed009ffb94d36d314b468f15b59b3dd8996490f88bc079897a6a55` |
| durable_proof_ignored | 3 assertions | `d866912ab99b6be1f6561db01b91862872ba0579ab2127bd19e0f96d42806f90` |
| forever_attempted | 1 assertion | `8facd034de2a6516676d15179396b24f80aeb1cbcc10b3c61801571b81d7803c` |
| strict_read_uses_list | 2 assertions | `47b6772abc7009fbf7f4aeabc5d51d09c0a75b5de6c1ca88adfca56d3f23dd9b` |
| quantity_alias_conflict_accepted | 2 assertions | `a89328267f2f7088c3e252b440e9db8ac9bdf647102583b6e9cfb009da930fbb` |
| price_alias_conflict_accepted | 2 assertions | `5ccb72b5aec5ff01112e4f5a23714871ffce463ded44dab64ba398e6a3d8f17d` |
| unknown_status_alias_accepted | 1 assertion | `fc4de4db28c7258ce05f6d5b60fddb7cbccc622dab15264e4a627ad230af064a` |
| list_not_found_swallowed | 1 DID NOT RAISE | `2b2ad6939dd3e5cd896c13fdf92673e56070e4ec94d025f14b7252ac303ff624` |
| terminal_venue_scope_removed | 1 assertion: Schwab fill incorrectly admitted | `2546938722334bf86ac1893d5ad23146bbea2aeda58d9fef344e32eba2523405` |
| terminal_origin_scope_removed | 2 assertions: client/UNKNOWN fill incorrectly admitted | `3c7bf1c931dbd94c74eee8486b4d66da53ce86c18f9eab173e6d5c103ea182dd` |
| terminal_remainder_reopened | 13 assertions, including all6 real OMS lifecycle variants | `0a240468a8e12925140a088a8cc4d7f603b15d0617d5e231312d89dd5a55c798` |
| terminal_execution_dropped | 4 assertions: real OMS fill/position quantity lost | `574b395151fd46396fb03f3148aab5402ec7c38fc069c2cef8882e910b6938ca` |
| cumulative_execution_doublecounted | 2 assertions: cumulative3 recorded instead of2 | `7e82f249dfdcef28b528c0d590d1e43b8613355fd714c28bd67661f2ef63aab5` |
| managed_terminal_delta_dropped | 2 assertions: managed quantity1 instead of2 | `a9ad9125a1877136fc4617aa9efa7d4047e2ccc4c7cb9f243fcbe461f0591129` |

Correct replacement claim:19 direct assertion/DID NOT RAISE kills and one
separately classified behavioral UNKNOWN exception; zero observed extraction
or class-cell errors. All six new mutation failures are explicit assertions
from production adapter/store/service paths, not source-string controls.

## Real Lifecycle Red/Green Evidence

The original adapter+real OmsRiskService+real OmsStore lifecycle test failed
all4 original variants on order status `partially_filled` versus terminal.
Raw `.webull429-terminal-partial-before-realpath.log` SHA256:
`c05a5040b5b5b9ea2a2768e4c471b7b9a1edee9bd791ae728bd4df9ac7586166`.
An earlier draft log containing two fixture-attribute errors is NOT used as
semantic proof; the corrected four-assertion log above is the before evidence.

Increasing cumulative execution exposed managed quantity1 instead of2 in
both terminal statuses. Raw `.webull429-terminal-partial-cumulative-before.log`
SHA256 `8712b6044c051aa114db43c58ac22583cecdcd6e73d75058945c8ece0a184ee0`.
After correction, six lifecycle variants cover CANCELLED/REJECTED, prior
cumulative quantity0/1/2, exact incremental-fill count, real virtual/account/
managed positions, terminal order+intent, next-poll idempotency, later real
SELL-report accounting, and no resurrection or actual broker order calls.
13 new cases include terminal-marker hygiene and negative provider/origin/
marker admission controls. Responses are explicitly synthetic; AIFA client
identity is retained from the accepted case, not presented as live repair.

Focused437 passed log `.webull429-terminal-partial-v2-all-focused.log` SHA256
`8b8276eb6d33d43a709c6c4e4fe97f997aaa2a466cb715efe6cfa1f8f746a4cf`.
