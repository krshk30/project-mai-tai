"""R4 — the refusal model, pinned against the REAL reject strings.

⛔ The engine's implicit broker model is "orders fill." These are the verbatim reasons the broker
actually returned over six sessions (2026-08-10 → 08-17, `live:schwab_1m_v2`, 82 rejected entry
orders, 82 of them carrying a reason).

⛔⭐ The four classes are modelled SEPARATELY and must never be collapsed into one refusal rate:
"not electronically tradeable" removes the trade entirely and is a property of the SYMBOL, while
"trigger not above ask" is a submit-time reject that depends on where the tape was.
"""
from __future__ import annotations

import inspect
from datetime import datetime

from project_mai_tai.backtest import broker_refusal as mod
from project_mai_tai.backtest.broker_refusal import (
    RefusalClass,
    build_refusal_model,
    classify_refusal,
    header,
)

# Verbatim, from Schwab's own book / our stored reject_reason.
R_NOT_TRADEABLE = "Opening transactions for this security must be placed with a broker. Contact us"
R_NOT_ELIGIBLE = (
    "Your order is not eligible for electronic entry. Please call a Charles Schwab "
    "representative at (800) 435-9050 for assistance with this trade."
)
R_TRIGGER = (
    "The stop price must be above the current ask for buy stop orders and below the bid for "
    "sell stop orders."
)
R_BUYING_POWER = "You do not have enough available cash/buying power for this order."
R_TIMEOUT = "The read operation timed out"
R_DNS = "{'fault': {'faultstring': 'Unable to resolve host traderapi-accounts.schwab.com'}}"


def test_each_observed_reason_classifies() -> None:
    assert classify_refusal(R_NOT_TRADEABLE) is RefusalClass.NOT_ELECTRONICALLY_TRADEABLE
    assert classify_refusal(R_NOT_ELIGIBLE) is RefusalClass.NOT_ELECTRONICALLY_TRADEABLE
    assert classify_refusal(R_TRIGGER) is RefusalClass.TRIGGER_NOT_ABOVE_ASK
    assert classify_refusal(R_BUYING_POWER) is RefusalClass.INSUFFICIENT_BUYING_POWER
    assert classify_refusal(R_TIMEOUT) is RefusalClass.CLIENT_ABORT
    assert classify_refusal(R_DNS) is RefusalClass.CLIENT_ABORT


def test_an_UNRECOGNISED_reason_returns_None_and_is_NOT_guessed() -> None:
    """⛔ Folding an unknown reason into the nearest class is how a taxonomy silently stops matching
    the broker. It must surface as UNKNOWN."""
    assert classify_refusal("Some entirely new Schwab wording nobody has seen") is None
    assert classify_refusal(None) is None
    assert classify_refusal("") is None


def test_a_COMBINED_reason_resolves_to_the_symbol_level_class() -> None:
    """Schwab pipe-joins messages. A name that is not electronically tradeable is untradeable
    regardless of what else was wrong with the order, so that class must win."""
    combined = f"{R_TRIGGER}|{R_NOT_TRADEABLE}"
    assert classify_refusal(combined) is RefusalClass.NOT_ELECTRONICALLY_TRADEABLE


def test_only_the_symbol_level_class_produces_a_REFUSED_SYMBOL() -> None:
    """⛔⭐ THE ONE THAT CHANGES RESULTS MOST. A name Schwab will not accept must be excluded
    entirely — otherwise the engine books P&L from a trade that could never have happened.
    A trigger reject is NOT a property of the symbol and must not exclude it."""
    m = build_refusal_model([
        ("BANL", R_NOT_TRADEABLE),
        ("INHD", R_NOT_ELIGIBLE),
        ("CRWU", R_TRIGGER),          # <- tradeable; just rejected at that instant
        ("XHLD", R_BUYING_POWER),
        ("IVF", R_TIMEOUT),
    ])
    assert m.refused_symbols == frozenset({"BANL", "INHD"})
    assert m.is_refused("banl") and m.is_refused("INHD")
    assert not m.is_refused("CRWU"), "a trigger reject must not make the symbol untradeable"
    assert not m.is_refused("IVF"), "our own abort says nothing about the symbol"


def test_the_four_classes_are_counted_SEPARATELY() -> None:
    """⛔ Never one blended refusal rate — they behave differently."""
    m = build_refusal_model([
        ("A", R_NOT_TRADEABLE), ("B", R_NOT_ELIGIBLE),
        ("C", R_TRIGGER), ("D", R_TRIGGER),
        ("E", R_BUYING_POWER),
        ("F", R_TIMEOUT), ("G", R_DNS),
    ])
    assert m.counts[RefusalClass.NOT_ELECTRONICALLY_TRADEABLE] == 2
    assert m.counts[RefusalClass.TRIGGER_NOT_ABOVE_ASK] == 2
    assert m.counts[RefusalClass.INSUFFICIENT_BUYING_POWER] == 1
    assert m.counts[RefusalClass.CLIENT_ABORT] == 2


def test_unclassified_reasons_are_SURFACED_not_swallowed() -> None:
    """⭐ A non-zero unclassified count means the broker changed its wording — a signal, not noise."""
    m = build_refusal_model([("X", "brand new wording"), ("Y", R_TRIGGER)])
    assert m.unclassified and "brand new wording" in m.unclassified[0]
    assert m.counts[RefusalClass.TRIGGER_NOT_ABOVE_ASK] == 1


def test_the_header_states_population_window_and_account_BEFORE_any_number() -> None:
    """R7 + R9 — no count without a denominator, and name the population first."""
    m = build_refusal_model([("A", R_NOT_TRADEABLE), ("C", R_TRIGGER)])
    h = header(m, account="live:schwab_1m_v2",
               start=datetime(2026, 8, 10), end=datetime(2026, 8, 18))
    assert "account=live:schwab_1m_v2" in h
    assert "window=2026-08-10..2026-08-18" in h
    assert "refused_symbols=1" in h


def test_there_is_NO_hand_edit_or_override_hook() -> None:
    """⛔⭐ THE LINE THAT KEEPS THIS EXECUTION, NOT STRATEGY. The list is derived from the broker's
    own reject reasons and takes no curated input. If anyone ever needs to add a symbol manually,
    that is the signal it has become strategy — so there must be nowhere to put it."""
    src = inspect.getsource(mod)
    for forbidden in ("MANUAL_", "OVERRIDE", "EXTRA_REFUSED", "ALWAYS_REFUSE", "WHITELIST"):
        assert forbidden not in src, f"an override hook ({forbidden}) would let this become strategy"
    sig = inspect.signature(build_refusal_model)
    assert list(sig.parameters) == ["rows"], "the model takes derived rows only, nothing curated"


def test_the_model_is_PURE_no_db_no_network() -> None:
    """Offline-derivable on purpose: a replay must be reproducible and fixture-runnable."""
    src = inspect.getsource(mod)
    for banned in ("psycopg", "requests", "urllib", "httpx", "session.execute("):
        assert banned not in src, f"{banned} in the model would make a replay non-reproducible"
