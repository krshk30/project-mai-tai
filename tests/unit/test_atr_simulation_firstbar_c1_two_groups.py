"""C1 21:29 corrections only; CONTROLLED prices, not actual173 replay."""

import builtins
from dataclasses import replace
from decimal import Decimal

import pytest

from project_mai_tai.atr_simulation.firstbar import FirstBarMode
from project_mai_tai.atr_simulation.firstbar_exits import FirstBarPosition
from project_mai_tai.atr_simulation.firstbar_replay import FIRSTBAR_REPLAY_VERSION, FirstBarReplayEngine
from project_mai_tai.atr_simulation.firstbar_target_rule import (
    EXIT_POLICY_VERSION, TARGET_RULE_SHA256, load_target_rule, target_rule_path,
)
from project_mai_tai.atr_simulation.types import Boundary, Coverage, EntryFact, SimExitReason
from tests.unit.test_atr_simulation_firstbar_exits import SOURCE, NS, key, trade
from tests.unit.test_atr_simulation_firstbar_replay import item, POLICY_MODE, trade as replay_trade


def held(mode, price="4.23", quantity=213):
    value = FirstBarPosition(mode=mode, sell_flip=Boundary(500*NS,SOURCE),
        session_end=Boundary(600*NS,SOURCE), coverage=Coverage(0,600*NS,True,(),(SOURCE,)))
    assert value.add_fill(EntryFact("CONTROLLED:entry",key(0),Decimal(price),quantity,SOURCE))
    return value


@pytest.mark.parametrize("mode",tuple(FirstBarMode))
@pytest.mark.parametrize("entry,nominal,limit,qty,pnl",(
    ("1.56","1.638","1.64",196,"15.68"),
    ("3.61","3.7905","3.80",251,"47.69")))
def test_named_price_geometry_waits_for_live_ceiling_not_reference_fit(mode,entry,nominal,limit,qty,pnl):
    value=held(mode,entry,qty)
    value.on_trade(trade(1,nominal))
    assert value.exit is None
    value.on_trade(trade(2,limit))
    assert value.exit.reason is SimExitReason.TARGET_5
    assert value.exit.price==Decimal(limit)
    assert value.quantity*(value.exit.price-value.vwap)==Decimal(pnl)
    assert value.exit.price_basis=="MODELED_A2_CEILING_TARGET_LIMIT_NOT_NATIVE_FILL"


@pytest.mark.parametrize("mode",tuple(FirstBarMode))
def test_bbci_stop_executes_current_first_eligible_trigger_print_not_threshold(mode):
    value=held(mode)
    value.on_trade(trade(1,"3.90"))
    assert value.exit is None
    value.on_trade(trade(2,"3.87"))
    assert value.exit.reason is SimExitReason.STOP_8
    assert value.exit.at==key(2)
    assert value.exit.price==Decimal("3.87")
    assert value.exit.price!=Decimal("3.8916")
    assert not value.can_fill
    assert not value.add_fill(EntryFact("CONTROLLED:late",key(3),Decimal("4"),100,SOURCE))


@pytest.mark.parametrize("size,conditions",((99,"14"),(100,"999999")))
def test_ineligible_print_cannot_supply_stop_execution(size,conditions):
    value=held(FirstBarMode.FIRSTBAR)
    value.on_trade(trade(1,"3.80",size=size,conditions=conditions))
    assert value.exit is None
    value.on_trade(trade(2,"3.87"))
    assert value.exit.price==Decimal("3.87")
    assert value.exit.at==key(2)


def test_unknown_eligibility_does_not_become_known_stop_outcome():
    value=held(FirstBarMode.FIRSTBAR)
    value.on_trade(replace(trade(1,"3.80"),execution=None))
    assert value.exit is None
    assert not value.can_fill
    value.on_trade(trade(2,"3.87"))
    assert "UNKNOWN_FIRSTBAR_EXIT_EXECUTION_ELIGIBILITY" in value.exit.issues


@pytest.mark.parametrize("mode",tuple(FirstBarMode))
def test_future_rebound_does_not_cancel_an_already_triggered_causal_stop(mode):
    value=held(mode,"3.27",195)
    value.on_trade(trade(1,"2.9501"))
    assert value.exit.price==Decimal("2.9501")
    value.on_trade(trade(2,"3.15"))
    assert value.exit.at==key(1)
    assert value.exit.price==Decimal("2.9501")


@pytest.mark.parametrize("raw,expected",((".52501",".5251"),("1","1.00"),("1.0001","1.01")))
def test_exact_shared_live_tick_and_ceiling_boundaries(raw,expected):
    assert load_target_rule()(Decimal(raw))==Decimal(expected)


def test_partial_vwap_target_and_x3_first_fill_clock_stay_causal():
    value=held(FirstBarMode.FIRSTBAR_X3,"10",100)
    assert value.add_fill(EntryFact("CONTROLLED:partial",key(100),Decimal("12"),100,SOURCE))
    assert value.vwap==Decimal("11")
    value.on_trade(trade(121,"11"))
    assert value.exit.reason is SimExitReason.TIME_NO_1PCT_120S
    assert value.exit.at==key(121)
    assert value.exit.quantity==200


@pytest.mark.parametrize("boundary",("sell","session"))
def test_existing_boundary_precedence_is_not_changed_to_target_or_stop(boundary):
    value=held(FirstBarMode.FIRSTBAR)
    if boundary=="session":
        value.sell_flip=None
    at=500 if boundary=="sell" else 600
    value.on_trade(trade(at,"2"))
    assert value.exit.reason is (SimExitReason.SELL_FLIP if boundary=="sell" else SimExitReason.SESSION_END)
    assert value.exit.price==Decimal("2")


def test_standalone_leaf_never_imports_broker_package(monkeypatch):
    original=builtins.__import__
    def checked(name,*args,**kwargs):
        if name.startswith("project_mai_tai.broker_adapters"):
            raise AssertionError("runtime broker import forbidden")
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,"__import__",checked)
    function=load_target_rule()
    assert function(Decimal("1.638"))==Decimal("1.64")
    assert function.__globals__["__name__"]=="_atrsim_verified_standalone_target_rule"


def test_live_target_bytes_must_match_even_after_a_previous_load(tmp_path):
    load_target_rule()
    copy=tmp_path/"target_price.py"
    copy.write_bytes(target_rule_path().read_bytes()+b"\n")
    with pytest.raises(ValueError,match="live_target_rule_source_mismatch"):
        load_target_rule(copy)


def test_connected_worlds_keep_shared_physical_print_conservation():
    rows=(replay_trade(61),replay_trade(62),replay_trade(63,"2.2"))
    results=FirstBarReplayEngine().simulate_many((item(rows,"one"),item(rows,"two")),fill_policy=POLICY_MODE)
    for mode in FirstBarMode:
        world=[r for r in results if r.mode is mode]
        assert sum(f.quantity for r in world for f in r.fills)==300
        assert {r.identity.account_id:r.position.filled_qty for r in world}=={"one":196,"two":104}
        assert all(r.version==FIRSTBAR_REPLAY_VERSION for r in world)
        assert world[0].exits[0].reason is SimExitReason.TARGET_5
        assert world[1].exits[0].reason is SimExitReason.OPEN
        assert not any("UNKNOWN_SHARED" in issue for r in world for issue in r.issues)
    assert "V2_A2_CEILING_CAUSAL_STOP_PRINT" in FIRSTBAR_REPLAY_VERSION
    assert EXIT_POLICY_VERSION=="C1_20261023_2129_A2_CEILING_FIRST_ELIGIBLE_STOP_PRINT"
    assert len(TARGET_RULE_SHA256)==64
