"""C1 12:51 HISTORICAL_CAPTURED_RR only; no production cache admission.

Exact fixed full-input dependency; known closed components through the original
selective APIs. UNKNOWN/open members recompute as whole dependency components.
"""
from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
from uuid import UUID

from atrsimlive1_captured_rr import ClosedOriginalRR, CapturedRefusal, SCOPE
from atrsimlive1_incremental_cache import fingerprint
from atrsimlive1_incremental_codec import encode_model, decode_model, encode_reports, decode_reports
from atrsimlive1_reuse_components import components, identity_key, exposure_guard


VERSION = 'C1_20261029_HISTORICAL_CAPTURED_RR_MODEL_REUSE_V1'
WORLDS = {'FIRSTBAR': ('FIRSTBAR', 'FIRSTBAR_X3'),
          'CORE': ('TODAY:SUPPLIED', 'VOLBODY:QUEUE_AWARE', 'VOLBODY:OPTIMISTIC')}

INPUT_TYPES = {
    'types': ('SourceRef', 'Coverage', 'EventKey', 'ConditionRuleEvidence', 'ExecutionPolicy',
              'ExecutionEligibility', 'TradePrint', 'Quote', 'FlipIdentity', 'Boundary',
              'EntryFact', 'LiveExitFact', 'LiveFacts', 'EpisodeInput', 'EvidenceKind', 'LiveReason'),
    'same_sip_types': ('EpisodeInput',),
    'firstbar': ('CompletedFlipBar',),
    'same_sip_firstbar': ('CompletedFlipBar',),
    'firstbar_replay': ('FirstBarReplayInput', 'FirstBarFillPolicy'),
    'same_sip_firstbar_replay': ('FirstBarReplayInput', 'FirstBarFillPolicy'),
}
INPUT_NAMES = frozenset('project_mai_tai.atr_simulation.' + module + '.' + name
                        for module, names in INPUT_TYPES.items() for name in names)
INPUT_NODE_CAP = 2000000


def typed_input_fingerprint(value, *, statistics=None):
    """Input-only structural digest; never decode or extend the output codec.

    Every field/type/order/value is bound. Memoize the content digest of shared
    objects (especially the same 55-rule policy on every print) instead of
    materializing repeated nested policy trees. No object ID enters the digest.
    """
    memo, scalar_memo, nodes = {}, {}, 0
    visits, object_hits, scalar_hits, max_depth = 0, 0, 0, 0
    def visit(item, depth=0):
        nonlocal nodes, visits, object_hits, scalar_hits, max_depth
        visits += 1
        max_depth = max(max_depth, depth)
        if depth > 64:
            raise CapturedRefusal('historical_input_depth_cap')
        kind = type(item)
        shared = is_dataclass(item) or kind in (tuple, list, dict)
        token = (id(item), kind)
        if shared and token in memo:
            object_hits += 1
            return memo[token]
        scalar_token = None
        if not shared:
            # Type-exact CONTENT keys. Python numeric equality alone would
            # conflate bool/int, Decimal scale, or float signed zero.
            if isinstance(item, Enum):
                scalar_token = (kind, item.name)
            elif kind is Decimal:
                if not item.is_finite():
                    raise CapturedRefusal('nonfinite_historical_input_decimal')
                scalar_token = (kind, str(item))
            elif kind is datetime:
                if item.utcoffset() is None:
                    raise CapturedRefusal('naive_historical_input_clock')
                scalar_token = (kind, item.isoformat(), item.fold)
            elif kind is UUID:
                scalar_token = (kind, str(item))
            elif kind is float:
                if not math.isfinite(item):
                    raise CapturedRefusal('nonfinite_historical_input_float')
                scalar_token = (kind, item.hex())
            elif item is None or kind in (str, int, bool):
                scalar_token = (kind, item)
            if scalar_token is not None and scalar_token in scalar_memo:
                scalar_hits += 1
                return scalar_memo[scalar_token]
        nodes += 1
        if nodes > INPUT_NODE_CAP:
            raise CapturedRefusal('historical_input_node_cap_unique=' + str(nodes)
                + '_visits=' + str(visits) + '_scalar_hits=' + str(scalar_hits))
        name = kind.__module__ + '.' + kind.__qualname__
        if isinstance(item, Enum):
            if name not in INPUT_NAMES:
                raise CapturedRefusal('unreviewed_historical_input_enum')
            content = ['enum', name, item.name, visit(item.value, depth + 1)]
        elif is_dataclass(item) and not isinstance(item, type):
            if name not in INPUT_NAMES:
                raise CapturedRefusal('unreviewed_historical_input_dataclass')
            content = ['dataclass', name, [[field.name, visit(getattr(item, field.name), depth + 1)]
                                         for field in fields(item)]]
        elif kind is Decimal:
            if not item.is_finite():
                raise CapturedRefusal('nonfinite_historical_input_decimal')
            content = ['Decimal', str(item)]
        elif kind is datetime:
            if item.utcoffset() is None:
                raise CapturedRefusal('naive_historical_input_clock')
            content = ['datetime', item.isoformat(), item.fold]
        elif kind is UUID:
            content = ['UUID', str(item)]
        elif kind is float:
            if not math.isfinite(item):
                raise CapturedRefusal('nonfinite_historical_input_float')
            content = ['float', item.hex()]
        elif kind in (tuple, list):
            content = [kind.__name__, [visit(part, depth + 1) for part in item]]
        elif kind is dict:
            if any(type(key) is not str for key in item):
                raise CapturedRefusal('nonstring_historical_input_key')
            content = ['dict', [[key, visit(part, depth + 1)] for key, part in item.items()]]
        elif item is None or kind in (str, int, bool):
            content = [kind.__name__, item]
        else:
            raise CapturedRefusal('unsupported_historical_input_type')
        result = hashlib.sha256(json.dumps(content, ensure_ascii=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if shared:
            memo[token] = result
        elif scalar_token is not None:
            scalar_memo[scalar_token] = result
        return result
    try:
        return visit(value)
    finally:
        if statistics is not None:
            statistics.update(total_visits=visits, attempted_unique_nodes=nodes,
                object_memo_hits=object_hits, scalar_memo_hits=scalar_hits,
                object_memo_entries=len(memo), scalar_memo_entries=len(scalar_memo),
                maximum_depth=max_depth, unique_node_cap=INPUT_NODE_CAP)


class HistoricalModels:
    closed_components = True

    def __init__(self, run, witness, data, previous):
        if type(witness) is not ClosedOriginalRR or run.binding['input_scope'] != SCOPE:
            raise CapturedRefusal('explicit_historical_run_required')
        self.run, self.witness, self.data = run, witness, data
        self.previous, self.next, self.contexts = previous, {}, {}
        # Includes ALL original rows, source hashes, bounds, certs and policy.
        # This is not a per-row SQL digest or a fresh RR verification.
        self.full_input = fingerprint(data)

    def require(self, producer, *, null_policy, same_sip_policy, execution_rules_sha256, policy_source):
        actual = dict(execution_rules_sha256=execution_rules_sha256,
                      null_policy=null_policy, same_sip_policy=same_sip_policy)
        if (actual != self.run.binding['policies'] or same_sip_policy is None
                or fingerprint(producer['source_pins']) != self.run.binding['source_seal']):
            raise CapturedRefusal('exact_historical_source_and_policy_required')

    def execute(self, family, data, items, origins, route_packet, line_inputs, *, fill_policy=None):
        from project_mai_tai.atr_simulation import same_sip_engine as core
        from project_mai_tai.atr_simulation import same_sip_firstbar_replay as firstbar
        from project_mai_tai.atr_simulation.same_sip import TieTape, report_key

        if family not in WORLDS or data is not self.data or route_packet is not None:
            raise CapturedRefusal('same_historical_data_and_literal_family_required')
        episodes = tuple(item.episode for item in items) if family == 'FIRSTBAR' else tuple(items)
        groups = components(episodes)
        worlds = WORLDS[family]
        engine_type = firstbar.FirstBarReplayEngine if family == 'FIRSTBAR' else core.SimulationEngine
        options = {'fill_policy': fill_policy} if family == 'FIRSTBAR' else {}
        input_statistics = {}
        input_digest = typed_input_fingerprint(tuple(items), statistics=input_statistics)
        dependency = fingerprint(dict(scope=SCOPE, capture=self.witness.capture_binding_sha256,
            full_input=self.full_input, items=input_digest, origins=origins,
            fill_policy=typed_input_fingerprint(fill_policy),
            line_inputs=line_inputs, source=self.run.binding, family=family))
        old = self.previous.get(family)
        accepted = {}
        if old is not None:
            if old['dependency'] != dependency:
                raise CapturedRefusal('historical_cache_dependency_changed')
            for world in worlds:
                accepted[world] = {index: old['candidates'][world][str(index)] for members in groups
                    if all(str(index) in old['candidates'][world] for index in members) for index in members}
        else:
            accepted = {world: {} for world in worlds}
        context = dict(dependency=dependency, episodes=episodes, groups=groups,
                       reused={world: tuple(sorted(accepted[world])) for world in worlds})
        self.contexts[family] = context
        if not any(accepted.values()):
            engine = engine_type()
            results = engine.simulate_many(tuple(items), **options)
            reports = engine.same_sip_reports
        else:
            full_results, reports = {world: {} for world in worlds}, {}
            full_count = len(TieTape(core._shared_tape(episodes)).groups)
            for members in groups:
                selected, subreports, bindings = {}, {}, {}
                for world in worlds:
                    if not all(index in accepted[world] for index in members):
                        continue
                    values = []
                    for index in members:
                        entry = accepted[world][index]
                        if fingerprint(entry['content']) != entry['sha256']:
                            raise CapturedRefusal('historical_typed_candidate_hash_changed')
                        value = decode_model(entry['content']['result'])
                        decoded = decode_reports(entry['content']['reports'])
                        key = report_key(episodes[index].identity, world)
                        if (value.identity != episodes[index].identity or set(decoded) != {key}
                                or decoded[key]['same_timestamp_groups'] != full_count):
                            raise CapturedRefusal('fixed_full_cohort_result_and_tie_context_required')
                        if not exposure_guard(episodes[index], entry['content']['receipt'],
                                entry['content']['row'], input_closed=True)['allowed']:
                            raise CapturedRefusal('historical_unknown_or_open_candidate_refused')
                        values.append(value)
                        subreports.update(decoded)
                    selected[world] = tuple(values)
                    bindings[world] = tuple(dependency for _ in members)
                if len(selected) != len(worlds):
                    arguments = {} if not selected else {
                        'retained_modes' if family == 'FIRSTBAR' else 'retained_worlds': selected,
                        'retained_reports': subreports, 'retained_bindings': bindings,
                        'current_bindings': dict(bindings)}
                    engine = engine_type()
                    fresh = engine.simulate_many(tuple(items[index] for index in members), **options, **arguments)
                    selected = {world: tuple(value for value in fresh if value.mode.value == world)
                                if family == 'FIRSTBAR' else tuple(fresh) for world in worlds}
                    subreports = deepcopy(engine.same_sip_reports)
                    for report in subreports.values():
                        report['same_timestamp_groups'] = full_count
                for world in worlds:
                    if tuple(value.identity for value in selected[world]) != tuple(episodes[i].identity for i in members):
                        raise CapturedRefusal('complete_ordered_historical_component_required')
                    full_results[world].update(zip(members, selected[world]))
                reports.update(subreports)
            selected = {world: tuple(full_results[world][i] for i in range(len(episodes))) for world in worlds}
            bindings = {world: tuple(dependency for _ in episodes) for world in worlds}
            engine = engine_type()
            results = engine.simulate_many(tuple(items), **options, **{
                'retained_modes' if family == 'FIRSTBAR' else 'retained_worlds': selected,
                'retained_reports': reports, 'retained_bindings': bindings, 'current_bindings': dict(bindings)})
            reports = engine.same_sip_reports
        self.run.audit.append(dict(family=family, scope=SCOPE, reused=context['reused'],
            input_fingerprint_sha256=input_digest, input_fingerprint_statistics=input_statistics,
            fresh={world: tuple(i for i in range(len(episodes)) if i not in accepted[world]) for world in worlds}))
        return results, reports

    def record(self, family, data, results, reports, document):
        from project_mai_tai.atr_simulation.same_sip import report_key
        from atrsimlive1_working_windows import nanos

        context = self.contexts[family]
        episodes, worlds = context['episodes'], WORLDS[family]
        saved = {world: {} for world in worlds}
        for world in worlds:
            typed = tuple(value for value in results if value.mode.value == world) if family == 'FIRSTBAR' else tuple(results)
            indexed = {fingerprint(item['identity']): item for item in document['tables'][world]['rows']}
            if tuple(value.identity for value in typed) != tuple(episode.identity for episode in episodes):
                raise CapturedRefusal('whole_historical_record_population_required')
            for index, (episode, value) in enumerate(zip(episodes, typed)):
                item = indexed[identity_key(episode)]
                receipt = document['producer']['engine_receipts'][item['outcome_id']]
                if not exposure_guard(episode, receipt, item['row'],
                        input_closed=episode.session_end.available_ns <= nanos(data['source_asof']))['allowed']:
                    continue
                key = report_key(episode.identity, world)
                if key not in reports:
                    raise CapturedRefusal('actual_historical_same_sip_report_required')
                content = dict(result=encode_model(value), reports=encode_reports({key: reports[key]}),
                               receipt=receipt, row=item['row'])
                saved[world][str(index)] = dict(content=content, sha256=fingerprint(content))
        self.next[family] = dict(dependency=context['dependency'], candidates=saved)
