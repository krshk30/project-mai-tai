from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from project_mai_tai.services.scanner_confirmed_capture import capture_events
from project_mai_tai.settings import Settings

EASTERN = ZoneInfo("America/New_York")


class _FakeSessionFactory:
    """Callable returning a context manager wrapping a Mock session.

    Records the params dict of every ``session.execute(sql, params)`` call.
    """

    def __init__(self) -> None:
        self.session = MagicMock()
        self.commits = 0
        self.executed_params: list[dict] = []

        def _execute(_sql, params=None):
            self.executed_params.append(params)
            result = MagicMock()          # the reconfirm_seq count query calls .scalar()
            result.scalar.return_value = 0
            return result

        self.session.execute.side_effect = _execute
        self.session.commit.side_effect = self._commit

    def _commit(self) -> None:
        self.commits += 1

    @contextmanager
    def _ctx(self):
        yield self.session

    def __call__(self):
        return self._ctx()


def _sample_confirmed() -> list[dict]:
    return [
        {
            "ticker": "AAAA",
            "confirmed_at": "09:46:52 AM ET",
            "confirmation_path": "PATH_C_EXTREME_MOVER",
            "force_watchlist": True,
            "rank_score": 87.5,
            "price": 3.21,
            "volume": 1_250_000,
            "shares_outstanding": 8_000_000,
            "change_pct": 42.3,
        },
        {
            "ticker": "bbbb",
            "confirmed_at": "10:01:00 AM ET",
            "confirmation_path": "PATH_B_TWO_SQUEEZE",
            "force_watchlist": False,
            "rank_score": 55.0,
            "price": 1.05,
            "volume": 600_000,
            "shares_outstanding": 12_000_000,
            "change_pct": 18.0,
        },
    ]


def test_capture_events_writes_confirm_fade_retention_rows():
    factory = _FakeSessionFactory()
    now = datetime(2026, 7, 10, 10, 5, 0, tzinfo=EASTERN)

    capture_events(
        factory,
        trade_date=now.date(),
        now=now,
        all_confirmed=_sample_confirmed(),
        faded_symbols=["cccc"],
        dropped_retention_symbols=["DDDD"],
    )

    # reconfirm_seq is computed via a separate count query per CONFIRM, so filter to INSERT params
    # (the count-query params have no "event_type").
    inserts = [p for p in factory.executed_params if p and "event_type" in p]
    assert len(inserts) == 4
    assert factory.commits == 1
    assert all("reconfirm_seq" in p for p in inserts)

    by_symbol = {p["symbol"]: p for p in inserts}

    aaaa = by_symbol["AAAA"]
    assert aaaa["event_type"] == "CONFIRM"
    assert aaaa["confirm_path"] == "PATH_C_EXTREME_MOVER"
    assert aaaa["force_watchlist"] is True
    assert aaaa["float_used"] == 8_000_000
    assert aaaa["rank_score"] == Decimal("87.5")
    assert aaaa["day_volume"] == 1_250_000
    # confirmed_at parsed onto trade_date in ET
    assert aaaa["event_at"] == datetime(2026, 7, 10, 9, 46, 52, tzinfo=EASTERN)

    bbbb = by_symbol["BBBB"]  # symbol upper-cased
    assert bbbb["event_type"] == "CONFIRM"
    assert bbbb["confirm_path"] == "PATH_B_TWO_SQUEEZE"
    assert bbbb["force_watchlist"] is False
    assert bbbb["float_used"] == 12_000_000
    assert bbbb["rank_score"] == Decimal("55.0")

    faded = by_symbol["CCCC"]
    assert faded["event_type"] == "FADE"
    assert faded["event_at"] == now
    assert faded["confirm_path"] is None
    assert faded["float_used"] is None

    dropped = by_symbol["DDDD"]
    assert dropped["event_type"] == "RETENTION_DROP"
    assert dropped["event_at"] == now
    assert dropped["rank_score"] is None


def test_capture_events_none_session_factory_is_noop():
    # Must not raise and must not attempt any DB work.
    capture_events(
        None,
        trade_date=datetime(2026, 7, 10, tzinfo=EASTERN).date(),
        now=datetime(2026, 7, 10, 10, 0, tzinfo=EASTERN),
        all_confirmed=_sample_confirmed(),
        faded_symbols=["X"],
        dropped_retention_symbols=["Y"],
    )


def test_capture_events_unparseable_confirmed_at_falls_back_to_now():
    factory = _FakeSessionFactory()
    now = datetime(2026, 7, 10, 10, 5, 0, tzinfo=EASTERN)
    capture_events(
        factory,
        trade_date=now.date(),
        now=now,
        all_confirmed=[{"ticker": "ZZZZ", "confirmed_at": "", "rank_score": 1.0}],
        faded_symbols=[],
        dropped_retention_symbols=[],
    )
    inserts = [p for p in factory.executed_params if p and "event_type" in p]
    assert len(inserts) == 1
    assert inserts[0]["event_at"] == now


def test_capture_events_swallows_db_errors():
    # A failing session must not propagate (the scan loop must never break).
    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")

    capture_events(
        _Boom(),
        trade_date=datetime(2026, 7, 10, tzinfo=EASTERN).date(),
        now=datetime(2026, 7, 10, 10, 0, tzinfo=EASTERN),
        all_confirmed=_sample_confirmed(),
        faded_symbols=[],
        dropped_retention_symbols=[],
    )


def test_scanner_confirmed_capture_flag_defaults_off():
    settings = Settings()
    assert settings.scanner_confirmed_capture_enabled is False


def test_insert_sql_has_no_reused_param_subquery():
    # Regression guard: reconfirm_seq must NOT be a scalar subquery in the INSERT VALUES. Reusing
    # :trade_date/:symbol in both VALUES and a subquery made Postgres deduce inconsistent param
    # types (text vs varchar -> AmbiguousParameter), which the mock-based tests could not catch.
    from project_mai_tai.services.scanner_confirmed_capture import _INSERT_SQL

    sql = str(_INSERT_SQL).lower()
    assert "select count" not in sql
    assert ":reconfirm_seq" in sql
