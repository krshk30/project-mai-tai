"""Track-2 Phase-2 Slice-1 — v2 position-state plumbing (oms_managed_positions).

Isolation-safe: creates ONLY the oms_managed_positions table on SQLite (NOT
Base.metadata.create_all — the codebase's market_trade_ticks JSONB column can't
render on SQLite). The load-bearing property proven here is SINGLE-WRITER + DORMANT:
- flag OFF → zero rows, zero behavior (dormant);
- flag ON → the OMS (and only the OMS path) creates/updates/closes the row from v2
  fills, with the ladder state derived from a fresh `exit_logic.Position`;
- non-v2 strategies are never managed.
The service gating is tested by calling `OmsRiskService._apply_managed_position_after_fill`
against a minimal harness (it uses only `self.settings` + `self.store`).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import OmsManagedPosition
from project_mai_tai.exit_logic.config import TradingConfig
from project_mai_tai.exit_logic.position import Position
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings

ACCT = "paper:schwab_1m_v2"


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    # ONLY our table — avoid the market_trade_ticks JSONB-on-SQLite landmine.
    OmsManagedPosition.__table__.create(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class _Harness:
    """Minimal stand-in exposing the two attributes the hook uses."""
    def __init__(self, *, enabled: bool) -> None:
        self.settings = Settings(oms_v2_exit_management_enabled=enabled)
        self.store = OmsStore()
        self._managed_v2_symbols: set[tuple[str, str]] = set()  # slice-3: the hook maintains this set


def _apply(harness: _Harness, session: Session, **kw):
    # default a v2 open BUY fill; callers override
    params = dict(
        strategy_code="schwab_1m_v2", broker_account_name=ACCT, symbol="VSME",
        side="buy", intent_type="open", quantity=Decimal("10"), price=Decimal("2.50"),
        metadata={"path": "MACD Cross"},
    )
    params.update(kw)
    OmsRiskService._apply_managed_position_after_fill(harness, session=session, **params)


def _rows(session: Session) -> list[OmsManagedPosition]:
    return list(session.scalars(select(OmsManagedPosition)).all())


# --------------------------------------------------------------------------- (1)

def test_make_v2_variant_reproduces_rescore_ladder() -> None:
    """make_v2_variant = the validated re-score ladder (1.5% stop, base scale/floor,
    qty 10) — and DIVERGES from make_1m_variant's 1.0% stop (the §7-a reason)."""
    v2 = TradingConfig().make_v2_variant()
    assert v2.stop_loss_pct == 1.5
    assert v2.default_quantity == 10
    assert v2.bar_interval_secs == 60
    # base scale ladder
    assert (v2.scale_normal2_pct, v2.scale_normal2_sell_pct) == (2.0, 50.0)
    assert (v2.scale_fast4_pct, v2.scale_fast4_sell_pct) == (4.0, 75.0)
    assert (v2.scale_4after2_pct, v2.scale_4after2_sell_pct) == (4.0, 25.0)
    # base floor ladder
    assert v2.profit_floor_lock_at_1pct_peak_pct == 0.0
    assert v2.profit_floor_lock_at_2pct_peak_pct == 0.5
    assert v2.profit_floor_lock_at_3pct_peak_pct == 1.5
    assert v2.profit_floor_trail_buffer_over_4pct_pct == 1.5
    # the divergence guard
    assert TradingConfig().make_1m_variant().stop_loss_pct == 1.0
    assert v2.stop_loss_pct != TradingConfig().make_1m_variant().stop_loss_pct


# --------------------------------------------------------------------------- (2)

def test_open_fill_creates_managed_row_when_enabled() -> None:
    h = _Harness(enabled=True)
    with _session_factory()() as s:
        _apply(h, s, symbol="VSME", quantity=Decimal("10"), price=Decimal("2.5000"),
               metadata={"path": "ATR Flip"})
        s.commit()
        rows = _rows(s)
        assert len(rows) == 1
        r = rows[0]
        assert r.strategy_code == "schwab_1m_v2" and r.broker_account_name == ACCT
        assert r.symbol == "VSME" and r.status == "open"
        assert Decimal(str(r.entry_price)) == Decimal("2.5000")
        assert r.original_quantity == 10 and r.current_quantity == 10
        assert r.entry_path == "ATR Flip" and r.config_name == "make_v2_variant"
        # fresh ladder state
        assert r.tier == 1 and r.floor_pct is None and r.floor_price is None
        assert Decimal(str(r.peak_profit_pct)) == Decimal("0")


# --------------------------------------------------------------------------- (3)

def test_dormant_when_flag_off() -> None:
    h = _Harness(enabled=False)
    with _session_factory()() as s:
        _apply(h, s)  # v2 open buy
        s.commit()
        assert _rows(s) == []  # zero rows — fully dormant


# --------------------------------------------------------------------------- (4)

def test_non_v2_strategy_never_managed() -> None:
    h = _Harness(enabled=True)
    with _session_factory()() as s:
        _apply(h, s, strategy_code="schwab_1m", symbol="AAA")
        _apply(h, s, strategy_code="macd_30s", symbol="BBB")
        s.commit()
        assert _rows(s) == []  # only schwab_1m_v2 is managed


# --------------------------------------------------------------------------- (5)

def test_open_fill_is_idempotent_one_row_per_symbol() -> None:
    h = _Harness(enabled=True)
    with _session_factory()() as s:
        _apply(h, s, symbol="VSME")
        _apply(h, s, symbol="VSME")  # second open — must not create a 2nd row
        s.commit()
        assert len(_rows(s)) == 1


# --------------------------------------------------------------------------- (6)

def test_position_state_persists_from_hydrated_position() -> None:
    """update_managed_position_from_position writes ladder state from a real
    exit_logic.Position; the -999 floor sentinel maps to NULL, set floor persists."""
    store = OmsStore()
    cfg = TradingConfig().make_v2_variant()
    with _session_factory()() as s:
        row = store.create_managed_position(
            s, strategy_code="schwab_1m_v2", broker_account_name=ACCT, symbol="VSME",
            entry_price=Decimal("10.0"), quantity=10, entry_path="MACD Cross",
        )
        # fresh position: no floor yet → NULL
        p = Position("VSME", 10.0, 10, entry_time="2026-01-01",
                     floor_lock_at_1pct_peak_pct=cfg.profit_floor_lock_at_1pct_peak_pct,
                     floor_lock_at_2pct_peak_pct=cfg.profit_floor_lock_at_2pct_peak_pct,
                     floor_lock_at_3pct_peak_pct=cfg.profit_floor_lock_at_3pct_peak_pct,
                     floor_trail_buffer_over_4pct_pct=cfg.profit_floor_trail_buffer_over_4pct_pct)
        # +2.5% (safely inside the 2% floor band — 10.20 floats to 1.9999% and
        # would miss it): tier 2, floor locks at 0.5% → floor_price 10.05.
        p.update_price(10.25)
        store.update_managed_position_from_position(s, row, p)
        s.commit()
        r = _rows(s)[0]
        assert r.tier == 2
        assert abs(Decimal(str(r.peak_profit_pct)) - Decimal("2.5")) < Decimal("0.001")
        assert r.floor_pct is not None and abs(Decimal(str(r.floor_pct)) - Decimal("0.5")) < Decimal("0.001")
        assert r.floor_price is not None and abs(Decimal(str(r.floor_price)) - Decimal("10.05")) < Decimal("0.001")


# --------------------------------------------------------------------------- (7)

def test_external_sell_flatten_closes_row() -> None:
    h = _Harness(enabled=True)
    with _session_factory()() as s:
        _apply(h, s, symbol="VSME", quantity=Decimal("10"))  # open
        _apply(h, s, symbol="VSME", side="sell", intent_type="close",
               quantity=Decimal("10"))  # full flatten
        s.commit()
        r = _rows(s)[0]
        assert r.status == "closed" and r.current_quantity == 0


def test_partial_sell_decrements_keeps_open() -> None:
    h = _Harness(enabled=True)
    with _session_factory()() as s:
        _apply(h, s, symbol="VSME", quantity=Decimal("10"))
        _apply(h, s, symbol="VSME", side="sell", intent_type="scale", quantity=Decimal("4"))
        s.commit()
        r = _rows(s)[0]
        assert r.status == "open" and r.current_quantity == 6
