# ORBCOUNTRY1 retained parent clock refusal

Follow-up to8a26424ab2986af797408ad36a9b0add2e505d1b, source-only/UNRUN.
One narrow Webull parent guard, no Settings/service writes or new IO.

`selection_from_metadata` parses a naive as_of without rejecting it. The old
exact comparison cannot match that naive parent's text against the aware
replacement event accepted by the original routed event validator. Dropping
stamp equality must not allow such a target to acquire replacement permission.
The new flagged parent check rejects a parsed nonempty naive parent stamp.
An empty as_of remains the originally permitted unknown-country provenance;
malformed/unparseable input already raises in the unchanged parser. Source
router still owns actual future-record status. No global naive-as-UTC rewrite.

Two connected actual-OMS negatives corrupt only the retained parent stamp to
naive while keeping the new event valid/aware: OFF original comparison refuses;
ON explicit clock guard refuses before cancel/replace. Both authored UNRUN.
Total roster now58=first33+connected25, not a collection/run result.
First075 and8a264 commits preserved; Main must use the latest literal source
and independently review the Main-only external V2 hook patch before execution.
