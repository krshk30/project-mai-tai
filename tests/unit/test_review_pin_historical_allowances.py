from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from test_review_pin_audit import audit, MergedPull, SCRIPTS
from audit_review_pins import load_config
from review_pin_gate import GateError
import audit_review_pins as module


POLICY = SCRIPTS.parent / ".github/review-pins/audit-policy.json"


def refused(pull):
    return GateError(
        f"REFUSED: PR #{pull.number} head {pull.head_sha[:9]} has no committed review pin", 1,
    )


def test_exact_nine_historical_exceptions_are_visible_not_covered(monkeypatch):
    cutoff, entries = load_config(POLICY)
    assert {e.pr_number for e in entries} == {857, 883, 885, 919, 937, 938, 941, 978, 1144}
    assert cutoff == datetime(2026, 8, 27, 17, 33, 34, tzinfo=UTC)
    pulls = [MergedPull(e.pr_number, datetime(2026, 10, 1, tzinfo=UTC),
                        e.base_sha or "a" * 40, e.head_sha, "historical", "") for e in entries]
    def verify(_repo, _ledger, _number, base, head):
        pull = next(p for p in pulls if p.head_sha == head)
        if pull.number == 938:
            raise GateError(
                f"COULD_NOT_TELL: current PR base {base[:9]} is not an ancestor of head {head[:9]}", 3,
            )
        raise refused(pull)
    monkeypatch.setattr(module, "verify", verify)
    result = audit(Path("unused"), Path("unused"), pulls,
                   head_fetcher=lambda *_: None, allowances=entries)
    assert result.exit_code == 0
    assert result.allowed_historical == 9 and result.covered == 0
    assert all(row.verdict == "ALLOWED_HISTORICAL" and "reason=" in row.detail for row in result.rows)


@pytest.mark.parametrize("change", ["new_pr", "new_head", "new_merge", "different_failure", "fetch_error"])
def test_allowance_cannot_mask_other_failures(monkeypatch, change):
    _, entries = load_config(POLICY)
    entry = entries[0]
    pull = MergedPull(entry.pr_number, datetime(2026, 10, 1, tzinfo=UTC),
                      "a" * 40, entry.head_sha, "historical", "")
    if change == "new_pr":
        pull = replace(pull, number=9999)
    elif change == "new_head":
        pull = replace(pull, head_sha="b" * 40)
    elif change == "new_merge":
        pull = replace(pull, merged_at=entry.reviewed_at + timedelta(days=1))
    def fail(*_):
        if change in {"different_failure", "fetch_error"}:
            raise GateError("COULD_NOT_TELL: malformed evidence or failed fetch", 3)
        raise refused(pull)
    monkeypatch.setattr(module, "verify", fail)
    result = audit(Path("unused"), Path("unused"), [pull],
                   head_fetcher=fail if change == "fetch_error" else lambda *_: None,
                   allowances=entries)
    assert result.exit_code != 0 and result.allowed_historical == 0


def test_ancestry_allowance_requires_exact_base():
    _, entries = load_config(POLICY)
    entry = next(e for e in entries if e.pr_number == 938)
    pull = MergedPull(938, datetime(2026, 10, 1, tzinfo=UTC), "c" * 40, entry.head_sha, "", "")
    error = GateError(
        f"COULD_NOT_TELL: current PR base {pull.base_sha[:9]} is not an ancestor of head {pull.head_sha[:9]}", 3,
    )
    assert not entry.matches(pull, error)
