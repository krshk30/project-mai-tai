from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping


SESSION_MARKER_CONDITION_CODES = frozenset({12})


@dataclass(frozen=True)
class ConditionRule:
    condition_id: int
    name: str
    updates_high_low: bool
    updates_open_close: bool
    updates_volume: bool
    raw: dict[str, Any]

    @property
    def updates_consolidated_ohlc(self) -> bool:
        return self.updates_high_low or self.updates_open_close


@dataclass(frozen=True)
class ConditionSnapshot:
    retrieved_at: datetime
    rules: dict[int, ConditionRule]

    @property
    def version(self) -> str:
        return self.retrieved_at.isoformat()

    def classify(self, condition_codes: Iterable[int]) -> tuple[bool, str]:
        return classify_condition_codes(self.rules, condition_codes)

    def payload(self) -> dict[str, Any]:
        return {
            "retrieved_at": self.retrieved_at.isoformat(),
            "rules": {str(code): asdict(rule) for code, rule in sorted(self.rules.items())},
        }


def classify_condition_codes(
    rules: Mapping[int, ConditionRule],
    condition_codes: Iterable[int],
    *,
    neutral_codes: frozenset[int] = SESSION_MARKER_CONDITION_CODES,
) -> tuple[bool, str]:
    codes = tuple(int(code) for code in condition_codes)
    if not codes:
        return True, "no_conditions"
    classified_codes = tuple(code for code in codes if code not in neutral_codes)
    unknown = [code for code in classified_codes if code not in rules]
    if unknown:
        return False, f"unknown_conditions={','.join(str(code) for code in unknown)}"
    excluded = [code for code in classified_codes if not rules[code].updates_consolidated_ohlc]
    if excluded:
        return False, f"not_consolidated_ohlc={','.join(str(code) for code in excluded)}"
    if len(classified_codes) != len(codes):
        return True, "aggregate_eligible_extended_hours"
    return True, "aggregate_eligible"


def _value(source: object, name: str, default: object = None) -> object:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _plain(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _plain(model_dump())
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return {str(key): _plain(item) for key, item in raw.items() if not str(key).startswith("_")}
    return str(value)


def build_condition_snapshot(
    rows: Iterable[object], *, retrieved_at: datetime
) -> ConditionSnapshot:
    rules: dict[int, ConditionRule] = {}
    for row in rows:
        condition_id = _value(row, "id")
        if condition_id is None:
            condition_id = _value(row, "condition_id")
        if condition_id is None:
            continue
        update_rules = _value(row, "update_rules", {}) or {}
        consolidated = _value(update_rules, "consolidated", {}) or {}
        rule = ConditionRule(
            condition_id=int(condition_id),
            name=str(_value(row, "name", "")),
            updates_high_low=bool(_value(consolidated, "updates_high_low", False)),
            updates_open_close=bool(_value(consolidated, "updates_open_close", False)),
            updates_volume=bool(_value(consolidated, "updates_volume", False)),
            raw=dict(_plain(row) or {}),
        )
        rules[rule.condition_id] = rule
    if not rules:
        raise RuntimeError("Massive returned no trade-condition metadata; refusing raw prints")
    return ConditionSnapshot(retrieved_at=retrieved_at, rules=rules)


def condition_snapshot_from_payload(payload: Mapping[str, object]) -> ConditionSnapshot:
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, Mapping):
        raise RuntimeError("persisted condition snapshot has no rules")
    rules: dict[int, ConditionRule] = {}
    for raw_code, value in raw_rules.items():
        if not isinstance(value, Mapping):
            raise RuntimeError(f"persisted condition {raw_code!r} is malformed")
        code = int(raw_code)
        rules[code] = ConditionRule(
            condition_id=code,
            name=str(value.get("name", "")),
            updates_high_low=bool(value.get("updates_high_low", False)),
            updates_open_close=bool(value.get("updates_open_close", False)),
            updates_volume=bool(value.get("updates_volume", False)),
            raw=dict(value.get("raw", {}) or {}),
        )
    retrieved_at = datetime.fromisoformat(str(payload["retrieved_at"]))
    return ConditionSnapshot(retrieved_at=retrieved_at, rules=rules)
