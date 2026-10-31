"""Pure SHADOW log/ledger projection. No acquisition, clocks or phone port."""

from .system import OUTPUT_BYTES, _bounded, evaluate_system


def evaluate_system_shadow(evidence, policy, *, as_of):
    """Keep supplied-policy conclusions; hard-disable every delivery eligibility.

    QUALIFIED_LIVE in a supplied policy is not a way around the SHADOW adapter.
    Candidate TRUE is a candidate detection, not a historically qualified page.
    Main/L4 own bounded ledger persistence, not this evaluator.
    """
    findings = evaluate_system(evidence, policy, as_of=as_of)
    rows = [dict(finding, paging_eligible=False,
        shadow=dict(mode="SHADOW", delivery_action="NONE", existing_senders_unchanged=True,
                    would_page_under_supplied_policy=finding["paging_eligible"],
                    candidate_detection=finding["status"] == "TRUE" and
                        finding["qualification"]["mode"] == "CANDIDATE_REPLAY"))
        for finding in findings]
    _bounded(rows, OUTPUT_BYTES)  # Explicit refusal; no truncated clean ledger.
    return rows
