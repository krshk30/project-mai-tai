"""One capability claim is atomic even across simultaneous local callers."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

from project_mai_tai.brokerdecides1_prewire import PublicationBoundary
from project_mai_tai.brokerdecides1_prewire_authority import SIDECAR, write_fresh_attempt
from tests.unit.test_brokerdecides1_entry_hooks import ACCOUNTS
from tests.unit.test_brokerdecides1_prewire_protocol import case as case


def test_simultaneous_boundaries_claim_one_fresh_capability_once(case):
    draft = replace(case.draft, metadata={**case.draft.metadata, "clearwait_dispatch_token": "thread-claim"})
    capability = write_fresh_attempt(case.factory, {
        "schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": "WORX",
        "opportunity_id": "100", "account_names": list(ACCOUNTS), "account_name": ACCOUNTS[0],
        "kind": "attempt", "attempt_token": "thread-claim"})
    setattr(draft, SIDECAR, capability)
    barrier = Barrier(2)

    def bind():
        barrier.wait(timeout=5)
        try:
            return PublicationBoundary(draft, ACCOUNTS[0], ACCOUNTS)
        except ValueError as exc:
            assert str(exc) == "fresh_journal_capability_consumed_or_mismatched"
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: bind(), range(2)))
    winners = [outcome for outcome in outcomes if outcome is not None]
    assert len(winners) == 1
    proof = winners[0].refuse("controlled", 1300)
    proof.validate()
