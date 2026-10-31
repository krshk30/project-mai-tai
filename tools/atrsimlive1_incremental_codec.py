"""Fixed pure-SIM result codec. No payload-directed imports or executable data.

Call ONLY after Main source-seal and private-cache envelope/hash validation.
Default/nonincremental callers do not import this module or its registry.
"""
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import importlib
from uuid import UUID

from atrsimlive1_incremental_cache import CacheRefusal


TAG = '__atrsim_incremental_model_v1__'
MAX_DEPTH = 64
MAX_NODES = 2000000
WHITELIST = {
    'types': ('SimExitReason', 'LiveReason', 'EntryMode', 'FillModel', 'EntryDecision', 'EvidenceKind',
        'SourceRef', 'Coverage', 'EventKey', 'ConditionRuleEvidence', 'ExecutionPolicy',
        'ExecutionEligibility', 'TradePrint', 'Quote', 'FlipIdentity', 'Boundary', 'EntryFact',
        'LiveExitFact', 'LiveFacts', 'BodySnapshot', 'SimOrder', 'BodyObservation', 'EntryPlan',
        'FillAllocation', 'SimExit', 'PositionReceipt', 'ScenarioResult', 'CompatibilityResult', 'EpisodeResult'),
    'same_sip_firstbar': ('FirstBarMode', 'CompletedFlipBar', 'FirstBarEntryPlan'),
    'same_sip_firstbar_replay': ('FirstBarFillPolicy', 'FirstBarReplayResult'),
    'same_sip_firstbar_live_baseline': ('AggregateExitReason', 'BaselineBarClose', 'AggregateBaselineResult'),
    'firstbar_study': ('StudyBuyLeg',),
}


def registry():
    """Import names are source literals, never taken from a cache tag."""
    from project_mai_tai.atr_simulation.same_sip_binding import verify_same_sip_files

    verify_same_sip_files()
    result = {}
    for module_name, names in WHITELIST.items():
        module = importlib.import_module('project_mai_tai.atr_simulation.' + module_name)
        for name in names:
            kind = getattr(module, name)
            if not isinstance(kind, type) or not (is_dataclass(kind) or issubclass(kind, Enum)):
                raise CacheRefusal('sealed_model_registry_type_changed')
            result[module_name + ':' + name] = kind
    return result


def _walk(value, operation, *, allowed):
    nodes = 0
    reverse = {kind: tag for tag, kind in allowed.items()}
    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise CacheRefusal('model_codec_depth_or_node_cap')
        if operation == 'encode':
            kind = type(item)
            if isinstance(item, Enum):
                if kind not in reverse:
                    raise CacheRefusal('unlisted_model_enum')
                return {TAG: 'enum', 'type': reverse[kind], 'name': item.name}
            if is_dataclass(item):
                if kind not in reverse:
                    raise CacheRefusal('unlisted_model_dataclass')
                return {TAG: 'dataclass', 'type': reverse[kind],
                        'fields': {field.name: visit(getattr(item, field.name), depth + 1) for field in fields(item)}}
            if isinstance(item, tuple):
                return {TAG: 'tuple', 'items': [visit(part, depth + 1) for part in item]}
            if isinstance(item, dict):
                if TAG in item or any(not isinstance(key, str) for key in item):
                    raise CacheRefusal('reserved_model_tag_or_nonstring_key')
                return {key: visit(part, depth + 1) for key, part in item.items()}
            if isinstance(item, list):
                return [visit(part, depth + 1) for part in item]
            if item is None or type(item) in (str, int, bool) or isinstance(item, (Decimal, datetime, UUID)):
                return item
            raise CacheRefusal('unsupported_model_value')
        if isinstance(item, list):
            return [visit(part, depth + 1) for part in item]
        if not isinstance(item, dict):
            if item is None or type(item) in (str, int, bool) or isinstance(item, (Decimal, datetime, UUID)):
                return item
            raise CacheRefusal('unsupported_decoded_model_value')
        if TAG not in item:
            return {key: visit(part, depth + 1) for key, part in item.items()}
        if item[TAG] == 'tuple' and set(item) == {TAG, 'items'} and isinstance(item['items'], list):
            return tuple(visit(part, depth + 1) for part in item['items'])
        if item[TAG] == 'enum' and set(item) == {TAG, 'type', 'name'}:
            kind = allowed.get(item['type'])
            if kind is None or not issubclass(kind, Enum) or item['name'] not in kind.__members__:
                raise CacheRefusal('unknown_sealed_model_enum')
            return kind.__members__[item['name']]
        if item[TAG] == 'dataclass' and set(item) == {TAG, 'type', 'fields'}:
            kind = allowed.get(item['type'])
            if kind is None or not is_dataclass(kind) or not isinstance(item['fields'], dict):
                raise CacheRefusal('unknown_sealed_model_dataclass')
            if set(item['fields']) != {field.name for field in fields(kind)}:
                raise CacheRefusal('exact_sealed_model_fields_required')
            values = {key: visit(part, depth + 1) for key, part in item['fields'].items()}
            return kind(**values)
        raise CacheRefusal('invalid_or_extra_model_tag_fields')
    return visit(value)


def encode_model(value):
    return _walk(value, 'encode', allowed=registry())


def decode_model(value):
    return _walk(value, 'decode', allowed=registry())


def encode_reports(reports):
    # report_key is ((seven literal identity fields), world); preserve tuple.
    return encode_model(tuple((key, report) for key, report in reports.items()))


def decode_reports(value):
    pairs = decode_model(value)
    if not isinstance(pairs, tuple):
        raise CacheRefusal('retained_report_tuple_required')
    result = {}
    for key, report in pairs:
        if (not isinstance(key, tuple) or len(key) != 2 or not isinstance(key[0], tuple)
                or len(key[0]) != 7 or any(not isinstance(part, str) for part in (*key[0], key[1]))
                or key in result or not isinstance(report, dict)):
            raise CacheRefusal('retained_report_key_or_duplicate_refused')
        result[key] = report
    return result
