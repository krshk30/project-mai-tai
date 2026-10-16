"""Original journal controls must detect removal of each critical boundary."""

import pytest

from project_mai_tai.db.models import BotControlAttempt, BrokerOrder
from project_mai_tai.oms.bot_control_journal import CleanupJournal, _view
from tests.unit.test_killsw1_journal import (
    journal as journal,
    test_native_parent_identity_replay_and_changed_broker_id_refusal as parent_control,
    test_native_unknown_never_terminal_or_replays_cancel as unknown_control,
    test_prepared_identity_survives_restart_without_replaying_dispatch as replay_control,
)


def test_original_parent_control_detects_removed_native_binding_check(journal, monkeypatch):
    monkeypatch.setattr(
        CleanupJournal,
        "_native_parent",
        staticmethod(lambda unit, _lease, row: unit.get(BrokerOrder, row.entry_order_id)),
    )
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        parent_control(journal)


def test_original_replay_control_detects_repeated_uncertain_dispatch(journal, monkeypatch):
    original = CleanupJournal.dispatch

    def repeated(self, lease, identity, *, now):
        with self.store._sessions() as unit:
            row = unit.get(BotControlAttempt, identity)
            if row.status == "MAY_HAVE_SENT":
                return _view(row)
        return original(self, lease, identity, now=now)

    monkeypatch.setattr(CleanupJournal, "dispatch", repeated)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        replay_control(journal)


def test_original_unknown_control_detects_treating_unknown_as_terminal(journal, monkeypatch):
    original = CleanupJournal.record_native_outcome

    def false_terminal(self, lease, identity, *, outcome, now):
        return original(self, lease, identity, outcome="released", now=now)

    monkeypatch.setattr(CleanupJournal, "record_native_outcome", false_terminal)
    with pytest.raises(AssertionError):
        unknown_control(journal, "unanswerable")
