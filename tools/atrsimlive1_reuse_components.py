"""Opt-in closed-component reuse through the existing sealed kernel APIs.

Planning does not execute a model. execute() is Main's explicit execution path;
it preserves dependency-connected accounts and validates the full final cohort.
No provider or database access is performed here.
"""
from dataclasses import asdict, fields, is_dataclass
from copy import deepcopy

from atrsimlive1_incremental_cache import CacheRefusal, fingerprint, finished_known, _sha
from atrsimlive1_incremental_reads import proof_bindings
from atrsimlive1_working_windows import nanos


COMPONENT_V1 = 'ATRSIMLIVE1_CLOSED_COMPONENT_REUSE_V2_SCOPED_HISTORY'
KERNEL_CONTRACT = 'ATRSIM_SAME_FULL_TAPE_CLOSED_COMPONENT_V1'
WORLDS = ('FIRSTBAR', 'FIRSTBAR_X3', 'TODAY:SUPPLIED',
          'VOLBODY:QUEUE_AWARE', 'VOLBODY:OPTIMISTIC')


def identity_key(episode):
    return fingerprint(asdict(episode.identity))


def input_key(item, *, snapshot_id, source_asof, source_hashes, component_history_sha256=None):
    """Stable semantics, with ONLY this collector's RR witness kept separate.

    This affects a cache KEY, never a retained input/result/SourceRef. Actual
    event/received clocks, all quantities/prices, source digest/kind/pointer,
    policy sources and every non-wrapper provenance field remain in the key.
    The complete current RR certificates are independently verified in plan.
    """
    from atrsimlive1_collect import digest
    from project_mai_tai.atr_simulation.same_sip_types import SourceRef

    observation_artifact = 'postgres:readonly-snapshot:' + snapshot_id
    observation_sha = digest(source_hashes)
    observation_asof = nanos(source_asof)
    scoped_history = observation_sha if component_history_sha256 is None else _sha(component_history_sha256)
    def visit(value):
        if isinstance(value, SourceRef):
            if (value.artifact == observation_artifact and value.sha256 == observation_sha
                    and value.asof_ns == observation_asof):
                return dict(current_RR_wrapper_verification_separate=True,
                    bound_component_history_sha256=scoped_history, kind=value.kind, pointer=value.pointer)
            return asdict(value)
        if is_dataclass(value):
            return {field.name: visit(getattr(value, field.name)) for field in fields(value)}
        if isinstance(value, dict):
            return {key: visit(part) for key, part in value.items()}
        if isinstance(value, (tuple, list)):
            return tuple(visit(part) for part in value)
        return value
    return fingerprint(visit(item))


def candidate_proof_key(proof):
    # Full certificate CONTENT is in history_binding. Its changing RR/asof
    # witness must remain separate, exactly as in the original proof_bindings.
    return fingerprint({key: value for key, value in proof.items()
                        if key not in ('current_verification', 'certificates', 'global_tie_context_sha256')})


def tie_context(episodes):
    """Bind the full-cohort report's tie-group membership, never a fake zero."""
    physical, groups = {}, {}
    for episode in episodes:
        for trade in episode.trades:
            if trade.symbol != episode.identity.symbol:
                raise CacheRefusal('component_trade_symbol_mismatch')
            key = trade.symbol, trade.key.raw_id
            if key in physical and physical[key] != trade:
                raise CacheRefusal('conflicting_shared_component_print')
            physical[key] = trade
    for trade in physical.values():
        groups.setdefault((trade.symbol, trade.key.event_ns), []).append((trade.key.sequence, trade.key.raw_id))
    return fingerprint([(key, tuple(sorted(members))) for key, members in sorted(groups.items()) if len(members) > 1])


def exposure_guard(episode, receipt, row, *, input_closed):
    """Keep complete original history proof; scope coverage to known exposure.

    No-entry/open/UNKNOWN results are NEVER admitted. A complete receipt is
    necessary but not sufficient: raw history/dependencies are checked below.
    """
    if input_closed is not True or not finished_known(receipt, row):
        return dict(allowed=False, reason='OPEN_INPUT_OR_UNKNOWN_OUTCOME')
    clocks = [fact.get('at', {}).get('event_ns') for fact in (*receipt['fills'], *receipt['exits'])]
    if any(type(clock) is not int or clock < 0 for clock in clocks):
        return dict(allowed=False, reason='EXACT_ACTUAL_FILL_EXIT_CLOCKS_REQUIRED')
    first, last = min(clocks), max(clocks)
    fill_clocks = [fact['at']['event_ns'] for fact in receipt['fills']]
    exit_clock = receipt['exits'][0]['at']['event_ns']
    if max(fill_clocks) > exit_clock:
        return dict(allowed=False, reason='ENTRY_AFTER_CLOSED_EXIT_REFUSED')
    tape, flip = episode.tape_coverage, episode.flip_coverage
    if (episode.watched is not True or tape.complete is not True or flip.complete is not True
            or tape.gaps or flip.gaps
            or not tape.start_ns <= first <= last < tape.end_ns
            or not flip.start_ns <= episode.buy_flip.available_ns < flip.end_ns):
        return dict(allowed=False, reason='UNKNOWN_OR_UNCOVERED_CLOSED_EXPOSURE')
    return dict(allowed=True, reason='KNOWN_CLOSED_EXPOSURE_WITH_FULL_HISTORY_REQUIRED',
                first_fill_ns=first, closed_exit_ns=last)


def components(episodes):
    """Dependency closure from actual inputs, not a supplied independent flag.

    Conservative temporal lifetimes reach SELL/session even for a target-exited
    position, preserving FIRSTBAR's original overlapping-slot refusal rule.
    Shared physical prints/quote identities also connect otherwise disjoint
    windows. All components retain original member order.
    """
    ids = tuple(identity_key(episode) for episode in episodes)
    if len(set(ids)) != len(ids):
        raise CacheRefusal('duplicate_component_episode_identity')
    footprints = []
    for episode in episodes:
        boundaries = [episode.buy_flip.available_ns]
        if episode.buy_flip.bar_start_ns is not None:
            boundaries.append(episode.buy_flip.bar_start_ns)
        boundaries.extend(fact.at.event_ns for fact in episode.today_entries)
        boundaries.extend(trade.key.event_ns for trade in episode.trades)
        right = min(episode.session_end.available_ns,
                    episode.sell_flip.available_ns if episode.sell_flip is not None else episode.session_end.available_ns)
        if any(type(clock) is not int for clock in (*boundaries, right)) or not min(boundaries) <= right:
            raise CacheRefusal('actual_component_lifetime_required')
        prints = {(trade.symbol, trade.key.raw_id) for trade in episode.trades}
        quotes = {(quote.symbol, quote.key.raw_id) for quote in episode.quotes}
        tie_clocks = {(trade.symbol, trade.key.event_ns) for trade in episode.trades}
        # CORE iterates the UNION tape for other orders too. A supplied tail
        # after this episode's SELL can still be inside another episode's wait;
        # include it in the dependency hull, not just this position's lifetime.
        footprint_right = max((right, *(trade.key.event_ns for trade in episode.trades)))
        footprints.append((episode.identity.symbol, min(boundaries), footprint_right, prints, quotes, tie_clocks))
    parent = list(range(len(episodes)))
    def root(index):
        while parent[index] != index:
            index = parent[index]
        return index
    for left, a in enumerate(footprints):
        for right in range(left + 1, len(footprints)):
            b = footprints[right]
            if a[0] == b[0] and (max(a[1], b[1]) <= min(a[2], b[2]) or a[3] & b[3] or a[4] & b[4] or a[5] & b[5]):
                parent[root(right)] = root(left)
    groups = {}
    for index in range(len(episodes)):
        groups.setdefault(root(index), []).append(index)
    return tuple(tuple(members) for members in groups.values())


def plan(episodes, candidates, current_proofs, *, world, source_seal, policies,
         global_tie_context_sha256, current_asof=None, current_snapshot=None, enabled=False):
    """Admit only entire independent components with exact current proof keys.

    current_proofs are produced by the reviewed CURRENT_RR wrapper, including
    complete original window/history, actual closed input, code/policy/current
    membership and source proof. A timestamp-only or fabricated flat witness
    does not meet that producer contract. Kernel rechecks typed results/tape.
    """
    if type(enabled) is not bool:
        raise CacheRefusal('explicit_component_boolean_required')
    if not enabled:
        return dict(enabled=False, retained=(), fresh=tuple(range(len(episodes))))
    if world not in WORLDS:
        raise CacheRefusal('literal_component_world_required')
    _sha(source_seal)
    _sha(global_tie_context_sha256)
    if global_tie_context_sha256 != tie_context(episodes):
        raise CacheRefusal('actual_whole_cohort_tie_context_required')
    nanos(current_asof)
    if type(current_snapshot) is not str or not current_snapshot:
        raise CacheRefusal('actual_current_RR_component_snapshot_required')
    ids = tuple(identity_key(episode) for episode in episodes)
    whole_membership = fingerprint(ids)
    retained, fresh, audit = [], [], []
    for members in components(episodes):
        group_ids = tuple(ids[index] for index in members)
        accepted = []
        reason = None
        for index in members:
            entry, proof = candidates.get(ids[index]), current_proofs.get(ids[index])
            if not isinstance(entry, dict) or not isinstance(proof, dict):
                reason = 'MISSING_CURRENT_CLOSED_HISTORY_OR_CANDIDATE'
                break
            required = {'schema', 'world', 'source_seal', 'policies', 'whole_membership_sha256',
                        'component_members', 'global_tie_context_sha256', 'history_binding',
                        'current_verification', 'input_closed', 'certificates', 'header_hashes', 'dependencies'}
            if set(proof) != required or proof['schema'] != COMPONENT_V1 or not proof['current_verification']:
                raise CacheRefusal('complete_current_component_proof_required')
            if proof['dependencies'].get('component_tie_context_sha256') != tie_context(tuple(episodes[i] for i in members)):
                raise CacheRefusal('actual_dependency_component_tie_context_required')
            _sha(proof['history_binding'])
            actual_history = proof_bindings([asdict(episodes[index].identity)],
                windows=[dict(identity=asdict(episodes[index].identity), certificates=proof['certificates'],
                              header_hashes=proof['header_hashes'])],
                cohort_dependencies=proof['dependencies'], policies=policies, source_seal=source_seal,
                current_asof=current_asof, current_snapshot=current_snapshot)
            if (proof['history_binding'] != actual_history['bindings'][0]
                    or proof['current_verification'] != actual_history['current_verification']):
                raise CacheRefusal('current_full_closed_history_binding_required')
            if (proof['world'] != world or proof['source_seal'] != source_seal or proof['policies'] != policies
                    or proof['whole_membership_sha256'] != whole_membership
                    or tuple(proof['component_members']) != group_ids
                    or proof['global_tie_context_sha256'] != global_tie_context_sha256
                    or entry.get('proof_key') != candidate_proof_key(proof)
                    or entry.get('identity') != asdict(episodes[index].identity)):
                reason = 'HISTORY_POLICY_MEMBERSHIP_OR_TIE_CONTEXT_CHANGED'
                break
            guard = exposure_guard(episodes[index], entry['receipt'], entry['row'],
                                   input_closed=proof['input_closed'])
            if not guard['allowed']:
                reason = guard['reason']
                break
            if (entry.get('receipt_sha256') != fingerprint(entry['receipt'])
                    or entry.get('row_sha256') != fingerprint(entry['row'])
                    or not entry.get('original_provenance') or not entry.get('original_world_key')):
                raise CacheRefusal('closed_component_integrity_or_original_provenance_required')
            _sha(entry['original_world_key'])
            accepted.append(dict(index=index, candidate=entry,
                                 current_verification=proof['current_verification']))
        if reason is None:
            retained.extend(accepted)
            audit.append(dict(members=members, action='RETAINED_COMPONENT_PROPOSAL', world=world))
        else:
            fresh.extend(members)
            audit.append(dict(members=members, action='FRESH_COMPONENT', reason=reason, world=world))
    return dict(enabled=True, schema=COMPONENT_V1, required_kernel_contract=KERNEL_CONTRACT,
                whole_membership_sha256=whole_membership, retained=tuple(retained),
                fresh=tuple(fresh), audit=tuple(audit))


def execute(family, items, candidates_by_world, current_proofs_by_world, *, source_seal,
            policies, global_tie_context_sha256, current_asof, current_snapshot,
            fill_policy=None, enabled=False):
    """Executable tools seam using ONLY the existing sealed engine APIs.

    Fresh independent components execute on the original engine. Complete
    ordered tuples are finally passed through the original FULL-cohort engine
    with its whole-world selective API, retaining full tape/type/policy checks.
    No retained marker is reported as an actual hit before that call succeeds.
    """
    from atrsimlive1_incremental_codec import decode_model, decode_reports
    from project_mai_tai.atr_simulation import same_sip_engine as core
    from project_mai_tai.atr_simulation import same_sip_firstbar_replay as firstbar
    from project_mai_tai.atr_simulation.same_sip import TieTape, report_key

    if type(enabled) is not bool or family not in ('CORE', 'FIRSTBAR'):
        raise CacheRefusal('literal_component_execution_profile_required')
    episodes = tuple(item.episode for item in items) if family == 'FIRSTBAR' else tuple(items)
    worlds = WORLDS[:2] if family == 'FIRSTBAR' else WORLDS[2:]
    engine_type = firstbar.FirstBarReplayEngine if family == 'FIRSTBAR' else core.SimulationEngine
    policy_args = {'fill_policy': fill_policy} if family == 'FIRSTBAR' else {}
    if not enabled:
        original = engine_type()
        results = original.simulate_many(tuple(items), **policy_args)
        return results, original.same_sip_reports, ()
    if set(candidates_by_world) != set(worlds) or set(current_proofs_by_world) != set(worlds):
        raise CacheRefusal('all_family_component_candidate_populations_required')
    admissions = {world: plan(episodes, candidates_by_world[world], current_proofs_by_world[world],
        world=world, source_seal=source_seal, policies=policies,
        global_tie_context_sha256=global_tie_context_sha256, current_asof=current_asof,
        current_snapshot=current_snapshot, enabled=True) for world in worlds}
    if not any(value['retained'] for value in admissions.values()):
        original = engine_type()
        results = original.simulate_many(tuple(items), **policy_args)
        return results, original.same_sip_reports, tuple(dict(world=world,
            reused_episode_indices=(), fresh_episode_indices=tuple(range(len(episodes)))) for world in worlds)

    # This is exactly the report field's existing semantics: TieState.report
    # uses len(self.tape.groups) on the actual full shared tape. The action
    # counters/events, economics, prices, quantities and clocks are untouched.
    full_tape = core._shared_tape(episodes)
    full_group_count = len(TieTape(full_tape).groups)
    retained = {world: {value['index']: value for value in admission['retained']}
                for world, admission in admissions.items()}
    full_results = {world: {} for world in worlds}
    full_reports = {}

    def candidate(index, world):
        entry = retained[world][index]['candidate']
        if 'typed_result' not in entry or 'typed_reports' not in entry:
            raise CacheRefusal('actual_typed_component_result_and_reports_required')
        result = decode_model(entry['typed_result'])
        reports = decode_reports(entry['typed_reports'])
        key = report_key(episodes[index].identity, world)
        if (set(reports) != {key} or type(reports[key].get('same_timestamp_groups')) is not int
                or reports[key]['same_timestamp_groups'] < 0 or result.identity != episodes[index].identity):
            raise CacheRefusal('retained_component_identity_or_full_tie_context_mismatch')
        # A later independent component can add whole-tape groups. Local
        # action/report dependencies were proved unchanged above. Keep the
        # immutable original report in the candidate; derive this ONE global
        # cardinality on a fresh copy for the current whole-cohort validator.
        reports = deepcopy(reports)
        reports[key]['same_timestamp_groups'] = full_group_count
        # The original whole-world API below performs the sealed type/mode/
        # policy/report validations. Do not replace those with duck typing.
        return result, reports

    for members in components(episodes):
        subset = tuple(items[index] for index in members)
        selected, selected_reports, bindings = {}, {}, {}
        for world in worlds:
            if all(index in retained[world] for index in members):
                values = []
                for index in members:
                    value, reports = candidate(index, world)
                    values.append(value)
                    selected_reports.update(reports)
                selected[world] = tuple(values)
                bindings[world] = tuple(current_proofs_by_world[world][identity_key(episodes[index])]['history_binding']
                                        for index in members)
        if set(selected) == set(worlds):
            for world in worlds:
                for index, value in zip(members, selected[world]):
                    full_results[world][index] = value
            full_reports.update(selected_reports)
            continue
        options = {}
        if selected:
            options = {'retained_modes' if family == 'FIRSTBAR' else 'retained_worlds': selected,
                       'retained_reports': selected_reports, 'retained_bindings': bindings,
                       'current_bindings': dict(bindings)}
        fresh_engine = engine_type()
        fresh_results = fresh_engine.simulate_many(subset, **policy_args, **options)
        if family == 'FIRSTBAR':
            for world in worlds:
                values = tuple(value for value in fresh_results if value.mode.value == world)
                if tuple(value.identity for value in values) != tuple(episodes[index].identity for index in members):
                    raise CacheRefusal('component_firstbar_result_order_required')
                for index, value in zip(members, values):
                    full_results[world][index] = value
        else:
            if tuple(value.identity for value in fresh_results) != tuple(episodes[index].identity for index in members):
                raise CacheRefusal('component_core_result_order_required')
            for world in worlds:
                for index, value in zip(members, fresh_results):
                    full_results[world][index] = value
        for key, report in fresh_engine.same_sip_reports.items():
            report = deepcopy(report)
            report['same_timestamp_groups'] = full_group_count
            full_reports[key] = report

    final_retained, final_bindings = {}, {}
    for world in worlds:
        if set(full_results[world]) != set(range(len(episodes))):
            raise CacheRefusal('complete_ordered_component_assembly_required')
        final_retained[world] = tuple(full_results[world][index] for index in range(len(episodes)))
        # This binds the assembly and ALL members, including fresh components.
        # Retained submembers were separately admitted against current full
        # original history; fresh submembers have just executed above.
        final_bindings[world] = tuple(fingerprint(dict(schema=COMPONENT_V1, world=world,
            identity=asdict(episode.identity), whole_membership=admissions[world]['whole_membership_sha256'],
            source_seal=source_seal, policies=policies, tie_context=global_tie_context_sha256,
            current_snapshot=current_snapshot, current_asof=current_asof,
            receipt=fingerprint(asdict(full_results[world][index]))))
            for index, episode in enumerate(episodes))
    final_engine = engine_type()
    options = {'retained_modes' if family == 'FIRSTBAR' else 'retained_worlds': final_retained,
               'retained_reports': full_reports, 'retained_bindings': final_bindings,
               'current_bindings': dict(final_bindings)}
    results = final_engine.simulate_many(tuple(items), **policy_args, **options)
    audit = tuple(dict(world=world, reused_episode_indices=tuple(sorted(retained[world])),
                       fresh_episode_indices=tuple(index for index in range(len(episodes)) if index not in retained[world]))
                  for world in worlds)
    return results, final_engine.same_sip_reports, audit
