# Bounded OLD Account-Leg Return Grammar

Source-only successor to exact359 assembly4c1366789b794fb5b407e8bb254542299fc4d9af.
Main's first actual candidate refusal at OLD_return_grammar_STOP_line_243 and
original log/audit remain retained; author does not parse or rerun actual data.

Source emitter evidence: OLD2e79 strategy_core/schwab_1m_v2.py:6776 calls
_resting_return_reason(state, "leg_skipped_" + reason, slot=slot + ":" + account).
Current359 source has the same call at6866. The log formatter emits this exact
slot string. Main's full5107-line census found3580 RETURN lines, with exactly
286 unsupported account-qualified rows, all leg_skipped_country_other_venue:
reclaim:live:schwab_1m_v2=103, first:live:schwab_1m_v2=126,
first:live:orb=50, reclaim:live:orb=7. These are supplied actual census facts,
not an author execution or a new fixed-count assertion on arbitrary input.

The parser preserves the old unqualified regex. One separate anchored regex
accepts only those four qualified slot/account pairs and that one reason.
Unknown accounts/slots/qualified positive or not-ready reasons still STOP.
The new kind ACCOUNT_LEG_CONTEXT_NOT_READINESS preserves exact full slot,
base_slot, account, reason, raw line/SHA, original clock, ordinal and bar.
It is not RETURN and cannot enter either old or C11810 readiness/count kernels.
Both candidate outputs retain every parsed account-leg row as account_leg_context;
no row is silently omitted from the output. OLD_READY_RETURNS is unchanged.

No clocks/raw bytes/provider interpretation, seed bounds, window policy,
APP334, schema, source carry/capability/expiry or caller permission changes.
Actual SHA/5107-line gates remain unchanged. Main must review the new context
and changed candidate digest independently before constructing its tracker;
the prior candidate never completed, and no old review is automatically reused.

New selection only: tests/unit/test_l12_old_account_leg_return_grammar.py.
16 intended CONTROLLED controls: four actual emitted slot shapes, eight adverse
qualified shapes/reasons, revised/historical context separation, independent
ordinary positive return and malformed-line refusal after a valid leg.
All16 UNRUN by author; no prior34/30/changed2 repeat requested. Author performed
static source reads and diff --check only; no imports/tests/PG/runtime/provider.

Existing actual122/private-restoration and populated manual-handler assembly
remain the next unfinished source work, not completed by this grammar closure.
