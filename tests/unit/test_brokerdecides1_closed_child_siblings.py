"""New sibling-membership negatives, no repetition of the prior 47 selectors."""

from copy import deepcopy
from decimal import Decimal

import pytest

from project_mai_tai.brokerdecides1_closed_owner import closed_child_episodes
from tests.unit.test_brokerdecides1_closed_child_owner import episode_rows


@pytest.mark.parametrize("fault", ["unclosed_sibling", "partial_sibling", "other_account", "reclaim_sibling"])
def test_another_buy_in_exact_opportunity_cannot_disappear_from_close_proof(fault):
    row, entry, child, buy, sell, account = episode_rows()
    extra = deepcopy(entry)
    extra.id = "other-entry"
    extra.client_order_id = "other-entry-client"
    extra_buy = deepcopy(buy)
    extra_buy.id, extra_buy.order_id = "other-fill", "other-entry"
    extra_account = deepcopy(account)
    if fault == "partial_sibling": extra_buy.quantity = Decimal(1)
    elif fault == "other_account":
        extra_account.id, extra_account.name = "other-account", "live:orb"
        extra.broker_account_id = extra_buy.broker_account_id = "other-account"
    elif fault == "reclaim_sibling": extra.payload["cw_entry_slot"] = "reclaim"
    assert closed_child_episodes([row], [(buy, entry, account), (extra_buy, extra, extra_account)],
        [(sell, child, account)]) == {}


def test_unrelated_older_opportunity_does_not_poison_this_closed_episode():
    row, entry, child, buy, sell, account = episode_rows()
    extra = deepcopy(entry)
    extra.id = "older-entry"
    extra.payload["fanout_segment_id"] = "1792750000000"
    extra_buy = deepcopy(buy)
    extra_buy.id, extra_buy.order_id = "older-fill", "older-entry"
    result = closed_child_episodes([row], [(buy, entry, account), (extra_buy, extra, account)], [(sell, child, account)])
    assert len(result["YMAT"]) == 1 and result["YMAT"][0].row_id == "managed"


def test_missing_shared_intent_is_not_native_child_identity():
    row, entry, child, buy, sell, account = episode_rows()
    entry.intent_id = child.intent_id = None
    assert closed_child_episodes([row], [(buy, entry, account)], [(sell, child, account)]) == {}
