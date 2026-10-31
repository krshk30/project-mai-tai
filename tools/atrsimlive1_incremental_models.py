"""Whole-world admission to Main's sealed selective API, never rendered-row reuse.

Facts retain original provenance; current verification is linked separately.
Unknown/open/mixed cohorts recompute. No individual account ledger is skipped.
"""
from dataclasses import asdict
from datetime import datetime, time

from atrsimlive1_incremental_cache import CacheRefusal, cache_key, choose, fingerprint, finished_known, features
from atrsimlive1_incremental_codec import encode_model, decode_model, encode_reports, decode_reports
from atrsimlive1_incremental_reads import proof_bindings
from atrsimlive1_incremental_container import copied_world
from atrsimlive1_working_windows import nanos, ET


CORE = ('TODAY:SUPPLIED', 'VOLBODY:QUEUE_AWARE', 'VOLBODY:OPTIMISTIC')
FIRSTBAR = ('FIRSTBAR', 'FIRSTBAR_X3')


class IncrementalModels:
    def __init__(self, run):
        self.run, self.contexts, self.reused = run, {}, {}
        self.closed_components = features(run.binding)['closed_components']
        if self.closed_components and run.binding['input_scope'] != 'CURRENT_RR':
            raise CacheRefusal('closed_components_require_current_RR_not_FILE_witness')
        self.component_contexts = {}

    def require(self, producer, *, null_policy, same_sip_policy, execution_rules_sha256, policy_source):
        expected = dict(execution_rules_sha256=execution_rules_sha256,
                        null_policy=null_policy, same_sip_policy=same_sip_policy)
        if (self.run.binding['policies'] != expected
                or fingerprint(producer['source_pins']) != self.run.binding['source_seal']
                or same_sip_policy is None):
            raise CacheRefusal('literal_paper_policy_and_actual_source_seal_required')
        self.policy_source_sha256 = fingerprint(None if policy_source is None else asdict(policy_source))

    def _proof(self, family, data, episodes, origins, route_packet, line_inputs):
        if family not in ('CORE', 'FIRSTBAR'):
            raise CacheRefusal('reviewed_whole_world_family_required')
        symbol, = data['symbols']
        states = self.run.next['windows'][symbol]
        inputs = {item['origin']: item for item in self.run.inputs[symbol]}
        resolved_origins = []
        for requested in origins:
            found = [origin for origin in inputs if origin == requested or origin.startswith(requested + ':original_ns=')]
            if len(found) != 1:
                raise CacheRefusal('one_actual_model_window_origin_required')
            resolved_origins.append(found[0])
        origins = resolved_origins
        if len(origins) != len(episodes) or len(set(origins)) != len(origins):
            raise CacheRefusal('actual_ordered_model_window_origins_required')
        horizon = max(nanos(inputs[origin]['certificates']['trades']['end']) for origin in origins)
        header_hashes = {}
        clocks = dict(bars='bar_time', capture_bars='event_ts', scanner='event_at', fills='filled_at')
        for kind, field in clocks.items():
            header_hashes[kind] = fingerprint([row for row in data['rows'][kind] if nanos(row[field]) < horizon])
        # Relevant actual route lines, not unrelated symbols or the growing
        # whole-log hash. Original receipt's log provenance is never rewritten.
        route_lines = (None if not isinstance(route_packet, dict) or not isinstance(route_packet.get('lines'), list)
                       else [line for line in route_packet['lines'] if isinstance(line, dict)
                             and isinstance(line.get('text'), str) and ' symbol=' + symbol + ' ' in line['text']])
        header_hashes['route'] = fingerprint(route_lines)
        header_hashes['oracle'] = fingerprint([line for line in line_inputs
                                               if line['derived_available_ns'] <= horizon])
        identities = [asdict(episode.identity) for episode in episodes]
        header_hashes['membership'] = fingerprint(dict(identities=identities, origins=origins))
        # Current effective guards, including newly observed faults beyond a
        # finished trade but inside its supplied envelope, MUST invalidate.
        guards = []
        for episode, origin in zip(episodes, origins):
            coverage, flip = episode.tape_coverage, episode.flip_coverage
            guard = dict(tape_complete=coverage.complete, tape_start=coverage.start_ns,
                tape_gaps=coverage.gaps,
                execution_policy_source_sha256=self.policy_source_sha256,
                same_sip_policy=episode.same_sip_policy,
                tape_covers_working_end=coverage.end_ns >= nanos(inputs[origin]['certificates']['trades']['end']),
                flip_complete=flip.complete, flip_gaps=flip.gaps)
            guard['watched'] = episode.watched
            guards.append(guard)
        header_hashes['coverage'] = fingerprint(guards)
        windows = [dict(identity=identity, certificates=inputs[origin]['certificates'],
                        header_hashes=header_hashes) for identity, origin in zip(identities, origins)]
        # Whole current source population is still reported fresh. Only the
        # closed model-input prefix is a reusable semantic dependency.
        dependencies = dict(family=family, ordered_origins=origins, headers=header_hashes,
            guards=guards, policies=self.run.binding['policies'])
        proof = proof_bindings(identities, windows=windows, cohort_dependencies=dependencies,
            policies=self.run.binding['policies'], source_seal=self.run.binding['source_seal'],
            current_asof=data['source_asof'], current_snapshot=data['snapshot_id'])
        proof['identities'], proof['origins'] = identities, origins
        session_end = nanos(datetime.combine(datetime.fromisoformat(data['trading_day']).date(), time(20), ET))
        bounded_day_closed = nanos(data['source_asof']) >= session_end and nanos(data['end']) == session_end
        # Clock closes the INPUT envelope only. A modeled OPEN/UNKNOWN outcome
        # still fails finished_known; no synthetic session-exit print exists.
        proof['closed'] = all(states[origin]['end_key'] is not None or bounded_day_closed for origin in origins)
        proof['guards_known'] = all(guard['tape_complete'] is True and guard['flip_complete'] is True
            and guard['watched'] is True
            and guard['tape_covers_working_end'] is True
            and not guard['tape_gaps'] and not guard['flip_gaps'] for guard in guards)
        self.contexts[symbol, family] = proof
        return proof

    def _component_frames(self, family, data, items, origins, route_packet, line_inputs):
        from atrsimlive1_reuse_components import (COMPONENT_V1, components, identity_key, input_key, tie_context)

        episodes = tuple(item.episode for item in items) if family == 'FIRSTBAR' else tuple(items)
        full = self._proof(family, data, episodes, origins, route_packet, line_inputs)
        symbol, = data['symbols']
        originals = {item['origin']: item for item in self.run.inputs[symbol]}
        states = self.run.next['windows'][symbol]
        ids = tuple(identity_key(episode) for episode in episodes)
        membership, ties = fingerprint(ids), tie_context(episodes)
        worlds = FIRSTBAR if family == 'FIRSTBAR' else CORE
        frames = {world: {} for world in worlds}
        session_end = nanos(datetime.combine(datetime.fromisoformat(data['trading_day']).date(), time(20), ET))
        day_closed = nanos(data['source_asof']) >= session_end and nanos(data['end']) == session_end
        for members in components(episodes):
            group_ids = tuple(ids[index] for index in members)
            horizon = max(nanos(originals[full['origins'][index]]['certificates']['trades']['end']) for index in members)
            headers = {kind: fingerprint([row for row in data['rows'][kind] if nanos(row[field]) < horizon])
                for kind, field in dict(bars='bar_time', capture_bars='event_ts', scanner='event_at', fills='filled_at').items()}
            route_lines = (None if not isinstance(route_packet, dict) or not isinstance(route_packet.get('lines'), list)
                else [line for line in route_packet['lines'] if isinstance(line, dict)
                    and isinstance(line.get('text'), str) and ' symbol=' + symbol + ' ' in line['text']])
            headers['route'] = fingerprint(route_lines)
            headers['oracle'] = fingerprint([line for line in line_inputs if line['derived_available_ns'] <= horizon])
            headers['membership'] = fingerprint(dict(identities=full['identities'], origins=full['origins']))
            # Coverage SourceRefs also contain the collector's changing RR
            # witness. The exact modeled coverage values are separately in
            # input_key; original certificate bounds/content below stay FULL.
            headers['coverage'] = fingerprint([dict(
                tape=tuple(getattr(episodes[index].tape_coverage, name) for name in ('start_ns', 'end_ns', 'complete', 'gaps')),
                flip=tuple(getattr(episodes[index].flip_coverage, name) for name in ('start_ns', 'end_ns', 'complete', 'gaps')),
                watched=episodes[index].watched) for index in members])
            stable_certs = [{kind: {key: cert[key] for key in ('kind', 'symbol', 'start', 'end', 'day_start',
                    'row_count', 'content_sha256', 'source_timezone', 'query_sha256')}
                for kind, cert in originals[full['origins'][index]]['certificates'].items()} for index in members]
            component_history = fingerprint(dict(certificates=stable_certs, headers=headers,
                whole_membership_sha256=membership, source_seal=self.run.binding['source_seal'], policies=self.run.binding['policies']))
            # Exact derived inputs AND complete original raw-window content are
            # retained. No cropping raw history at a modeled profitable exit.
            dependencies = dict(family=family, whole_membership_sha256=membership,
                component_members=group_ids,
                typed_input_sha256=tuple(input_key(items[index], snapshot_id=data['snapshot_id'],
                    source_asof=data['source_asof'], source_hashes=data['source_hashes'],
                    component_history_sha256=component_history) for index in members),
                component_tie_context_sha256=tie_context(tuple(episodes[index] for index in members)),
                execution_policy_source_sha256=self.policy_source_sha256)
            for index in members:
                origin = full['origins'][index]
                certificates = originals[origin]['certificates']
                # FULL original component window/header proof, never an exit
                # crop, timestamp watermark or report-row approximation.
                actual = proof_bindings([asdict(episodes[index].identity)],
                    windows=[dict(identity=asdict(episodes[index].identity),
                        certificates=certificates, header_hashes=headers)],
                    cohort_dependencies=dependencies, policies=self.run.binding['policies'],
                    source_seal=self.run.binding['source_seal'], current_asof=data['source_asof'],
                    current_snapshot=data['snapshot_id'])
                for world in worlds:
                    frames[world][ids[index]] = dict(schema=COMPONENT_V1, world=world,
                        source_seal=self.run.binding['source_seal'], policies=self.run.binding['policies'],
                        whole_membership_sha256=membership, component_members=group_ids,
                        global_tie_context_sha256=ties, certificates=certificates,
                        header_hashes=headers, dependencies=dependencies,
                        history_binding=actual['bindings'][0], current_verification=actual['current_verification'],
                        input_closed=states[origin]['end_key'] is not None or day_closed)
        return episodes, frames, ties

    def execute(self, family, data, items, origins, route_packet, line_inputs, *, fill_policy=None):
        """Opt-in connected caller; OFF must use the unchanged arguments path."""
        from atrsimlive1_reuse_components import execute

        if not self.closed_components:
            raise CacheRefusal('component_execute_requires_explicit_ON_profile')
        episodes, frames, ties = self._component_frames(family, data, items, origins, route_packet, line_inputs)
        symbol, = data['symbols']
        worlds = FIRSTBAR if family == 'FIRSTBAR' else CORE
        candidates = {}
        for world in worlds:
            old = self.run.previous.get('worlds', {}).get(symbol + '|' + world, {})
            candidates[world] = old.get('component_candidates', {})
            if not isinstance(candidates[world], dict):
                raise CacheRefusal('closed_candidate_population_mapping_required')
        results, reports, audit = execute(family, tuple(items), candidates, frames,
            source_seal=self.run.binding['source_seal'], policies=self.run.binding['policies'],
            global_tie_context_sha256=ties, current_asof=data['source_asof'],
            current_snapshot=data['snapshot_id'], fill_policy=fill_policy, enabled=True)
        self.component_contexts[symbol, family] = dict(items=tuple(items), episodes=episodes,
            frames=frames, candidates=candidates, audit=audit)
        for value in audit:
            self.run.audit.append(dict(symbol=symbol, family=family, action='COMPONENT_MODEL_EXECUTED',
                world=value['world'], reused_episode_indices=value['reused_episode_indices'],
                fresh_episode_indices=value['fresh_episode_indices'],
                reused_episode_worlds=len(value['reused_episode_indices']),
                whole_world_reused=len(value['reused_episode_indices']) == len(episodes),
                current_full_tie_context_sha256=ties,
                current_snapshot=data['snapshot_id']))
        return results, reports

    def _record_components(self, family, data, results, reports, document):
        from atrsimlive1_reuse_components import (COMPONENT_V1, candidate_proof_key, exposure_guard, identity_key)
        from project_mai_tai.atr_simulation.same_sip import report_key

        symbol, = data['symbols']
        context = self.component_contexts[symbol, family]
        episodes, frames = context['episodes'], context['frames']
        worlds = FIRSTBAR if family == 'FIRSTBAR' else CORE
        audits = {value['world']: value for value in context['audit']}
        for world in worlds:
            typed = tuple(result for result in results if result.mode.value == world) if family == 'FIRSTBAR' else tuple(results)
            if tuple(result.identity for result in typed) != tuple(episode.identity for episode in episodes):
                raise CacheRefusal('complete_current_component_result_population_required')
            rows = document['tables'][world]['rows']
            indexed = {fingerprint(item['identity']): item for item in rows}
            if len(indexed) != len(rows) or set(indexed) != {identity_key(episode) for episode in episodes}:
                raise CacheRefusal('exact_complete_component_row_membership_required')
            saved = {}
            for index, (episode, result) in enumerate(zip(episodes, typed)):
                key = identity_key(episode)
                frame, item = frames[world][key], indexed[key]
                receipt = document['producer']['engine_receipts'][item['outcome_id']]
                if not exposure_guard(episode, receipt, item['row'], input_closed=frame['input_closed'])['allowed']:
                    continue
                prior = context['candidates'][world].get(key)
                if index in audits[world]['reused_episode_indices']:
                    # Preserve the ORIGINAL source receipt/provenance separately
                    # from this CURRENT RR verification, never retag old as new.
                    if not isinstance(prior, dict) or prior['proof_key'] != candidate_proof_key(frame):
                        raise CacheRefusal('reused_component_original_proof_required')
                    saved[key] = dict(prior, last_verification=frame['current_verification'])
                    continue
                actual_report_key = report_key(episode.identity, world)
                if actual_report_key not in reports:
                    raise CacheRefusal('actual_component_same_sip_report_required')
                saved[key] = dict(identity=asdict(episode.identity), proof_key=candidate_proof_key(frame),
                    receipt=receipt, row=item['row'], receipt_sha256=fingerprint(receipt),
                    row_sha256=fingerprint(item['row']), typed_result=encode_model(result),
                    typed_reports=encode_reports({actual_report_key: reports[actual_report_key]}),
                    original_world_key=fingerprint(dict(world=world, frame_key=candidate_proof_key(frame), identity=key)),
                    original_provenance=dict(source_asof=data['source_asof'], snapshot_id=data['snapshot_id'],
                        input_refs=data['source_hashes'], current_verification=frame['current_verification'],
                        full_tie_context_sha256=frame['global_tie_context_sha256']))
            self.run.next['worlds'][symbol + '|' + world] = dict(component_profile=COMPONENT_V1,
                component_candidates=saved, last_verification=dict(snapshot_id=data['snapshot_id'], asof=data['source_asof']))
            self.run.audit.append(dict(symbol=symbol, world=world, action='CLOSED_COMPONENTS_SAVED', count=len(saved)))

    def arguments(self, family, data, items, origins, route_packet, line_inputs):
        episodes = tuple(item.episode for item in items) if family == 'FIRSTBAR' else tuple(items)
        proof = self._proof(family, data, episodes, origins, route_packet, line_inputs)
        symbol, = data['symbols']
        worlds = FIRSTBAR if family == 'FIRSTBAR' else CORE
        retained, reports, bindings = {}, {}, {}
        if not proof['closed'] or not proof['guards_known']:
            self.run.audit.append(dict(symbol=symbol, family=family, action='FRESH_WORLD', reason='OPEN_OR_UNKNOWN_INPUT'))
            return {}
        for world in worlds:
            slot = symbol + '|' + world
            old = self.run.previous.get('worlds', {}).get(slot)
            keys = tuple(cache_key(identity=identity, world=world, window_sha256=window,
                cohort_sha256=proof['cohort_sha256'], policies=self.run.binding['policies'],
                source_seal=self.run.binding['source_seal'])
                for identity, window in zip(proof['identities'], proof['window_hashes']))
            if old is None or tuple(old.get('member_keys', ())) != keys or tuple(old.get('bindings', ())) != proof['bindings']:
                self.run.audit.append(dict(symbol=symbol, world=world, action='FRESH_WORLD', reason='DEPENDENCY_CHANGED_OR_MISSING'))
                continue
            if len(old.get('entries', ())) != len(keys):
                raise CacheRefusal('complete_cached_world_members_required')
            admitted = [choose(entry, expected_key=key, current_dependency_sha256=proof['cohort_sha256'],
                               source_seal=self.run.binding['source_seal'])
                        for key, entry in zip(keys, old['entries'])]
            if any(item['action'] != 'REUSE_FINISHED' for item in admitted):
                self.run.audit.append(dict(symbol=symbol, world=world, action='FRESH_WORLD', reason='UNKNOWN_OR_MIXED_OUTPUT'))
                continue
            retained[world] = decode_model(old['typed_results'])
            self.reused[symbol, world] = old
            decoded_reports = decode_reports(old['typed_reports'])
            if any(key[1] != world for key in decoded_reports):
                raise CacheRefusal('cached_reports_must_belong_to_retained_world')
            reports.update(decoded_reports)
            bindings[world] = proof['bindings']
            self.run.audit.append(dict(symbol=symbol, world=world, action='REUSE_FINISHED_WORLD',
                                       current_verification=proof['current_verification']))
        if not retained:
            return {}
        key = 'retained_modes' if family == 'FIRSTBAR' else 'retained_worlds'
        return {key: retained, 'retained_reports': reports,
                'retained_bindings': bindings, 'current_bindings': dict(bindings)}

    def record(self, family, data, results, reports, document):
        if self.closed_components:
            return self._record_components(family, data, results, reports, document)
        symbol, = data['symbols']
        proof = self.contexts[symbol, family]
        if not proof['closed'] or not proof['guards_known']:
            return
        worlds = FIRSTBAR if family == 'FIRSTBAR' else CORE
        for world in worlds:
            if (symbol, world) in self.reused:
                self.run.next['worlds'][symbol + '|' + world] = copied_world(self.reused[symbol, world],
                    last_verification=proof['current_verification'])
                continue
            typed = tuple(result for result in results if result.mode.value == world) if family == 'FIRSTBAR' else tuple(results)
            if tuple(asdict(result.identity) for result in typed) != tuple(proof['identities']):
                raise CacheRefusal('complete_actual_model_result_order_required')
            rows = document['tables'][world]['rows']
            indexed = {fingerprint(item['identity']): item for item in rows}
            if len(indexed) != len(rows):
                raise CacheRefusal('duplicate_current_model_identity')
            entries, keys = [], []
            for identity, window in zip(proof['identities'], proof['window_hashes']):
                item = indexed.get(fingerprint(identity))
                if item is None:
                    raise CacheRefusal('missing_current_model_row')
                receipt = document['producer']['engine_receipts'][item['outcome_id']]
                if not finished_known(receipt, item['row']):
                    entries = []
                    break
                key = cache_key(identity=identity, world=world, window_sha256=window,
                    cohort_sha256=proof['cohort_sha256'], policies=self.run.binding['policies'],
                    source_seal=self.run.binding['source_seal'])
                keys.append(key)
                entries.append(dict(key=key, source_seal=self.run.binding['source_seal'],
                    dependency_sha256=proof['cohort_sha256'], receipt=receipt, row=item['row'],
                    receipt_sha256=fingerprint(receipt), row_sha256=fingerprint(item['row']),
                    original_provenance=dict(source_asof=data['source_asof'], snapshot_id=data['snapshot_id'],
                        input_refs=data['source_hashes'], current_verification=proof['current_verification'])))
            if not entries:
                self.run.audit.append(dict(symbol=symbol, world=world, action='NOT_CACHED', reason='UNKNOWN_OPEN_OR_MIXED'))
                continue
            relevant_reports = {key: value for key, value in reports.items() if key[1] == world}
            self.run.next['worlds'][symbol + '|' + world] = dict(member_keys=keys, bindings=proof['bindings'],
                entries=entries, typed_results=encode_model(typed), typed_reports=encode_reports(relevant_reports))
