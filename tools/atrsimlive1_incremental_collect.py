"""Explicit incremental collection coordinator inside original RR/budgets.

No own connection, provider, process, SQL execution or mutable broker state.
The existing collector supplies its bounded fetch and actual identity witness.
"""
from dataclasses import replace
from contextlib import contextmanager

from atrsimlive1_collect import encoded
from atrsimlive1_incremental_cache import (CacheRefusal, INCREMENTAL_V1, fingerprint,
    locked_cache, require_version, MAX_CACHE_BYTES, features)
from atrsimlive1_incremental_reads import (query_pair, certify, read_plan,
    rows_with_fingerprints, content_hash, merge_open)
from atrsimlive1_working_windows import plan, terminal, selection, nanos
from atrsimlive1_window_collect import clock, SCHEMA
from atrsimlive1_progress import stage


class IncrementalRun:
    def __init__(self, store):
        if store.binding['input_scope'] != 'CURRENT_RR':
            raise CacheRefusal('production_requires_current_RR_cache_scope')
        self.store = store
        self.binding = store.binding
        self.audit = []
        stage('CACHE_LOAD', '+')
        try:
            self.previous = store.load() or {}
            if (not isinstance(self.previous, dict)
                    or not isinstance(self.previous.get('windows', {}), dict)
                    or not isinstance(self.previous.get('worlds', {}), dict)):
                raise CacheRefusal('cache_population_shape')
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            self.previous = {}
            self.audit.append(dict(action='FRESH_ALL', reason='CACHE_UNAVAILABLE_OR_REFUSED'))
        stage('CACHE_LOAD', '-')
        self.next = dict(windows={}, worlds={})
        self.inputs = {}
        self.model_controller = None
        self.completed_model_roster = None
        self.certificates, self.fresh_payloads = {}, {}
        self.since_query_hashes = set()
        self.materialized_bytes = 0
        self.identity = self.params = None
        self.symbols = ()

    def bind_snapshot(self, identity, params, symbols):
        if (identity['read_only'] != 'on' or identity['isolation'] != 'repeatable read'
                or str(params['day']) != self.binding['trading_day']):
            raise CacheRefusal('actual_current_RR_day_required')
        self.identity, self.params, self.symbols = identity, params, tuple(symbols)
        self.next['roster'] = list(symbols)
        self.next['source_asof'] = params['asof']

    def _certificate(self, kind, symbol, start, end, fetch):
        key = kind, symbol, start, end, self.identity['snapshot_id'], nanos(self.params['asof'])
        if key in self.certificates:
            return self.certificates[key]
        queries = query_pair(kind)
        bound = dict(symbol=symbol, start=clock(start), end=clock(end),
                     day_start=self.params['start'], asof=self.params['asof'])
        result = fetch(queries['content'], bound, 'INCREMENTAL_FINGERPRINT:' + kind + ':' + symbol)
        if len(result) != 1:
            raise CacheRefusal('one_actual_content_fingerprint_required')
        certificate = certify(result[0], kind=kind, symbol=symbol, start=bound['start'], end=bound['end'],
            day_start=bound['day_start'], asof=self.params['asof'],
            snapshot_id=self.identity['snapshot_id'], query_sha256=queries['query_sha256'],
            read_only=self.identity['read_only'], isolation=self.identity['isolation'], complete=True)
        self.certificates[key] = certificate
        return certificate

    def _fresh_rows(self, kind, symbol, start, end, fetch):
        key = kind, symbol, start, end, self.identity['snapshot_id'], nanos(self.params['asof'])
        if key in self.fresh_payloads:
            return self.fresh_payloads[key]
        sql = query_pair(kind)['rows']
        rows, hashes, after, after_id = [], {}, None, None
        while True:
            bound = dict(symbol=symbol, start=clock(start), end=clock(end),
                day_start=self.params['start'], asof=self.params['asof'], after_ts=after, after_id=after_id)
            found = fetch(sql, bound, 'INCREMENTAL_ROWS:' + kind + ':' + symbol)
            page = found[:5000]
            raw, checksums = rows_with_fingerprints(page, kind=kind)
            rows.extend(raw)
            if set(hashes).intersection(checksums):
                raise CacheRefusal('duplicate_capture_page_identity')
            hashes.update(checksums)
            if len(rows) > 1000000:
                raise CacheRefusal('today_symbol_capture_cap')
            if len(found) <= 5000:
                break
            current = page[-1]['event_ts'], page[-1]['id']
            if after is not None and current <= (after, after_id):
                raise CacheRefusal('capture_cursor_not_advancing')
            after, after_id = current
        self.fresh_payloads[key] = rows, hashes
        return rows, hashes

    @staticmethod
    def _hashes(symbol, value):
        return {(symbol, int(key)): checksum for key, checksum in value.items()}

    def _since_rows(self, kind, symbol, start, end, previous_asof, fetch):
        from atrsimlive1_change_proof import candidate_query

        rows, hashes, after, after_id = [], {}, None, None
        while True:
            query = candidate_query(kind, symbol=symbol, previous_asof=previous_asof,
                current_asof=self.params['asof'], start=clock(start), end=clock(end),
                day_start=self.params['start'], after_ts=after, after_id=after_id)
            self.since_query_hashes.add(query['query_sha256'])
            found = fetch(query['sql'], query['params'], 'INCREMENTAL_SINCE_ROWS:' + kind + ':' + symbol)
            page = found[:5000]
            raw, checksums = rows_with_fingerprints(page, kind=kind)
            if set(hashes).intersection(checksums):
                raise CacheRefusal('duplicate_capture_page_identity')
            rows.extend(raw)
            hashes.update(checksums)
            if len(rows) > 1000000:
                raise CacheRefusal('today_symbol_capture_cap')
            if len(found) <= 5000:
                return rows, hashes
            current = page[-1]['event_ts'], page[-1]['id']
            if after is not None and current <= (after, after_id):
                raise CacheRefusal('capture_cursor_not_advancing')
            after, after_id = current

    def _since_window(self, kind, symbol, start, end, old, full, fetch):
        from atrsimlive1_change_proof import merge_since

        if any(old['certificate'].get(key) != full.get(key) for key in
               ('schema', 'kind', 'symbol', 'start', 'day_start', 'source_timezone', 'query_sha256', 'complete')):
            rows, hashes = self._fresh_rows(kind, symbol, start, end, fetch)
            if len(rows) != full['row_count'] or content_hash(rows, hashes) != full['content_sha256']:
                raise CacheRefusal('actual_window_content_mismatch')
            self.audit.append(dict(symbol=symbol, kind=kind, action='FRESH_WINDOW',
                                   reason='PRIOR_FINGERPRINT_SCOPE_CHANGED'))
            return dict(rows=rows, hashes={str(key[1]): value for key, value in hashes.items()}, certificate=full)
        # Reuse unchanged closed content WITHOUT adding a pointless suffix
        # query. The just-read full certificate is already the exact prefix.
        if nanos(old['certificate']['end']) == end:
            unchanged = read_plan(old['certificate'], full, current_asof=self.params['asof'],
                current_snapshot=self.identity['snapshot_id'], open_window=False)
            if unchanged['action'] == 'RETAIN_CLOSED_INPUT':
                rows, hashes, action = old['rows'], self._hashes(symbol, old['hashes']), unchanged
            else:
                rows = hashes = action = None
        else:
            rows = hashes = action = None
        if action is None:
            candidates, candidate_hashes = self._since_rows(kind, symbol, start, end, old['certificate']['asof'], fetch)
            merged = merge_since(old, candidates, candidate_hashes, current_full=full,
                current_asof=self.params['asof'], current_snapshot=self.identity['snapshot_id'])
            if merged['action'] == 'SINCE_MERGE_EXACT_CURRENT_FULL_CERTIFICATE':
                rows, hashes = merged['rows'], merged['hashes']
                action = {key: value for key, value in merged.items() if key not in ('rows', 'hashes')}
            else:
                rows, hashes = self._fresh_rows(kind, symbol, start, end, fetch)
                action = dict(merged, fallback='ORIGINAL_CURRENT_FULL_WINDOW_ROWS')
        if len(rows) != full['row_count'] or content_hash(rows, hashes) != full['content_sha256']:
            raise CacheRefusal('actual_window_content_mismatch')
        self.audit.append(dict(symbol=symbol, kind=kind, **action))
        return dict(rows=rows, hashes={str(key[1]): value for key, value in hashes.items()}, certificate=full)

    def _window_rows(self, kind, symbol, start, end, previous, fetch):
        full = self._certificate(kind, symbol, start, end, fetch)
        old = None if previous is None else previous.get(kind)
        action = dict(action='FRESH_WINDOW', reason='NO_PRIOR_INPUT')
        if old is not None and old['certificate']['start'] == clock(start) and nanos(old['certificate']['end']) <= end:
            if features(self.binding)['since_last_run']:
                return self._since_window(kind, symbol, start, end, old, full, fetch)
            old_end = nanos(old['certificate']['end'])
            prefix = self._certificate(kind, symbol, start, old_end, fetch)
            action = read_plan(old['certificate'], prefix, current_asof=self.params['asof'],
                               current_snapshot=self.identity['snapshot_id'], open_window=old_end < end)
            if action['action'] == 'RETAIN_CLOSED_INPUT':
                rows, hashes = old['rows'], self._hashes(symbol, old['hashes'])
                if len(rows) != full['row_count'] or content_hash(rows, hashes) != full['content_sha256']:
                    raise CacheRefusal('retained_input_content_mismatch')
            elif action['action'] == 'DELTA':
                delta, delta_hashes = self._fresh_rows(kind, symbol, old_end, end, fetch)
                rows, hashes = merge_open(prefix_rows=old['rows'], prefix_hashes=self._hashes(symbol, old['hashes']),
                    delta_rows=delta, delta_hashes=delta_hashes, cutoff=clock(old_end),
                    current_full_certificate=full, current_asof=self.params['asof'],
                    current_snapshot=self.identity['snapshot_id'])
            else:
                rows, hashes = self._fresh_rows(kind, symbol, start, end, fetch)
        else:
            rows, hashes = self._fresh_rows(kind, symbol, start, end, fetch)
        if len(rows) != full['row_count'] or content_hash(rows, hashes) != full['content_sha256']:
            raise CacheRefusal('actual_window_content_mismatch')
        self.audit.append(dict(symbol=symbol, kind=kind, **action))
        return dict(rows=rows, hashes={str(key[1]): value for key, value in hashes.items()}, certificate=full)

    def collect_symbol(self, *, symbol, scoped, params, snapshot, universe, fetch, policy, source, queries,
                       null_policy=None):
        if self.identity is None or snapshot != self.identity['snapshot_id'] or symbol not in self.symbols:
            raise CacheRefusal('bound_whole_roster_current_snapshot_required')
        anchors = []
        for row in scoped['fills']:
            if row['side'] == 'buy':
                anchors.append(dict(symbol=symbol, at=row['filled_at'], source='fill:' + row['fill_id']))
                if row['submitted_at'] is not None:
                    anchors.append(dict(symbol=symbol, at=row['submitted_at'], source='order:' + row['order_id']))
        planned = plan([symbol], scoped['bars'], start=params['start'], end=params['end'], placements=anchors)
        prior = self.previous.get('windows', {}).get(symbol, {})
        current, resolved, proof_inputs = {}, [], []
        combined = {kind: {} for kind in ('trades', 'quotes')}
        for window in planned['windows']:
            old = prior.get(window.origin)
            if window.terminal_pending:
                retained_terminal = False
                probe_floor = window.boundary_ns
                if (old is not None and old.get('boundary_ns') == window.boundary_ns
                        and old.get('start_ns') == window.start_ns and old.get('end_key') is not None):
                    old_end = nanos(old['trades']['certificate']['end'])
                    witness = self._certificate('trades', symbol, window.start_ns, old_end, fetch)
                    exact = read_plan(old['trades']['certificate'], witness, current_asof=params['asof'],
                                      current_snapshot=snapshot, open_window=False)
                    if exact['action'] == 'RETAIN_CLOSED_INPUT':
                        window = replace(window, terminal_pending=False, end_key=tuple(old['end_key']),
                                         status='BOUNDARY_FIRST_ELIGIBLE_KEY')
                        retained_terminal = True
                elif (old is not None and old.get('boundary_ns') == window.boundary_ns
                      and old.get('start_ns') == window.start_ns
                      and old.get('status') == 'ASOF_NO_ELIGIBLE_TERMINATOR'):
                    old_end = nanos(old['trades']['certificate']['end'])
                    if old_end <= window.asof_end_ns:
                        witness = self._certificate('trades', symbol, window.start_ns, old_end, fetch)
                        exact = read_plan(old['trades']['certificate'], witness, current_asof=params['asof'],
                                         current_snapshot=snapshot, open_window=False)
                        if exact['action'] == 'RETAIN_CLOSED_INPUT':
                            probe_floor = max(probe_floor, old_end)
                            if probe_floor == window.asof_end_ns:
                                window = replace(window, terminal_pending=False, status='ASOF_NO_ELIGIBLE_TERMINATOR')
                                retained_terminal = True
                if not retained_terminal:
                    after, after_id = None, None
                    while window.terminal_pending:
                        found = fetch(queries.CAPTURE_PAGES['trades'], dict(params, symbol=symbol,
                            start=clock(probe_floor), after_ts=after, after_id=after_id),
                            'INCREMENTAL_TERMINAL:' + symbol)
                        page = found[:queries.PAGE_SIZE]
                        window = terminal(window, page, policy=policy, source=source,
                            complete=len(found) <= queries.PAGE_SIZE, null_policy=null_policy)
                        if window.terminal_pending:
                            key = page[-1]['event_ts'], page[-1]['id']
                            if after is not None and key <= (after, after_id):
                                raise CacheRefusal('terminal_cursor_not_advancing')
                            after, after_id = key
            # Stored PostgreSQL clocks have microsecond precision. Exclusive
            # <last+1us is exactly <=last; it does not retime any raw event.
            end = window.asof_end_ns if window.end_key is None else window.end_key[0] + 1000
            state = dict(start_ns=window.start_ns, boundary_ns=window.boundary_ns,
                         end_key=window.end_key, origin=window.origin, status=window.status)
            for kind in ('trades', 'quotes'):
                state[kind] = self._window_rows(kind, symbol, window.start_ns, end, old, fetch)
                for row in state[kind]['rows']:
                    key = row['id']
                    previous = combined[kind].get(key)
                    if previous is not None and previous != row:
                        raise CacheRefusal('cohort_shared_physical_row_conflict')
                    combined[kind][key] = row
            current[window.origin] = state
            resolved.append(window)
            proof_inputs.append(dict(origin=window.origin,
                certificates={kind: state[kind]['certificate'] for kind in ('trades', 'quotes')}))
        for kind, values in combined.items():
            scoped[kind] = sorted(values.values(), key=lambda row: (row['event_ts'], row['id']))
        self.materialized_bytes += len(encoded(scoped))
        if self.materialized_bytes > MAX_CACHE_BYTES:
            raise CacheRefusal('today_source_byte_cap')
        self.next['windows'][symbol] = current
        self.inputs[symbol] = proof_inputs
        census = [row for row in universe['census'] if row['symbol'] == symbol]
        certificate = dict(schema=SCHEMA, purpose='SAME_RR_SQL_WINDOWS_PLUS_FULL_CENSUS', snapshot_id=snapshot,
            descriptors=selection(resolved),
            metadata=dict(census=census, conditions=[row for row in universe['conditions'] if row['symbol'] == symbol],
                          incremental_content=proof_inputs),
            full_counts=dict(trades=sum(row['row_count'] for row in census),
                quotes=sum(row['row_count'] for row in universe['quote_counts'] if row['symbol'] == symbol)))
        if null_policy is not None:
            certificate['null_policy'] = null_policy
            certificate['metadata']['null_policy'] = null_policy
        return certificate

    def persist(self):
        if self.identity is None or set(self.next['windows']) != set(self.symbols):
            raise CacheRefusal('all_original_roster_components_required')
        if self.completed_model_roster is not None:
            completed = tuple(self.completed_model_roster)
            if completed != self.symbols[:len(completed)]:
                raise CacheRefusal('exact_original_completed_model_prefix_required')
            if completed != self.symbols:
                # Full SQL materialization is NOT full model completion. Do not
                # enter the writer at all or replace a prior complete cache.
                receipt = dict(status='NOT_WRITTEN_INCOMPLETE_MODEL_ROSTER',
                    original_symbols=len(self.symbols), completed_symbols=len(completed),
                    pending_symbols=len(self.symbols) - len(completed), previous_cache_preserved=True)
                self.audit.append(dict(action='CACHE_SAVE_SKIPPED_PARTIAL', **receipt))
                stage('CACHE_WRITE_SKIPPED_PARTIAL', '-')
                return receipt
        stage('CACHE_WRITE', '+')
        result = self.store.save(self.next)
        stage('CACHE_WRITE', '-', result['bytes'])
        return result

    def seal_packet(self, data):
        from atrsimlive1_window_view import envelope_sha256

        data['query_sha256'] = fingerprint(dict(base_window_queries=data['query_sha256'],
            incremental_version=INCREMENTAL_V1,
            exact_fingerprints={kind: query_pair(kind) for kind in ('trades', 'quotes')},
            **({'actual_since_query_sha256': sorted(self.since_query_hashes)}
               if features(self.binding)['since_last_run'] else {})))
        data['window_view']['envelope_sha256'] = envelope_sha256(data)
        return data


@contextmanager
def incremental_session(root=None, binding=None, *, version=None, retention_enabled=False):
    """Default None opens nothing. Commit cache only after complete model run."""
    require_version(version)
    if version is None:
        yield None
        return
    with locked_cache(root, binding, retention_enabled=retention_enabled) as store:
        run = IncrementalRun(store)
        yield run
        run.cache_write_receipt = run.persist()
