"""Q check fallback plus C6 original-identity regressions, now using paged LIST."""
from types import SimpleNamespace

import pytest

from tests.unit.test_killsw1_atr_stop import book as book, drive
from tests.unit.test_oms_webull_late_close_guard import _drive, _service
from tests.unit.test_q_c6_original_list import original_case, original_regression, waiting_incident


def test_software_check_exception_retains_actual_legacy_exit_attempts(caplog):
    class BrokenCheckRow(SimpleNamespace):
        @property
        def strategy_code(self):
            raise OSError("controlled new software-check failure")

    service = _service(enabled=False)
    service.row = BrokenCheckRow(**vars(service.row))
    assert _drive(service) == "refused"  # Controlled broker rejection, not check refusal.
    assert _drive(service) == "refused"
    assert service.submits == 2
    assert service.fetches == 0 and not service.resolved
    assert sum("WBATTACH1-SOFTWARE-CHECK-UNKNOWN" in message for message in caplog.messages) == 2
    assert "controlled new software-check failure" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("hours", [8, 11, 14, 21])
async def test_killsw1_unknown_attach_releases_and_reads_complete_children_before_sell(book, monkeypatch, hours):
    await original_regression(book, monkeypatch, "terminal")


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("hours", [8, 11, 14, 21])
async def test_killsw1_unknown_attach_unreadable_inventory_has_no_sell_and_incident(book, monkeypatch, hours):
    state = original_case(book, monkeypatch)
    state.unreadable = True
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    identity = waiting_incident(book)
    state.advance()
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    assert waiting_incident(book) == identity
    assert [kind for kind, _ in state.trace].count("cancel") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("evidence", ["terminal", "missing-T", "missing-S", "unreadable-T", "unreadable-S"])
@pytest.mark.parametrize("hours", [8, 11, 14, 21])
async def test_killsw1_reconciles_exact_journal_original_pair_not_entry_canonical_children(
        book, monkeypatch, evidence, hours):
    # The 76 raw failures remain retained. TOP14:1x replaces DETAIL-shaped
    # instrumentation with the actual full-paged LIST proof, not weaker safety.
    await original_regression(book, monkeypatch, evidence)
