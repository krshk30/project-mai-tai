"""Main-only retained census and adapter-copy comparison, never a native simulator.

Source chunks are read-only. Current ledger rows cannot reconstruct historical
quotes/books or cause a counterfactual order to fill. Components are labelled.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


OLD = '9c10a7de1cd2fc7a7df2b0e587fe132d46faa4a7'
NEW = '724d36c5010f3e544e0d29d509060528662f5f82'
MANIFEST_SHA = 'c2e0b426792fa3b63f85d2fbd70ef856ce0633fdf9a2bb2c0917fd9758291cbf'
ROOTS = {OLD: Path('/private/tmp/oct24-l14-only-a2-fallback-20261024'),
         NEW: Path('/private/tmp/oct25-weekend-9c-lineage-20261025')}
ET = ZoneInfo('America/New_York')
SINCE = datetime(2026, 10, 12, tzinfo=ET)
MAX_CHUNK = 16 * 1024 * 1024


class EvidenceError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise EvidenceError('numeric_field_not_exact')
    result = Decimal(value)
    if not result.is_finite():
        raise EvidenceError('nonfinite_number')
    return result


def stamp(value):
    if not isinstance(value, str):
        raise EvidenceError('timestamp_missing')
    fraction = value.partition('.')[2]
    digits = ''
    for char in fraction:
        if not char.isdigit():
            break
        digits += char
    if len(digits) > 6 and any(char != '0' for char in digits[6:]):
        raise EvidenceError('submicrosecond_clock_requires_reviewed_adapter')
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None or at.utcoffset() is None:
        raise EvidenceError('timezone_missing')
    return at


def session(value):
    try:
        local = stamp(value).astimezone(ET)
    except (EvidenceError, ValueError):
        return 'UNKNOWN_TIME'
    minute = local.hour * 60 + local.minute
    return ('PRE_0400_0930' if 240 <= minute < 570 else 'RTH_0930_1600'
            if 570 <= minute < 960 else 'AH_1600_2000' if 960 <= minute < 1200
            else 'OUTSIDE_0400_2000')


def family(code):
    if isinstance(code, str) and code.startswith('momentum_'):
        return 'MOM'
    return 'ORB' if code in {'orb', 'orb_schwab'} else None


class Capture:
    """Selected FULL source tables, not Mirror's structural subset or a DB read."""
    def __init__(self, manifest, parts, *, expected_sha=MANIFEST_SHA):
        raw = Path(manifest).read_bytes()
        if digest(raw) != expected_sha:
            raise EvidenceError('capture_manifest_hash')
        self.manifest_sha = expected_sha
        self.manifest = json.loads(raw)
        self.parts = Path(parts).resolve(strict=True)
        if (self.manifest.get('version') != 2 or self.manifest.get('snapshot_policy')
                != 'ONE_READONLY_REPEATABLE_READ_FULL_PK_NO_OTHER_PREDICATES'):
            raise EvidenceError('full_capture_policy_required')
        self.counts = {}

    def part(self, spec):
        path = self.parts / spec['reference']
        if path.is_symlink() or path.resolve(strict=True) != path or not path.is_relative_to(self.parts):
            raise EvidenceError('part_path_escape_or_symlink')
        if not 0 <= spec['bytes'] <= MAX_CHUNK or path.stat().st_size != spec['bytes']:
            raise EvidenceError('part_bytes')
        raw = path.read_bytes()
        if digest(raw) != spec['sha256']:
            raise EvidenceError('part_sha256')
        return json.loads(raw)

    def rows(self, table):
        spec = self.manifest['tables'][table]
        if spec.get('source_state') != 'PRESENT_FULL_EXHAUSTED':
            raise EvidenceError('full_table_not_present:' + table)
        previous, last, count, size = '0' * 64, None, 0, 0
        for sequence, chunk in enumerate(spec['chunks'], 1):
            body = self.part(chunk)
            if (body['table'] != table or body['snapshot_id'] != self.manifest['snapshot_id']
                    or body['asof_ms'] != self.manifest['asof_ms']
                    or body['sequence'] != sequence or body['previous_sha256'] != previous):
                raise EvidenceError('chunk_chain_or_scope')
            rows = body['rows']
            if not isinstance(rows, list) or len(rows) != chunk['rows'] or not rows:
                raise EvidenceError('chunk_row_count')
            ids = [row['id'] for row in rows]
            if (ids != sorted(set(ids)) or (last is not None and ids[0] <= last)
                    or [ids[0]] != chunk['first_key'] or [ids[-1]] != chunk['last_key']):
                raise EvidenceError('chunk_primary_keys')
            count += len(rows)
            size += chunk['bytes']
            for index, row in enumerate(rows, 1):
                yield row, {'table': table, 'part': chunk['reference'],
                            'part_sha256': chunk['sha256'], 'row_ordinal_1based': index}
            last, previous = ids[-1], chunk['sha256']
        end = self.part(spec['end_probe'])
        if (end['rows'] != [] or end['table'] != table
                or end['snapshot_id'] != self.manifest['snapshot_id']
                or end['asof_ms'] != self.manifest['asof_ms']
                or end['after_key'] != ([last] if last is not None else None)
                or count != spec['exact_export_rows'] or size != spec['exact_export_bytes']):
            raise EvidenceError('table_end_or_totals')
        self.counts[table] = count


def in_window(value, cutoff):
    try:
        at = stamp(value)
    except (EvidenceError, ValueError):
        return None
    if at > cutoff:
        return None  # A captured future-clock row is uncertain, not silently excluded.
    return SINCE <= at


def day(value):
    try:
        return stamp(value).astimezone(ET).date().isoformat()
    except (EvidenceError, ValueError):
        return 'UNKNOWN_DAY'


def census(capture):
    accounts = {row['id']: row for row, _ in capture.rows('broker_accounts')}
    strategies = {row['id']: row for row, _ in capture.rows('strategies')}
    asof = capture.manifest['asof_ms']
    cutoff = datetime.fromtimestamp(asof // 1000, UTC).replace(microsecond=(asof % 1000) * 1000)
    orders, pointers, selected, uncertain, issues = {}, {}, set(), [], []
    for order, pointer in capture.rows('broker_orders'):
        orders[order['id']], pointers[order['id']] = order, pointer
        account, strategy = accounts.get(order['broker_account_id']), strategies.get(order['strategy_id'])
        if account is None or strategy is None:
            issues.append({'order_id': order['id'], 'reason': 'account_or_strategy_missing'})
            continue
        if family(strategy['code']) is None or not str(account['name']).startswith('live:'):
            continue
        window = in_window(order.get('submitted_at'), cutoff)
        if window is not False:
            selected.add(order['id'])
        if window is None:
            uncertain.append({'order_id': order['id'], 'reason': 'submitted_at_UNKNOWN_since_membership'})
    fills, fill_pointers, unbound = defaultdict(list), {}, []
    fill_denominator, fill_count = Counter(), 0
    for fill, pointer in capture.rows('fills'):
        account, strategy = accounts.get(fill['broker_account_id']), strategies.get(fill['strategy_id'])
        if account is None or strategy is None:
            issues.append({'fill_id': fill['id'], 'reason': 'fill_scope_missing'})
            continue
        if family(strategy['code']) is None or not str(account['name']).startswith('live:'):
            continue
        window = in_window(fill.get('filled_at'), cutoff)
        if window is False:
            continue
        fill_count += 1
        fill_denominator['|'.join((day(fill.get('filled_at')), family(strategy['code']),
            account['provider'], session(fill.get('filled_at')), fill['side']))] += 1
        order = orders.get(fill['order_id'])
        if order is None or any(order.get(key) != fill.get(key) for key in (
                'strategy_id', 'broker_account_id', 'symbol', 'side')):
            issues.append({'fill_id': fill['id'], 'order_id': fill['order_id'], 'reason': 'fill_order_identity_mismatch'})
            unbound.append({'fill': fill, 'source_pointer': pointer})
            continue
        selected.add(order['id'])  # Include a pre-window parent with a fill INSIDE the window.
        fills[order['id']].append(fill)
        fill_pointers[fill['id']] = pointer
        if window is None:
            uncertain.append({'fill_id': fill['id'], 'reason': 'filled_at_UNKNOWN_since_membership'})
    wanted_intents = {orders[key]['intent_id'] for key in selected}
    intents = {row['id']: row for row, _ in capture.rows('trade_intents') if row['id'] in wanted_intents}
    cases, denominator = [], Counter()
    for key in sorted(selected):
        order = orders[key]
        account, strategy = accounts[order['broker_account_id']], strategies[order['strategy_id']]
        intent = intents.get(order['intent_id'])
        if intent is None:
            issues.append({'order_id': key, 'reason': 'intent_missing'})
        elif any(intent.get(field) != order.get(field) for field in ('symbol', 'strategy_id', 'broker_account_id')):
            issues.append({'order_id': key, 'reason': 'intent_identity_mismatch'})
        if strategy.get('execution_mode') != 'live':
            uncertain.append({'order_id': key, 'reason': 'live_account_strategy_mode_not_live'})
        group = (day(order.get('submitted_at')), family(strategy['code']), account['provider'],
                 session(order.get('submitted_at')), order['side'])
        denominator['|'.join(group)] += 1
        cases.append({'order': order, 'intent': intent, 'strategy_code': strategy['code'],
                      'account_name': account['name'], 'provider': account['provider'],
                      'physical_account_equivalence': 'UNKNOWN' if account.get('external_account_id') is None
                      else 'EXTERNAL_ID_PRESENT_NOT_INDEPENDENT_NATIVE_EQUIVALENCE',
                      'source_order_pointer': pointers[key], 'fills': fills[key],
                      'source_fill_pointers': {row['id']: fill_pointers[row['id']] for row in fills[key]},
                      'historical_input_status': 'UNKNOWN_QUOTES_NATIVE_ASOF_OWNER_AND_RECEIPT_TIMELINE'})
    return {'schema': 'MOM_ORB_CAPTURE_CENSUS_V1', 'manifest_sha256': capture.manifest_sha,
            'capture_asof_ms': capture.manifest['asof_ms'], 'since': SINCE.isoformat(),
            'source_table_counts': capture.counts, 'orders': len(cases),
            'fills': fill_count, 'linked_fills': sum(len(case['fills']) for case in cases),
            'unbound_fills': unbound,
            'denominator_order_day_bot_broker_session_side': dict(sorted(denominator.items())),
            'denominator_fill_day_bot_broker_session_side': dict(sorted(fill_denominator.items())),
            'uncertain_membership': uncertain, 'identity_issues': issues, 'cases': cases,
            'scope': 'CAPTURE_MEMBERSHIP_NOT_HISTORICAL_NATIVE_OR_DECISION_PROOF',
            'historical_replay_qualified': False}


def request_fields(case):
    order, intent = case['order'], case['intent']
    if intent is None or any(intent.get(field) != order.get(field) for field in (
            'side', 'symbol', 'strategy_id', 'broker_account_id')):
        raise EvidenceError('request_intent_preimage_missing_or_changed')
    # Stored metadata remains exactly captured. No missing field gets an assumed price/session.
    metadata = order['payload']
    if not isinstance(metadata, dict) or intent.get('intent_type') not in {'open', 'scale', 'close', 'cancel'}:
        raise EvidenceError('request_shape_unknown')
    return dict(client_order_id=order['client_order_id'], broker_account_name=case['account_name'],
                strategy_code=case['strategy_code'], symbol=order['symbol'], side=order['side'],
                intent_type=intent['intent_type'], quantity=number(order['quantity']),
                reason=intent['reason'], metadata=dict(metadata),
                order_type=order['order_type'], time_in_force=order['time_in_force'])


def project_requests(data, source_root, expected_head, census_sha):
    """Actual unchanged leaf formatters; NOT entry decisions, dispatch or fill simulation."""
    root = Path(source_root).resolve(strict=True)
    if (expected_head not in ROOTS or root != ROOTS[expected_head]
            or any(name.startswith('project_mai_tai') for name in sys.modules)):
        raise EvidenceError('one_clean_source_root_per_process_required')
    sys.path[:0] = [str(root / 'src'), str(root)]
    from project_mai_tai.broker_adapters.protocols import OrderRequest
    from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    rows = []
    for case in data['cases']:
        record = {'order_id': case['order']['id'], 'status': 'UNKNOWN',
                  'historical_decision': 'UNKNOWN', 'fills_reproduced': False}
        try:
            request = OrderRequest(**request_fields(case))
            if case['provider'] == 'schwab':
                adapter = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
                if str(request.metadata.get('exit_only_oco', '')).lower() in {'1', 'true', 'yes'}:
                    wire = adapter._build_exit_only_oco_payload(request)
                elif str(request.metadata.get('bracket', '')).lower() in {'1', 'true', 'yes'}:
                    raise EvidenceError('historical_bracket_flag_at_dispatch_UNKNOWN')
                else:
                    wire = adapter._build_order_payload(request)
            elif case['provider'] == 'webull':
                adapter = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
                if str(request.metadata.get('webull_exit_only_pair', '')).lower() in {'1', 'true', 'yes'}:
                    wire = adapter._build_exit_only_pair_payload(request)
                elif str(request.metadata.get('bracket', '')).lower() in {'1', 'true', 'yes'}:
                    raise EvidenceError('historical_bracket_flag_at_dispatch_UNKNOWN')
                else:
                    limit, stop, _, refusal = adapter._prepare_single_leg_prices(request=request,
                        order_type=adapter._order_type(request),
                        limit_price=adapter._meta_price(request, 'limit_price', 'reference_price'),
                        stop_price=adapter._meta_price(request, 'stop_price'))
                    wire = {'limit': str(limit) if limit is not None else None,
                            'stop': str(stop) if stop is not None else None, 'refusal': refusal}
            else:
                raise EvidenceError('provider_unknown')
            record.update(status='CAPTURED_REQUEST_FORMATTED_NOT_DISPATCHED', wire=wire,
                          side=request.side, quantity=str(request.quantity))
        except (EvidenceError, ValueError, KeyError, AttributeError, TypeError, RuntimeError, ArithmeticError) as error:
            record['reason'] = type(error).__name__ + ':' + str(error)[:180]
        rows.append(record)
    modules = {name: module.__file__ for name, module in sys.modules.items()
               if name.startswith('project_mai_tai.') and getattr(module, '__file__', None)}
    if any(not Path(path).resolve().is_relative_to(root / 'src') for path in modules.values()):
        raise EvidenceError('loaded_module_source_escape')
    return {'schema': 'MOM_ORB_FORMATTER_COMPONENT_V1', 'head': expected_head,
            'source_head_seal': 'MAIN_GIT_AND_WHOLE_SOURCE_PINS_REQUIRED',
            'census_sha256': census_sha,
            'manifest_sha256': data['manifest_sha256'], 'rows': rows, 'module_origins': modules,
            'fills_reproduced': False, 'historical_replay_qualified': False}


def compare_components(data, old, new, census_sha):
    if (old.get('schema') != 'MOM_ORB_FORMATTER_COMPONENT_V1'
            or new.get('schema') != 'MOM_ORB_FORMATTER_COMPONENT_V1'
            or old['head'] != OLD or new['head'] != NEW
            or old['manifest_sha256'] != data['manifest_sha256']
            or new['manifest_sha256'] != data['manifest_sha256']
            or old.get('census_sha256') != census_sha or new.get('census_sha256') != census_sha):
        raise EvidenceError('old_new_source_or_input_binding')
    expected = {case['order']['id'] for case in data['cases']}
    if len(expected) != len(data['cases']):
        raise EvidenceError('duplicate_census_order')
    indexed = []
    for report in (old, new):
        rows = report['rows']
        if len({row['order_id'] for row in rows}) != len(rows) or {row['order_id'] for row in rows} != expected:
            raise EvidenceError('denominator_changed_or_duplicate')
        indexed.append({row['order_id']: row for row in rows})
    changed, unknown = [], []
    for key in sorted(expected):
        before, after = indexed[0][key], indexed[1][key]
        if before['status'] == 'UNKNOWN' or after['status'] == 'UNKNOWN':
            unknown.append({'order_id': key, 'old': before, 'new': after})
        elif before != after:
            changed.append({'order_id': key, 'reason': 'ADAPTER_COMPONENT_OUTPUT_CHANGED', 'old': before, 'new': after})
    return {'schema': 'MOM_ORB_COMPONENT_COMPARISON_V1', 'orders': len(expected),
            'changed_rows': changed, 'unknown_rows': unknown,
            'historical_orders_replayed': 0, 'historical_fills_reproduced': 0,
            'historical_replay_qualified': False,
            'remaining': ['actual_signal_quote_owner_native_asof_inputs',
                          'actual_entry_exit_driver_callbacks_and_fill_receipts',
                          'causal_clock_and_bar_duration_for_one_bar_tolerance',
                          'shared_sync_native_read_contention_and_db_writer_cost']}


def compare_replays(data, old, new, census_sha):
    """Compare original-driver outputs; metadata alone never qualifies their inputs.

    Main binds each report/trace to its existing original-driver invocation. This
    comparator neither injects a fill nor turns controlled recording ports into
    historical ownership/native proof.
    """
    expected = {case['order']['id']: case for case in data['cases']}
    if len(expected) != len(data['cases']):
        raise EvidenceError('duplicate_census_order')
    legs = []
    for report, head in ((old, OLD), (new, NEW)):
        if (report.get('schema') != 'MOM_ORB_ORIGINAL_DRIVER_RECEIPT_V1'
                or report.get('head') != head or report.get('census_sha256') != census_sha
                or report.get('input_manifest_sha256') != old.get('input_manifest_sha256')
                or not report.get('input_manifest_sha256') or not report.get('source_inventory_sha256')):
            raise EvidenceError('original_driver_source_input_binding')
        rows = report['rows']
        if len(rows) != len(expected) or {row['order_id'] for row in rows} != set(expected):
            raise EvidenceError('original_driver_denominator')
        legs.append({row['order_id']: row for row in rows})
    changed, unknown, matched = [], [], 0
    for key, case in expected.items():
        before, after = legs[0][key], legs[1][key]
        missing = []
        for label, row, report in (('OLD', before, old), ('NEW', after, new)):
            if (report.get('mode') != 'RETAINED_HISTORY' or row.get('unknown_inputs') != []
                    or row.get('status') == 'UNKNOWN' or not row.get('driver_trace_sha256')):
                missing.append(label + '_historical_inputs_or_actual_driver_trace_UNKNOWN')
        if missing:
            unknown.append({'order_id': key, 'reasons': missing})
            continue
        reasons = []
        if before.get('status') != after.get('status'):
            reasons.append('DECISION_CHANGED')
        for field in ('side', 'client_order_id'):
            if field not in before or field not in after:
                missing.append(field + '_UNKNOWN')
            elif before[field] != after[field]:
                reasons.append(field.upper() + '_CHANGED')
        for field in ('quantity', 'price'):
            try:
                if number(before[field]) != number(after[field]):
                    reasons.append(field.upper() + '_CHANGED')
            except (KeyError, EvidenceError, ArithmeticError):
                missing.append(field + '_UNKNOWN')
        try:
            delta = abs((stamp(before['decision_at']) - stamp(after['decision_at'])).total_seconds())
            duration = after.get('bar_seconds')
            proven_bar = (type(duration) is int and 0 < duration <= 3600
                and before.get('bar_seconds') == duration
                and before.get('bar_duration_provenance') == after.get('bar_duration_provenance')
                == 'RETAINED_STRATEGY_PROFILE')
            if delta > 0 and not proven_bar:
                missing.append('one_bar_tolerance_UNKNOWN')
            elif delta > 0 and delta > duration:
                reasons.append('DECISION_TIME_BEYOND_ONE_BAR')
        except (KeyError, EvidenceError, ValueError):
            missing.append('decision_clock_UNKNOWN')
        actual = {row['id']: row for row in case['fills']}
        for label, row in (('OLD', before), ('NEW', after)):
            reproduced = row.get('fills')
            if (row.get('fill_reconstruction_basis') != 'ORIGINAL_CALLBACKS_RETAINED_REPORTS'
                    or not isinstance(reproduced, list)):
                missing.append(label + '_fill_reproduction_UNKNOWN')
                continue
            if len(reproduced) != len(actual) or {fill['id'] for fill in reproduced} != set(actual):
                reasons.append(label + '_ACTUAL_FILL_MEMBERSHIP_DIFF')
                continue
            for fill in reproduced:
                original = actual[fill['id']]
                if any(fill.get(field) != original.get(field) for field in ('order_id', 'side', 'symbol')):
                    reasons.append(label + '_ACTUAL_FILL_IDENTITY_DIFF')
                try:
                    if (number(fill['quantity']) != number(original['quantity'])
                            or number(fill['price']) != number(original['price'])
                            or stamp(fill['filled_at']) != stamp(original['filled_at'])):
                        reasons.append(label + '_ACTUAL_FILL_VALUES_DIFF')
                except (KeyError, EvidenceError, ValueError, ArithmeticError):
                    missing.append(label + '_fill_values_UNKNOWN')
        if reasons:
            changed.append({'order_id': key, 'reasons': reasons, 'old': before, 'new': after,
                            'source_pointer': case['source_order_pointer']})
        if missing:
            unknown.append({'order_id': key, 'reasons': missing})
        if not reasons and not missing:
            matched += 1
    return {'schema': 'MOM_ORB_ORIGINAL_DRIVER_COMPARISON_V1', 'orders': len(expected),
            'matched_output_rows_pending_input_review': matched, 'changed_rows': changed,
            'unknown_rows': unknown, 'capture_identity_issues': data.get('identity_issues', []),
            'capture_membership_unknowns': data.get('uncertain_membership', []),
            'unbound_fills': data.get('unbound_fills', []),
            'historical_replay_qualified': False,
            'qualification': 'MAIN_MUST_REVIEW_ACTUAL_DRIVER_INPUTS_TRACES_AND_ALL_ROWS_NOT_METADATA_ASSERTIONS'}


def write_new(path, value):
    path = Path(path)
    if (not path.is_absolute() or not path.is_relative_to('/private/tmp')
            or path.parent.resolve(strict=True) != path.parent
            or path.parent.stat().st_uid != os.getuid()
            or path.parent.stat().st_mode & 0o777 != 0o700):
        raise EvidenceError('fresh_private_0700_output_parent_required')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('census', 'project', 'compare', 'compare-replays'))
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--parts', type=Path)
    parser.add_argument('--census', type=Path)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--head', choices=(OLD, NEW))
    parser.add_argument('--old', type=Path)
    parser.add_argument('--new', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'census':
        result = census(Capture(args.manifest, args.parts))
    else:
        raw_census = args.census.read_bytes()
        data = json.loads(raw_census)
        if data.get('schema') != 'MOM_ORB_CAPTURE_CENSUS_V1' or data.get('manifest_sha256') != MANIFEST_SHA:
            raise EvidenceError('census_binding')
        if args.mode == 'project':
            result = project_requests(data, args.source_root, args.head, digest(raw_census))
        else:
            compare = compare_replays if args.mode == 'compare-replays' else compare_components
            result = compare(data, json.loads(args.old.read_text()), json.loads(args.new.read_text()), digest(raw_census))
    write_new(args.output, result)
    print(json.dumps({'artifact': str(args.output), 'sha256': digest(args.output.read_bytes()),
                      'historical_replay_qualified': False, 'mode': args.mode}))


if __name__ == '__main__':
    main()
