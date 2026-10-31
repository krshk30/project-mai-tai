"""Explicit C119:54:03 TODAY-paper NULL successor; raw rows stay unchanged.

None is the existing boundary: raw SQL NULL has missing condition evidence.
The named opt-in treats an ACTUALLY PRESENT NULL as regular/no_conditions,
using the unchanged classifier. Missing column, policy, malformed codes and
round-lot guards retain their original conservative outcomes.
"""


NULL_REGULAR_V1 = 'C1_20261028_195403_TODAY_PAPER_PRESENT_NULL_REGULAR_V1'


def require_null_policy(value):
    if value is not None and value != NULL_REGULAR_V1:
        raise ValueError('explicit_supported_TODAY_null_policy_required')
    return value


def classify_condition_value(trade, conditions, policy, *, column_present, null_policy=None):
    require_null_policy(null_policy)
    if null_policy is None:
        from project_mai_tai.atr_simulation.execution_eligibility import classify_execution
    else:
        # C1 measured policy-object hashing; Main owns this additive validated
        # identity-rule cache. Original sealed classifier/default remain exact.
        from project_mai_tai.atr_simulation.same_sip_eligibility import classify_execution
    if type(column_present) is not bool:
        raise ValueError('actual_condition_column_presence_required')
    if not column_present and conditions is not None:
        raise ValueError('absent_column_cannot_supply_conditions')
    # The old default is intentionally unchanged for existing stored SQL NULL.
    present = column_present and (null_policy is not None or conditions is not None)
    return classify_execution(trade, conditions, policy, conditions_present=present)


def classify_row_execution(trade, row, policy, *, null_policy=None):
    return classify_condition_value(trade, row.get('conditions'), policy,
        column_present='conditions' in row, null_policy=null_policy)
