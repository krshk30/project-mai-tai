# C11419 minimal owner-context APP diff for C1 review

SOURCE ONLY, dedicated successor of1a59d5c48d3278ab0caf14485733479af60daf0c
(included81 APP preimage). Main/C1 exact diff review BEFORE fixed controls or
changed actual AHT. No author imports/tests/AST/compile/PG/replay/provider.
No ordinary FULL/GO/source carry is implied by this source checkpoint.

## Actual causal preimage, not setup failure

Main FIRST1 XML:
/private/tmp/OCT26-L111A59-C11419-CAUSAL-PREIMAGE-FIRST1-20261026.xml
SHA91c8de4090e05477bc691437c212023eb21baee4aa765c672189c07d1f5c3f3a.
Actual1FAIL/1.386s, intended assert not resurrected at test130. Its actual
scope_lifetime property records parent_task4448536128, child4448537856,
token4466777296, parent_popped_before_child_read=true,
child_inherited_parent_token=true, completed_parent_token_resurrected=true,
native_joined=true, pending0 and scope_count_after_owner1. These are retained
actual identity witnesses, not fabricated IDs, same-clock aliases or a new PASS.
Original full-AHT340451/error0/drafts0/477history-active failure remains intact;
this confirms the reachable APP route, not every historical token's identity.

## Entire APP delta (one file; no other APP paths)

services/schwab_1m_v2_bot.py imports copy_context beside existing ContextVar.
At actual _enqueue_keepwatch_bar owner creation only:

```python
owner_context = copy_context()
owner_context.run(self._keepwatch_history_context.set, None)
task = asyncio.create_task(self._run_keepwatch_bar_owner(symbol),
                          name=f"keepwatch-bar:{symbol}", context=owner_context)
```

It copies the caller's context then resets ONLY the history-operation variable
IN THE COPY. Parent operation/context, all unrelated variables (including L12
and bar-consumption variables) remain unchanged. No empty Context/global wipe.
The persistent child cannot borrow/re-register the completed parent operation.
Its real native task still registers native scope before membership and joins
normally; pending inbox/current work/epoch/freshness/UNKNOWN/quarantine/fleet/
dangerous/exit/ownership guards remain byte-identical. No scope dict/set is
cleared, no native work declared done, no poll/timer interval/threshold changed.
Other task sites and parent history registration/finally are untouched.

## Same focused source and class sweep retained

tests/unit/test_keepwatch_history_context_ownership.py is BYTE-IDENTICAL to1a59:
SHAaf9cd451b622a1c824fa0ec2f055ee1724f914e7ba4f2d4a503c6dd8ead644c5.
Eight-node selection remains the exact one in
analysis/L11-C11419-HISTORY-CONTEXT-CAUSAL-CONTROLS-20261026.md, including
active parent, native/unjoined cancellation, owner/pending cancellation,
foreign symbol/anchor/epoch and independent-context sentinel preservation.
No assertions relaxed or legacy controls repeated by author. Only causal1 was
actually run by Main on the PREIMAGE; the other7 and fixed8 are UNRUN here.
The complete static L11/L12 task-creation class sweep stays in that same note;
no changes to Arendt's L12 context/joins or any other worker's files.

After C1 review: Main may run the exact fixed focused selection under its
source-bound memory fence/fresh output/inventory, preserving the preimage FAIL.
Only after reviewed fixed controls may it claim ONE changed actual AHT original
full feed expecting08:46q236/09:30q237 or a truthful refusal. Then reseal/newFULL,
heavy9/all original gates. No native fill or historical permission supplied.
42ff/2f5 remain evidence only under OptionB, not an install path.
