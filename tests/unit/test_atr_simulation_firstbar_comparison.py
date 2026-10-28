"""New provided-reference-field comparison controls, no real replay claims."""

from dataclasses import replace
from decimal import Decimal

import pytest

from project_mai_tai.atr_simulation.firstbar import FirstBarMode
from project_mai_tai.atr_simulation.firstbar_comparison import compare_firstbar_case
from project_mai_tai.atr_simulation.firstbar_reference import ReferenceSeedCase
from project_mai_tai.atr_simulation.firstbar_replay import FirstBarFillPolicy, FirstBarReplayEngine
from tests.unit.test_atr_simulation_firstbar_replay import item, trade


BASE = 1791802800 * 1_000_000_000  # CONTROLLED 2026-10-12 07:00 ET.


def operands(*, false_flip=False, first_price="2.01", first_size=100):
    value = item((trade(61, first_price, first_size), trade(62), trade(63, "2.2")))
    def boundary(old):
        return replace(old, available_ns=old.available_ns + BASE,
                       bar_start_ns=old.bar_start_ns + BASE if old.bar_start_ns is not None else None)
    episode = value.episode
    episode = replace(episode, identity=replace(episode.identity, trading_day="2026-10-12"),
        buy_flip=boundary(episode.buy_flip), sell_flip=boundary(episode.sell_flip),
        session_end=boundary(episode.session_end),
        tape_coverage=replace(episode.tape_coverage, start_ns=BASE, end_ns=BASE + 300_000_000_000),
        flip_coverage=replace(episode.flip_coverage, start_ns=BASE, end_ns=BASE + 300_000_000_000),
        trades=tuple(replace(row, key=replace(row.key, event_ns=row.key.event_ns + BASE)) for row in episode.trades))
    value = replace(value, episode=episode, entry_cutoff=boundary(value.entry_cutoff),
        flip_bar=replace(value.flip_bar, start_ns=BASE, available_ns=BASE + 60_000_000_000))
    if false_flip:
        value = replace(value, flip_bar=replace(value.flip_bar, close=Decimal("1.97"),
                                               atr_state_at_close="short"))
    result = FirstBarReplayEngine().simulate(value, fill_policy=FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)[0]
    fields = {"qty": "196", "A_px": "2.01", "A_fill_et": "07:01:01",
              "A_exit_et": "07:01:03", "A_exit_px": "2.12", "A_usd": "21.56",
              "A_why": "target", "fbh_skip": ""}
    source = value.flip_bar.source
    case = ReferenceSeedCase(("X", "2026-10-12", "07:00:05"), source, source,
                             tuple(fields.items()), ())
    return case, result


def test_compares_fields_only_with_explicit_binding_and_preserves_uncentized_delta():
    case, result = operands()
    comparison = compare_firstbar_case(case, (result,), mode=FirstBarMode.FIRSTBAR,
                                      bindings=(result.identity,))
    assert comparison.status == "MATCH_COMPARED_FIELDS_ONLY"
    assert not comparison.differences
    assert comparison.reported_pnl_delta == Decimal("0.00")
    assert comparison.scope == "PROVIDED_REFERENCE_FIELDS_NOT_NATIVE_ACCEPTANCE"


def test_successor_ceiling_still_preserves_nonzero_uncentized_delta():
    case, result = operands(first_price="2.0101", first_size=196)
    fields = dict(case.csv_fields, A_px="2.0101", A_usd="21.54")
    case = replace(case, csv_fields=tuple(fields.items()))
    comparison = compare_firstbar_case(case, (result,), mode=FirstBarMode.FIRSTBAR,
                                      bindings=(result.identity,))
    assert result.gross_pnl == Decimal("21.5404")
    assert comparison.status == "MATCH_COMPARED_FIELDS_ONLY"
    assert comparison.reported_pnl_delta == Decimal("0.0004")


def test_quantity_difference_is_per_case_not_hidden_by_equal_provided_pnl():
    case, result = operands()
    fields = dict(case.csv_fields)
    fields["qty"] = "197"
    case = replace(case, csv_fields=tuple(fields.items()))
    comparison = compare_firstbar_case(case, (result,), mode=FirstBarMode.FIRSTBAR,
                                      bindings=(result.identity,))
    assert comparison.status == "DIFFERENT"
    assert [difference.field for difference in comparison.differences] == ["filled_quantity"]
    assert comparison.differences[0].reference_source == case.csv_source


def test_uncertain_economics_not_reported_as_match_or_zero():
    case, result = operands()
    result = replace(result, decision="UNKNOWN", issues=("UNKNOWN_SHARED_FIRSTBAR_ALLOCATION",), gross_pnl=None)
    comparison = compare_firstbar_case(case, (result,), mode=FirstBarMode.FIRSTBAR,
                                      bindings=(result.identity,))
    assert comparison.status == "UNKNOWN"
    assert comparison.reported_pnl_delta is None
    assert "UNKNOWN_SHARED_FIRSTBAR_ALLOCATION" in comparison.issues


def test_missing_second_account_binding_is_not_implicitly_deduplicated():
    case, result = operands()
    second = replace(result.identity, account_id="two", broker="schwab")
    with pytest.raises(ValueError, match="all_and_only_bound_reference_accounts_required"):
        compare_firstbar_case(case, (result,), mode=FirstBarMode.FIRSTBAR,
                              bindings=(result.identity, second))


def test_affirmative_model_skip_reports_known_difference_not_unknown_or_fake_fill():
    case, result = operands(false_flip=True)
    assert result.is_skipped and not result.is_trade
    comparison = compare_firstbar_case(case, (result,), mode=FirstBarMode.FIRSTBAR,
                                      bindings=(result.identity,))
    assert comparison.status == "DIFFERENT"
    assert comparison.reported_pnl_delta == Decimal("-21.56")
    assert not comparison.issues

