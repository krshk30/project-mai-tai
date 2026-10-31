"""Main-only cold/warm ORIGINAL captured-RR replay. Private outputs, never TODAY.

No SQL/provider/client imports or calls. Native/SQL delta/raw-pool cost remains
UNQUALIFIED. Source review and controlled tests precede Main's two invocations.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

from atrsimlive1_captured_rr import (
    CapturedRefusal, SCOPE, decode, verified_bytes, verify_closed_original,
    original_pairs, historical_model_input, QUOTE_REHYDRATION, SealedRoutes,
)

CAP = 512 * 1024 * 1024
RSS_CAP = 2 * 1024 * 1024 * 1024
STDOUT_CAP = 4 * 1024 * 1024
AUTHORITY = '5ebc5e902bcbe439a4ddcfe2db20ba016e8312fb9ec65557d6056ff635e21461'


def canonical(value):
    from atrsimlive1_collect import plain
    return json.dumps(plain(value), sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode()


def private_root(path, *, create=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise CapturedRefusal('literal_private_root_required')
    if create:
        path.mkdir(mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise CapturedRefusal('owned_0700_root_required')
    return path


def write_new(path, value):
    raw = canonical(value)
    if len(raw) > CAP:
        raise CapturedRefusal('historical_file_cap')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


def known_rows(document):
    found, seen = {}, set()
    for world, table in document['tables'].items():
        for item in table['rows']:
            key = (world, item['outcome_id'])
            if key in seen:
                raise CapturedRefusal('duplicate_historical_outcome')
            seen.add(key)
            if item['row']['pnl_label_known'] is True:
                found[key] = hashlib.sha256(canonical(item)).hexdigest()
    return found


def compare_known(original, actual):
    old, new = known_rows(original), known_rows(actual)
    if old != new:
        raise CapturedRefusal('original_known_dollar_row_bytes_changed')
    return old


def child(args):
    from atrsimlive1 import source_binding
    from atrsimlive1_incremental_cache import fingerprint, chunks, _pairs, _constant
    from atrsimlive1_historical_models import HistoricalModels, VERSION
    from atrsimlive1_models import produce
    from atrsimlive1_output import EvidenceWriter
    from atrsimlive1_line_route import parity_summary

    started = time.monotonic()
    source = source_binding(args.source_binding)
    required = {'tools/atrsimlive1_captured_rr.py', 'tools/atrsimlive1_historical_models.py',
                'tools/atrsimlive1_historical_replay.py'}
    if not required <= set(source['source_pins']):
        raise CapturedRefusal('all_new_historical_dependencies_must_be_pinned')
    manifest = decode(verified_bytes({'path': args.input_binding, 'sha256': args.input_sha256}))
    if manifest.get('c1_authority_sha256') != AUTHORITY:
        raise CapturedRefusal('explicit_C11251_historical_scope_authority_required')
    witness = verify_closed_original(manifest)
    binding = dict(version=VERSION, input_scope=SCOPE, capture=witness.capture_binding_sha256,
                   source_seal=fingerprint(source['source_pins']), authority=AUTHORITY)
    cache = private_root(args.namespace, create=args.phase == 'cold')
    output = private_root(args.output_dir)
    lockfd = os.open(cache / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    evidence = EvidenceWriter(output)
    try:
        if args.phase == 'cold':
            write_new(cache / 'binding.json', binding)
        else:
            names = {'.lock', 'binding.json', 'cold-complete.json', 'cold-joined.json',
                     *(symbol + '.json' for symbol in witness.symbols)}
            if {p.name for p in cache.iterdir()} != names or any(p.is_symlink() for p in cache.iterdir()):
                raise CapturedRefusal('exact_historical_namespace_population_required')
            if decode((cache / 'binding.json').read_bytes()) != binding:
                raise CapturedRefusal('cold_warm_same_exact_binding_required')
            cold = decode((cache / 'cold-complete.json').read_bytes())
            if cold.get('binding') != binding or cold.get('symbols') != list(witness.symbols):
                raise CapturedRefusal('physically_completed_full_cold_phase_required')
            joined = decode((cache / 'cold-joined.json').read_bytes())
            if (joined.get('binding') != binding or joined.get('child_joined') is not True
                    or joined.get('pipes_closed') is not True or joined.get('returncode') != 0):
                raise CapturedRefusal('actual_supervisor_cold_child_join_required')
        totals, comparisons, operations, hashes = {}, [], [], []
        count_known, symbol_count, cache_bytes = 0, 0, sum(p.stat().st_size for p in cache.iterdir())
        for original_input, original_model in original_pairs(witness):
            if time.monotonic() - started >= 240:
                raise TimeoutError('historical_child_240s')
            symbol, = original_input['symbols']
            routes = SealedRoutes(witness, original_input, original_model)
            data = historical_model_input(original_input)
            policies = dict(execution_rules_sha256=original_model['producer']['policy_sha256'],
                null_policy=original_model['producer']['stored_null_policy'],
                same_sip_policy=original_model['producer']['same_sip_policy'])
            run = SimpleNamespace(binding=dict(binding, policies=policies), audit=[], inputs={symbol: [
                dict(scope=SCOPE, original_snapshot=witness.snapshot_id, fresh_SQL_performed=False)]})
            saved_path = cache / (symbol + '.json')
            previous = {}
            if args.phase == 'warm':
                raw = verified_bytes({'path': str(saved_path), 'sha256': cold['cache_files'][symbol]['sha256']}, cap=CAP)
                previous = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
            run.model_controller = HistoricalModels(run, witness, data, previous)
            actual = produce(data, policy_path=manifest['files']['policy']['path'], producer=source,
                same_sip_policy=policies['same_sip_policy'], null_policy=policies['null_policy'],
                incremental=run, historical_routes=routes)
            known = compare_known(original_model, actual)
            count_known += len(known)
            hashes.extend((symbol, world, outcome, checksum) for (world, outcome), checksum in sorted(known.items()))
            for world, table in actual['tables'].items():
                totals[world] = totals.get(world, 0) + len(table['rows'])
            comparisons.extend(actual['producer']['c1_buy_parity'])
            operations.extend(dict(symbol=symbol, **item) for item in run.audit)
            evidence.append('input', original_input, symbol=symbol)
            evidence.append('model', actual, symbol=symbol)
            if args.phase == 'cold':
                raw = ''.join(chunks(run.model_controller.next)).encode()
                if cache_bytes + len(raw) > CAP - STDOUT_CAP:
                    raise CapturedRefusal('aggregate_historical_cache_512MiB_cap')
                # Preserve the existing safe typed cache scalar encoding.
                fd = os.open(saved_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                cache_bytes += len(raw)
            symbol_count += 1
            del actual, data, original_input, original_model, run
        parity = parity_summary('2026-10-28', comparisons)
        expected = {'FIRSTBAR': 49, 'FIRSTBAR_X3': 49, 'AGG_ENTRY': 5, 'LIVE:ACTUAL': 5,
                    'TODAY:SUPPLIED': 49, 'VOLBODY:QUEUE_AWARE': 49, 'VOLBODY:OPTIMISTIC': 49}
        if symbol_count != 23 or count_known != 106 or totals != expected or parity['blocks_qualification']:
            raise CapturedRefusal('all23_all6BUY_all255_and106_known_required')
        source_binding(args.source_binding)
        result = dict(schema=VERSION, phase=args.phase, binding=binding, symbols=list(witness.symbols),
            original_snapshot=witness.snapshot_id, original_asof=witness.source_asof,
            elapsed_seconds=time.monotonic() - started, original_known_dollar_rows=count_known,
            original_known_rows_sha256=fingerprint(hashes), rows_by_table=totals,
            comparisons=parity, operations=operations, raw_route_packet='UNKNOWN_NOT_RETAINED',
            input_rehydration=QUOTE_REHYDRATION,
            fresh_SQL_performed=False, since_SQL_qualified=False, raw_pool_qualified=False,
            DB_cost_qualified=False, native_execution_qualified=False,
            generated_at=datetime.now(timezone.utc).isoformat())
        evidence.append('run', result)
        result['evidence'] = evidence.seal()
        if args.phase == 'cold':
            result['cache_files'] = {symbol: dict(sha256=hashlib.sha256((cache / (symbol + '.json')).read_bytes()).hexdigest())
                                     for symbol in witness.symbols}
            write_new(cache / 'cold-complete.json', result)
            if sum(p.stat().st_size for p in cache.iterdir()) > CAP:
                raise CapturedRefusal('aggregate_historical_cache_512MiB_cap')
        if args.phase == 'warm' and result['original_known_rows_sha256'] != cold['original_known_rows_sha256']:
            raise CapturedRefusal('cold_warm_known_rows_byte_hash_mismatch')
        result['elapsed_seconds'] = time.monotonic() - started
        result['warm_le_60_seconds'] = result['elapsed_seconds'] <= 60 if args.phase == 'warm' else None
        write_new(output / 'phase.json', result)
        print(canonical(result).decode(), flush=True)
    finally:
        evidence.close()
        fcntl.flock(lockfd, fcntl.LOCK_UN)
        os.close(lockfd)


def supervise(args):
    """One owned child, actual observed RSS, hard wall/output caps and join."""
    output = private_root(args.output_dir, create=True)
    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], '--child']
    begin = time.monotonic()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    stdout, stderr, peak, samples = bytearray(), bytearray(), 0, 0
    failure = None
    try:
        for stream, buffer in ((proc.stdout, stdout), (proc.stderr, stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, buffer)
        while selector.get_map():
            if time.monotonic() - begin >= 240:
                raise TimeoutError('historical_child_240s')
            if proc.poll() is None:
                sample = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', str(proc.pid)],
                    capture_output=True, timeout=1, check=False)
                if sample.returncode == 0 and sample.stdout.strip():
                    rss = int(sample.stdout.strip()) * 1024
                    samples += 1
                    peak = max(peak, rss)
                    if peak > RSS_CAP:
                        raise CapturedRefusal('observed_child_RSS_2GiB_cap')
                elif proc.poll() is None:
                    raise CapturedRefusal('actual_RSS_observation_unavailable')
            for key, _ in selector.select(.25):
                raw = os.read(key.fileobj.fileno(), 65536)
                if not raw:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(raw)
                if len(key.data) > (STDOUT_CAP if key.data is stdout else 65536):
                    raise CapturedRefusal('historical_phase_output_cap')
        proc.wait(timeout=max(.001, 240 - (time.monotonic() - begin)))
    except BaseException as error:
        failure = type(error).__name__
    finally:
        selector.close()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)
        proc.stdout.close()
        proc.stderr.close()
        for name, raw in (('stdout', stdout), ('stderr', stderr)):
            fd = os.open(output / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
        write_new(output / 'outer.json', dict(phase=args.phase, returncode=proc.returncode,
            failure_type=failure, child_pid=proc.pid, child_joined=proc.poll() is not None,
            pipes_closed=proc.stdout.closed and proc.stderr.closed, observed_peak_rss_bytes=peak,
            observed_rss_samples=samples,
            elapsed_seconds=time.monotonic() - begin, outer_cap_seconds=260,
            warm_le_60_seconds=(time.monotonic() - begin <= 60) if args.phase == 'warm' else None,
            stdout_sha256=hashlib.sha256(stdout).hexdigest(), stderr_sha256=hashlib.sha256(stderr).hexdigest()))
    if failure or proc.returncode != 0 or time.monotonic() - begin > 260 or samples == 0:
        raise SystemExit(2)
    if args.phase == 'cold':
        phase = decode((output / 'phase.json').read_bytes())
        write_new(private_root(args.namespace) / 'cold-joined.json', dict(binding=phase['binding'],
            child_joined=True, pipes_closed=proc.stdout.closed and proc.stderr.closed,
            returncode=proc.returncode))
    if args.phase == 'warm' and (time.monotonic() - begin > 60
            or decode((output / 'phase.json').read_bytes())['warm_le_60_seconds'] is not True):
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-binding', required=True)
    parser.add_argument('--input-sha256', required=True)
    parser.add_argument('--source-binding', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--phase', choices=('cold', 'warm'), required=True)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args)
    else:
        supervise(args)


if __name__ == '__main__':
    main()
