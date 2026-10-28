"""Compare explicit episode bindings with provided study fields, not native facts."""

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .firstbar import FirstBarMode
from .firstbar_reference import ReferenceSeedCase
from .firstbar_replay import FIRSTBAR_REPLAY_VERSION, FirstBarReplayResult
from .types import FlipIdentity, SimExitReason, SourceRef


@dataclass(frozen=True)
class ReferenceFieldDifference:
    field: str
    reference_value: object
    engine_value: object
    reference_source: SourceRef


@dataclass(frozen=True)
class FirstBarCaseComparison:
    reference_key: tuple[str, str, str]
    mode: FirstBarMode
    status: str
    bindings: tuple[FlipIdentity, ...]
    differences: tuple[ReferenceFieldDifference, ...]
    issues: tuple[str, ...]
    reported_pnl_delta: Decimal | None
    reference_reason: str | None
    engine_reasons: tuple[SimExitReason, ...]
    scope: str = "PROVIDED_REFERENCE_FIELDS_NOT_NATIVE_ACCEPTANCE"


def _clock(day: str, clock: str) -> int:
    whole, dot, fraction = clock.partition(".")
    if dot and (not fraction.isascii() or not fraction.isdigit() or len(fraction) > 9):
        raise ValueError("reference_nanosecond_clock_required")
    local_time = time.fromisoformat(whole)
    if local_time.tzinfo is not None:
        raise ValueError("plain_reference_et_clock_required")
    local = datetime.combine(date.fromisoformat(day), local_time, ZoneInfo("America/New_York"))
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        raise ValueError("ambiguous_reference_clock_requires_explicit_source")
    delta = local.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int(fraction.ljust(9, "0") or "0")


def compare_firstbar_case(case: ReferenceSeedCase, results: tuple[FirstBarReplayResult, ...], *,
                          mode: FirstBarMode, bindings: tuple[FlipIdentity, ...]
                          ) -> FirstBarCaseComparison:
    """Compare one already-bound seed; never infer broker identity from a group.

    Callers must provide ALL account legs for this seed and exactly this mode.
    Independent calls/reference groups are not additive native-economic proof.
    """
    if not isinstance(mode, FirstBarMode) or not bindings or len(set(bindings)) != len(bindings):
        raise ValueError("explicit_unique_reference_episode_bindings_required")
    if (len(results) != len(bindings) or {result.identity for result in results} != set(bindings)
            or len({result.identity for result in results}) != len(results)):
        raise ValueError("all_and_only_bound_reference_accounts_required")
    if any(result.mode is not mode or result.version != FIRSTBAR_REPLAY_VERSION for result in results):
        raise ValueError("reference_mode_or_engine_contract_mismatch")
    symbol, day, _ = case.reference_key
    if any(identity.symbol != symbol or identity.trading_day != day for identity in bindings):
        raise ValueError("reference_case_day_symbol_binding_mismatch")
    csv_row, x3 = dict(case.csv_fields), dict(case.x3_fields)
    if mode is FirstBarMode.FIRSTBAR:
        source = case.csv_source
        expected = dict(quantity=Decimal(csv_row["qty"]), price=csv_row.get("A_px"),
                        entry=csv_row.get("A_fill_et"), exit=csv_row.get("A_exit_et"),
                        exit_price=csv_row.get("A_exit_px"), pnl=csv_row.get("A_usd"),
                        reason=csv_row.get("A_why"), skip=csv_row.get("fbh_skip"))
    else:
        source = case.x3_source
        expected = dict(quantity=Decimal(str(x3["qty"])), price=x3.get("fbh_px"),
                        entry=x3.get("fbh_fill_et"), exit=x3.get("fbh_exit_et"),
                        exit_price=x3.get("fbh_exit_px"), pnl=x3.get("fbh_usd"),
                        reason=x3.get("fbh_why_exit"), skip=x3.get("fbh_why"))
    differences, issues = [], []
    for result in results:
        issues.extend(result.issues)
        if result.decision == "UNKNOWN" or (result.is_trade and result.gross_pnl is None):
            issues.append("UNKNOWN_ENGINE_CASE_ECONOMICS")

    def difference(field, reference_value, engine_value):
        if reference_value != engine_value:
            differences.append(ReferenceFieldDifference(field, reference_value, engine_value, source))

    expected_skip = bool(expected["skip"]) and expected["price"] in (None, "")
    difference("is_trade", not expected_skip, any(result.is_trade for result in results))
    reasons = tuple(exit.reason for result in results for exit in result.exits)
    delta = None
    if expected_skip:
        if any(not result.is_skipped for result in results):
            difference("affirmative_skip", True, all(result.is_skipped for result in results))
    else:
        required = ("price", "entry", "exit", "exit_price", "pnl", "reason")
        if any(expected[name] in (None, "") for name in required):
            issues.append("UNKNOWN_REFERENCE_MODEL_FIELDS")
        else:
            fills = tuple(fill for result in results for fill in result.fills)
            exits = tuple(exit for result in results for exit in result.exits)
            quantity = sum(fill.quantity for fill in fills)
            difference("filled_quantity", expected["quantity"], quantity)
            entry_price = sum((fill.price * fill.quantity for fill in fills), Decimal(0)) / quantity if quantity else None
            difference("entry_vwap", Decimal(str(expected["price"])), entry_price)
            difference("first_fill_ns", _clock(day, expected["entry"]),
                       min((fill.at.event_ns for fill in fills), default=None))
            closed = bool(exits) and all(exit.at is not None and exit.price is not None for exit in exits)
            exit_quantity = sum(exit.quantity for exit in exits) if closed else 0
            if closed and exit_quantity != quantity:
                raise ValueError("comparison_exit_quantity_conservation_required")
            exit_price = (sum((exit.price * exit.quantity for exit in exits), Decimal(0)) / exit_quantity
                          if closed and exit_quantity else None)
            difference("exit_vwap", Decimal(str(expected["exit_price"])), exit_price)
            difference("last_exit_ns", _clock(day, expected["exit"]),
                       max((exit.at.event_ns for exit in exits), default=None) if closed else None)
            families = {"target": {SimExitReason.TARGET_5}, "stop": {SimExitReason.STOP_8},
                        "x3_time": {SimExitReason.TIME_NO_1PCT_120S},
                        "lineclose": {SimExitReason.SELL_FLIP, SimExitReason.SAME_AS_LIVE},
                        "eod": {SimExitReason.SESSION_END}}
            if expected["reason"] not in families:
                issues.append("UNKNOWN_REFERENCE_EXIT_REASON_FAMILY")
            elif not reasons or any(reason not in families[expected["reason"]] for reason in reasons):
                difference("exit_reason_family", expected["reason"], reasons)
            if all(result.gross_pnl is not None or (result.is_skipped and not result.is_trade
                       and not result.issues) for result in results):
                # Only an affirmative, evidence-complete no-trade has zero
                # exposure. UNKNOWN/open outcomes never enter this branch.
                modeled = sum((result.gross_pnl if result.gross_pnl is not None else Decimal(0)
                               for result in results), Decimal(0))
                provided = Decimal(str(expected["pnl"]))
                delta = modeled - provided
                difference("reported_cent_pnl", provided, modeled.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            else:
                issues.append("UNKNOWN_ENGINE_CASE_ECONOMICS")
    issues = tuple(dict.fromkeys(issues))
    status = "UNKNOWN" if issues else ("DIFFERENT" if differences else "MATCH_COMPARED_FIELDS_ONLY")
    return FirstBarCaseComparison(case.reference_key, mode, status, bindings,
                                  tuple(differences), issues, delta, expected["reason"], reasons)

