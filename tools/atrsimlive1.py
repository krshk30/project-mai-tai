#!/usr/bin/env python3
"""Separate TODAY oneshot. No trading services or SIM database writes.

Source-only candidate: existing engine coverage still refuses incomplete watch/
tape evidence. Do not install as economically qualified without the C1 input
scope review and two actual known SIM entry/exit witnesses.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from atrsimlive1_runtime import bounded_child, decode, exclusive_run, publish
from atrsimlive1_progress import stage
from atrsimlive1_route_log import read_today_routes


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SOURCE = {
    "tools/atrsimlive1.py", "tools/atrsimlive1_runtime.py", "tools/atrsimlive1_collect.py",
    "tools/atrsimlive1_queries.py", "tools/atrsimlive1_models.py", "tools/atrsimlive1_coverage.py",
    "tools/atrsimlive1_data_coverage.py",
    "tools/atrsimlive1_line_route.py",
    "tools/atrsimlive1_progress.py",
    "tools/atrsimlive1_route_log.py",
    "tools/atrsimlive1_working_windows.py", "tools/atrsimlive1_window_census.py",
    "tools/atrsimlive1_window_collect.py", "tools/atrsimlive1_window_coverage.py",
    "tools/atrsimlive1_window_view.py",
    "tools/atrsimlive1_null_policy.py",
    "tools/atrsimlive1_output.py",
    "tools/atrsimlive1_incremental_cache.py", "tools/atrsimlive1_incremental_codec.py",
    "tools/atrsimlive1_incremental_reads.py", "tools/atrsimlive1_incremental_collect.py",
    "tools/atrsimlive1_incremental_models.py",
    "tools/atrsimlive1_incremental_file.py",
    "tools/atrsimlive1_incremental_container.py",
    "tools/atrsimlive1_reuse_components.py", "tools/atrsimlive1_cache_pool.py",
    "tools/atrsimlive1_change_proof.py",
    "tools/atrsimlive1_partial.py", "tools/atrsimlive1_partial_run.py",
    "tools/atrsimlive1_failure.py", "tools/atrsimlive1_failure_literals.json",
    "tools/atrsimlive1_namespace.py",
    "src/project_mai_tai/atr_country_route.py",
    "src/project_mai_tai/services/market_capture_app.py", "src/project_mai_tai/db/models.py",
    *("src/project_mai_tai/atr_simulation/" + name for name in (
        "types.py", "engine.py", "entry.py", "exits.py", "fill_models.py", "volume_body.py",
        "execution_eligibility.py", "firstbar.py", "firstbar_replay.py", "firstbar_exits.py",
        "firstbar_live_baseline.py", "firstbar_study.py", "firstbar_target_rule.py",
        "read_model.py", "repository.py", "cohort_provenance.py", "cp_sim_display.py")),
    "src/project_mai_tai/momentum_paper/conditions.py",
    "src/project_mai_tai/broker_adapters/target_price.py",
    "src/project_mai_tai/backtest/atr_oracle.py",
}
REQUIRED_SOURCE.update("src/project_mai_tai/atr_simulation/" + path.name
                       for path in (ROOT / "src/project_mai_tai/atr_simulation").glob("*.py"))


def source_binding(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("source_manifest_cap")
    manifest = decode(raw)
    if (manifest.get("schema") != "ATRSIMLIVE1_REVIEWED_SOURCE_V1"
            or not re.fullmatch("[0-9a-f]{40}", manifest.get("head", ""))
            or not isinstance(manifest.get("files"), dict) or not manifest["files"]):
        raise ValueError("external_reviewed_source_binding_required")
    if not REQUIRED_SOURCE <= set(manifest["files"]):
        raise ValueError("all_loadbearing_source_pins_required")
    for relative, checksum in manifest["files"].items():
        name = Path(relative)
        if (name.is_absolute() or ".." in name.parts or not name.parts
                or re.fullmatch("[0-9a-f]{64}", checksum) is None):
            raise ValueError("bounded_source_path_required")
        actual = ROOT / name
        if actual.is_symlink() or not actual.resolve().is_relative_to(ROOT):
            raise ValueError("source_path_escape")
        if hashlib.sha256(actual.read_bytes()).hexdigest() != checksum:
            raise ValueError("reviewed_source_bytes_changed")
    return dict(head=manifest["head"], source_pins=manifest["files"],
                source_manifest_sha256=hashlib.sha256(raw).hexdigest(),
                source_attestation="external Main binding plus actual file hashes; no native qualification")


def country_route_projection(path, expected_sha256):
    """One immutable local evidence read; unavailable input grants no route."""
    if path is None and expected_sha256 is None:
        return None, "NOT_SUPPLIED"
    if (not isinstance(path, str) or not Path(path).is_absolute()
            or not isinstance(expected_sha256, str)
            or re.fullmatch("[0-9a-f]{64}", expected_sha256) is None):
        return None, "INVALID_BINDING"
    try:
        literal = Path(path)
        if ".." in literal.parts or any(part.is_symlink() for part in (literal, *literal.parents)):
            return None, "UNREADABLE_OR_INVALID"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_size > 2 * 1024 * 1024
                    or before.st_uid not in (0, os.geteuid()) or before.st_mode & 0o022):
                return None, "FILE_BOUND_OR_SHAPE"
            raw = stream.read(2 * 1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if (identity(before) != identity(after) or len(raw) != before.st_size
                or hashlib.sha256(raw).hexdigest() != expected_sha256):
            return None, "CHANGED_OR_HASH_MISMATCH"
        packet = decode(raw)
        if (not isinstance(packet, dict)
                or packet.get("schema") != "ATRSIMLIVE1_COUNTRY_LOG_PROJECTION_V1"):
            return None, "INVALID_PROJECTION_SCHEMA"
        return packet, "BOUND_LOCAL_PROJECTION"
    except (OSError, ValueError, UnicodeError):
        return None, "UNREADABLE_OR_INVALID"


INCREMENTAL_VERSION = "C1_20261028_200908_ATRSIM_INCREMENTAL_V1"


@contextmanager
def incremental_context(args, producer, conservative):
    """Default opens/imports nothing; failed child never permits publication.

    Cache commit happens only after complete roster/models, sealed evidence and
    post-source validation. Compact bytes leave the child only after cache
    commit; parent still requires successful physical child exit to publish.
    """
    if args.incremental_version is None:
        yield None
        return
    from atrsimlive1_incremental_cache import INCREMENTAL_V1, FEATURE_PROFILE, FEATURE_NAMES, fingerprint
    from atrsimlive1_incremental_collect import incremental_session
    from atrsimlive1_namespace import namespace_root
    from atrsimlive1_window_collect import POLICY_SHA

    if INCREMENTAL_V1 != INCREMENTAL_VERSION or conservative is None:
        raise ValueError('explicit_reviewed_incremental_and_same_sip_policy_required')
    binding = dict(version=INCREMENTAL_V1, input_scope='CURRENT_RR',
        trading_day=datetime.now(timezone.utc).astimezone(ZoneInfo('America/New_York')).date().isoformat(),
        source_seal=fingerprint(producer['source_pins']),
        policies=dict(execution_rules_sha256=POLICY_SHA,
            null_policy=args.stored_null_policy, same_sip_policy=conservative))
    selected_features = {name: getattr(args, 'cache_' + name, False) for name in FEATURE_NAMES}
    if any(selected_features.values()):
        binding['features'] = dict(version=FEATURE_PROFILE, **selected_features)
    retention = getattr(args, 'cache_retention_read_lease', False)
    if retention:
        # Main alone composes its reviewed C2 module; absent/unpinned is a
        # refusal, never a successful no-op or an invented permission.
        relative = 'tools/atrsimlive1_namespace_retention.py'
        if relative not in producer['source_pins']:
            raise ValueError('reviewed_C2_reader_lease_source_pin_required')
        namespace = namespace_root(args.incremental_cache_root, binding, retention_enabled=True)
    else:
        namespace = namespace_root(args.incremental_cache_root, binding)
    with incremental_session(namespace, binding, version=args.incremental_version, retention_enabled=retention) as run:
        yield run
        stage('CACHE_SAVE', '+')
    stage('CACHE_SAVE', '-')


def _run_main(argv=None, *, _failure, _child_wall_seconds=240,
              _publication_deadline=None, _child_closure_observer=None):
    from atrsimlive1_null_policy import NULL_REGULAR_V1

    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--policy")
    parser.add_argument("--stored-null-policy", choices=(NULL_REGULAR_V1,))
    parser.add_argument("--incremental-version", choices=(INCREMENTAL_VERSION,))
    parser.add_argument("--incremental-cache-root")
    parser.add_argument("--cache-closed-components", action="store_true")
    parser.add_argument("--cache-raw-pool", action="store_true")
    parser.add_argument("--cache-since-last-run", action="store_true")
    parser.add_argument("--deadline-partial-seconds", type=float)
    parser.add_argument("--cache-retention-read-lease", action="store_true")
    parser.add_argument("--evidence-root")
    parser.add_argument("--evidence-run-dir")
    parser.add_argument("--capture-log", default="/var/log/project-mai-tai/market-capture.log")
    parser.add_argument("--country-route-projection")
    parser.add_argument("--country-route-sha256")
    parser.add_argument("--dsn-env", choices=("MAI_TAI_DATABASE_URL", "DATABASE_URL"), required=True)
    parser.add_argument("--destination", default="/var/lib/project-mai-tai/atr-simulation/today/current.json")
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args(argv)
    if args.stored_null_policy is not None and args.policy is None:
        parser.error("--stored-null-policy requires the retained --policy")
    if (args.child and args.evidence_root is not None
            or not args.child and args.evidence_run_dir is not None):
        parser.error("parent accepts --evidence-root; child accepts only its new --evidence-run-dir")
    if (args.incremental_version is None) != (args.incremental_cache_root is None):
        parser.error("incremental version and private cache root must be supplied together")
    if (args.cache_closed_components or args.cache_raw_pool or args.cache_since_last_run
            or args.cache_retention_read_lease) and args.incremental_version is None:
        parser.error("cache feature switches require the reviewed incremental version")
    if args.deadline_partial_seconds is not None:
        import math
        if (not math.isfinite(args.deadline_partial_seconds) or not 0 < args.deadline_partial_seconds < 240
                or args.incremental_version is None):
            parser.error("deadline partial mode requires reviewed incremental mode and soft budget below240s")
    if args.incremental_version is not None:
        if args.policy is None or args.stored_null_policy is None:
            parser.error("incremental mode requires explicit retained policy and stored-NULL policy")
        if (args.evidence_run_dir if args.child else args.evidence_root) is None:
            parser.error("incremental mode requires per-run sealed compact evidence")
        root = Path(args.incremental_cache_root)
        if not root.is_absolute() or '..' in root.parts:
            parser.error("incremental cache root must be literal absolute private path")
    _failure['role'] = 'child' if args.child else 'parent'
    if args.child:
        _failure['directory'] = args.evidence_run_dir
    elif args.evidence_root is not None:
        from atrsimlive1_runtime import private_directory
        root = Path(args.evidence_root)
        if not root.is_absolute() or '..' in root.parts:
            raise ValueError('literal_absolute_evidence_root_required')
        root = private_directory(root)
        if root.stat().st_mode & 0o777 != 0o700:
            raise ValueError('owned_0700_evidence_root_required')
        _failure['directory'] = tempfile.mkdtemp(prefix='run-', dir=root)
    producer = source_binding(args.source_manifest)
    if args.child:
        sys.path.insert(0, str(ROOT / "src"))
        from atrsimlive1_collect import collect, dsn_from_environment, encoded
        from atrsimlive1_models import produce, combine

        if args.country_route_projection is None and args.country_route_sha256 is None:
            route_packet, route_status = read_today_routes()
        else:
            route_packet, route_status = country_route_projection(
                args.country_route_projection, args.country_route_sha256)
        producer["country_route_projection_status"] = route_status
        conservative = None
        if args.policy is not None:
            from project_mai_tai.atr_simulation.same_sip_binding import verify_same_sip_files
            conservative = verify_same_sip_files()
        with incremental_context(args, producer, conservative) as incremental:
            def model(data):
                cache_options = {} if incremental is None else {'incremental': incremental}
                return produce(data, policy_path=args.policy, producer=producer, route_packet=route_packet,
                    same_sip_policy=conservative, null_policy=args.stored_null_policy, **cache_options)
            options = dict(dsn=dsn_from_environment(args.dsn_env), capture_log_path=args.capture_log,
                           working_policy_path=args.policy, null_policy=args.stored_null_policy)
            if incremental is not None:
                options['incremental'] = incremental
            if args.deadline_partial_seconds is not None:
                from atrsimlive1_incremental_cache import fingerprint
                from atrsimlive1_partial_run import PartialWriter

                partial = PartialWriter(args.evidence_run_dir, source_seal=fingerprint(producer['source_pins']),
                    budget_seconds=args.deadline_partial_seconds)
                def process(data):
                    return partial.process(data, model)
                collect(processor=process, deadline_partial=partial, **options)
                incremental.completed_model_roster = tuple(partial.completed)
                source_binding(args.source_manifest)
                raw = encoded(partial.finish())
            elif args.evidence_run_dir is not None:
                from atrsimlive1_output import EvidenceWriter, project_component, compact_document, encode_compact
                with EvidenceWriter(args.evidence_run_dir) as evidence:
                    def process(data):
                        symbols = data['symbols']
                        if not isinstance(symbols, list) or len(symbols) != 1:
                            raise ValueError('one_original_symbol_packet_required')
                        evidence.append('input', data, symbol=symbols[0])
                        original = model(data)
                        evidence.append('model', original, symbol=symbols[0])
                        return project_component(original)
                    began = time.monotonic()
                    outputs = collect(processor=process, **options)
                    elapsed = time.monotonic() - began
                    source_binding(args.source_manifest)
                    evidence.append('run', dict(source=producer, stored_null_policy=args.stored_null_policy,
                        same_sip_policy=conservative, collect_models_evidence_seconds=elapsed))
                    began = time.monotonic()
                    sealed = evidence.seal()
                    seal_seconds = time.monotonic() - began
                stage('COMBINE', '+')
                document = compact_document(outputs, evidence=sealed,
                    timings=dict(collect_models_evidence_seconds=elapsed, evidence_seal_seconds=seal_seconds))
                stage('COMBINE', '-')
                source_binding(args.source_manifest)
                stage('ENCODE', '+')
                raw = encode_compact(document)
                stage('ENCODE', '-', len(raw))
            else:
                outputs = collect(processor=model, **options)
                stage("COMBINE", "+")
                document = combine(outputs)
                stage("COMBINE", "-")
                source_binding(args.source_manifest)  # Actual post-computation byte check.
                stage("ENCODE", "+")
                raw = encoded(document)
                stage("ENCODE", "-", len(raw))
        sys.stdout.buffer.write(raw)
        return 0
    destination = Path(args.destination)
    with exclusive_run(destination.parent):
        command = [sys.executable, str(Path(__file__).resolve()), "--child",
                   "--source-manifest", args.source_manifest, "--dsn-env", args.dsn_env]
        if args.evidence_root is not None:
            command += ['--evidence-run-dir', _failure['directory']]
        if args.policy is not None:
            command += ["--policy", args.policy]
        if args.stored_null_policy is not None:
            command += ["--stored-null-policy", args.stored_null_policy]
        command += ["--capture-log", args.capture_log]
        if args.country_route_projection is not None:
            command += ["--country-route-projection", args.country_route_projection]
        if args.country_route_sha256 is not None:
            command += ["--country-route-sha256", args.country_route_sha256]
        if args.incremental_version is not None:
            command += ["--incremental-version", args.incremental_version,
                        "--incremental-cache-root", args.incremental_cache_root]
        for name in ('closed-components', 'raw-pool', 'since-last-run'):
            if getattr(args, 'cache_' + name.replace('-', '_')):
                command.append('--cache-' + name)
        if args.cache_retention_read_lease:
            command.append('--cache-retention-read-lease')
        if args.deadline_partial_seconds is not None:
            from atrsimlive1_incremental_cache import fingerprint
            from atrsimlive1_partial_run import run_key, finalize, record_timeout
            from atrsimlive1_partial import observe_closed_child
            from subprocess import TimeoutExpired

            command += ['--deadline-partial-seconds', str(args.deadline_partial_seconds)]
            source_seal = fingerprint(producer['source_pins'])
            run_id = run_key(_failure['directory'], source_seal)
            closures = []
            def observed(process):
                closures.append(observe_closed_child(process, run_id=run_id,
                    source_seal=source_seal, allow_failed=True))
            try:
                raw, seconds = bounded_child(command, seconds=240, closure_observer=observed)
            except (TimeoutError, TimeoutExpired) as error:
                source_binding(args.source_manifest)
                if len(closures) != 1:
                    raise ValueError('one_actual_closed_child_required_for_partial') from None
                # Preserve the ACTUAL deadline failure before any recovery.
                # A missing/unfinished roster or component still refuses.
                record_timeout(_failure['directory'], closure=closures[0], source_seal=source_seal,
                    exception_class=type(error).__name__)
                partial = finalize(_failure['directory'], source_seal=source_seal, closure=closures[0],
                    stop_reason='ACTUAL_CHILD_WALL_TIMEOUT')
            else:
                source_binding(args.source_manifest)
                if len(closures) != 1:
                    raise ValueError('one_actual_closed_child_required_for_partial')
                partial = finalize(_failure['directory'], source_seal=source_seal, closure=closures[0],
                    stop_reason='COOPERATIVE_CHILD_EXIT', child_document=decode(raw))
            # Explicit private receipt only. Last complete current.json stays
            # untouched, even if every symbol completed in this new arm.
            print(json.dumps(dict(event='ATRSIMLIVE1_PRIVATE_PARTIAL_RECEIPT',
                status=partial['status'], completed=len(partial['completed_symbols']),
                pending=len(partial['pending_symbols']), whole_population_pnl=None,
                child_returncode=closures[0]['returncode'], native_execution_qualified=False)))
            return 2  # UNKNOWN/partial diagnostic, never a full-run PASS.
        if (_child_wall_seconds == 240 and _publication_deadline is None
                and _child_closure_observer is None):
            raw, seconds = bounded_child(command, seconds=240)
        else:
            if _publication_deadline is not None:
                # Refresh-only: account for ALL actual pre-child setup time,
                # retaining four seconds for the original bounded physical join.
                _child_wall_seconds = min(_child_wall_seconds,
                    _publication_deadline - time.monotonic() - 4.0)
                if _child_wall_seconds <= 0:
                    raise TimeoutError('refresh_pre_child_deadline')
            raw, seconds = bounded_child(command, seconds=_child_wall_seconds,
                                         closure_observer=_child_closure_observer)
        source_binding(args.source_manifest)
        document = decode(raw)
        expected = 'ATRSIMLIVE1_TODAY_COMPACT_V2' if args.evidence_root is not None else 'ATRSIMLIVE1_TODAY_V1'
        if document.get('schema') != expected:
            raise ValueError('explicit_requested_output_schema_required')
        if _publication_deadline is None:
            size = publish(document, destination, datetime.now(timezone.utc))
        else:
            size = publish(document, destination, datetime.now(timezone.utc),
                           deadline_monotonic=_publication_deadline)
        print(json.dumps(dict(event="ATRSIMLIVE1_FILE_WRITTEN", seconds=seconds,
                              bytes=size, native_execution_qualified=False)))
    return 0


def main(argv=None):
    """Keep the original failure and expose only the private receipt token."""
    failure = dict(directory=None, role='parent')
    try:
        return _run_main(argv, _failure=failure)
    except Exception as error:
        token = dict(exception_class='OTHER', evidence_sha256=None)
        try:
            from atrsimlive1_failure import exception_class, failure_token, write_failure
            from atrsimlive1_runtime import ChildFailure
            token['exception_class'] = exception_class(error)
            if isinstance(error, ChildFailure):
                validated = failure_token(json.dumps(error.child_diagnostic).encode())
                if validated is not None:
                    token = validated
            elif failure['directory'] is not None:
                token = write_failure(error, failure['directory'], role=failure['role'])
        except Exception:
            pass  # Receipt unavailable remains SHA=None; never print raw fallback.
        try:
            error._atrsim_failure_token = token
        except Exception:
            pass
        try:
            print(json.dumps(token, sort_keys=True), flush=True)
        except Exception:
            pass  # Broken stdout cannot replace the original failure.
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # DB/SDK/DSN/identities and raw error strings never enter a public log.
        print(json.dumps(dict(event="ATRSIMLIVE1_UNAVAILABLE", exc=type(error).__name__,
                              publication_state="UNPROVEN_ON_FAILURE")), file=sys.stderr)
        raise SystemExit(1)
