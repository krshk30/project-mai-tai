"""C12139 mode of the existing materializer: file sizing, shards, complete iterator.

No engine, role, lease, provider, CLI, retry or permission constructor. Main owns
all actual source/target/execution acceptance. Limits are serialized-byte bounds,
not an assertion about Python allocator RSS.
"""

from hashlib import sha256
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat

from sqlalchemy import Float, Numeric, text

from tools import l12_original122_retained_materialization as material

CASE_LIMIT = 64 * 1024 * 1024
SOURCE_LIMIT = 32 * 1024 * 1024
FREE_RESERVE = 5 * 1024 * 1024 * 1024
SHARD_VERSION = "ORIGINAL122_FULL_SOURCE_ROWS_WITH_EXACT_RESTORED_COMPARISON_V2"
MANIFEST_VERSION = "ORIGINAL122_COMPLETE_SHARDS_AFTER_RRRO_ROLLBACK_V2"
MEASURE_VERSION = "ORIGINAL122_EXACT_EIGHT_SOURCE_SELECTION_SIZING_V1"


def _hash():
    value = sha256()
    value.update(b"[")
    return value


def _append_hash(digest, count, row):
    if count:
        digest.update(b",")
    digest.update(material.canonical(row))


def _finish_hash(digest):
    value = digest.copy()
    value.update(b"]")
    return value.hexdigest()


def _prefix(case):
    return b'{"version":' + material.canonical(SHARD_VERSION) + b',"case":' + material.canonical(case) + b',"rows":{'


def _table_prefix(name, index):
    return (b"," if index else b"") + material.canonical(name) + b":["


def _suffix(readback):
    return b'},"readback_sha256":' + material.canonical(readback) + b',"permission_granted":false,"real_objects_constructed":false}'


def _item(name, case, source, reference, *, restored=None):
    # Full immutable row ONCE plus every restored Numeric representation. All
    # other columns are already strictly equal, so this losslessly reconstructs
    # the complete readback row without a second large payload copy.
    ref = {k: v for k, v in reference.items() if k != "numeric_equivalent_columns"}
    ref["numeric_comparison_columns"] = sorted(col.name for col in material.TABLES[name].columns
        if isinstance(col.type, Numeric) and not isinstance(col.type, Float))
    numeric = {column: (restored[column] if restored is not None else material.normalized(
        material.typed_value(material.TABLES[name].c[column], source[column])))
        for column in ref["numeric_comparison_columns"]}
    reconstructed = dict(source, **numeric)
    digest = sha256(material.canonical(reconstructed)).hexdigest()
    if restored is not None and digest != reference["restored_row_sha256"]:
        raise ValueError("full_readback_row_reconstruction_changed")
    ref["restored_row_sha256"] = digest
    return dict(row=source, restored_numeric_values=numeric, reference=ref,
        chronology=material.classify_retained_row(name, source, case))


def _manifest(plan, descriptors, source_sha, measure_sha):
    return dict(version=MANIFEST_VERSION, status="COMPLETE122_AFTER_ALL_RESELECTS_AND_ROLLBACK_CLOSE",
        original_cases=122, omitted_cases=0, source_sha256=material.EXPORT_SHA,
        source_manifest_sha256=source_sha, selection_plan_sha256=sha256(material.canonical(plan)).hexdigest(),
        sizing_sha256=measure_sha, cases=descriptors, real_objects_constructed=False,
        permission_granted=False, historical_native_permission="UNKNOWN_NOT_STORED")


def _descriptors(plan, measurements):
    return [dict(case_id=case["case_id"], reference=f"{index:03d}.json", bytes=measurement["bytes"],
        sha256="0" * 64) for index, (case, measurement) in enumerate(zip(plan["cases"], measurements))]


def _source_rows(name, manifest, reader, cache):
    previous, total = None, 0
    table = material.TABLES[name]
    for descriptor in manifest["tables"][name]["chunks"]:
        cache.pop("part", None)
        part = material.read_part(reader, descriptor, limit=SOURCE_LIMIT)
        cache.update(reference=descriptor["reference"], part=part)
        if part["table"] != name or len(part["rows"]) != descriptor["rows"]:
            raise ValueError("source_part_table_or_count_changed")
        for row in part["rows"]:
            key = tuple(row[col.name] for col in table.primary_key.columns)
            if previous is not None and key <= previous:
                raise ValueError("source_full_PK_order_changed")
            if not tuple(descriptor["first_key"]) <= key <= tuple(descriptor["last_key"]):
                raise ValueError("source_part_PK_bounds_changed")
            previous, total = key, total + 1
            yield row
        del part
    if total != manifest["tables"][name]["exact_export_rows"]:
        raise ValueError("source_full_table_membership_changed")


def size_original122_source(original_raw, *, source_manifest_raw, source_plan, source_reader,
        clock_references, maximum_rows_per_query, source_guard):
    """ONE file-only scan of each required table; no DB or output file creation.

    Measures the exact source-shaped shard encoding, all refs and manifest. Raw
    restored digests have fixed64-byte hex length, so do not require guessing
    actual PG formatting. SQL and source selection share the same closed rules.
    """
    if source_guard(source_plan) is not True:
        raise ValueError("source_sizing_acceptance_missing")
    source_sha = sha256(source_manifest_raw).hexdigest()
    if source_sha != source_plan["manifest_sha256"]:
        raise ValueError("source_manifest_changed")
    manifest = json.loads(source_manifest_raw)
    if manifest["limits"]["chunk_bytes"] > SOURCE_LIMIT:
        raise ValueError("source_part_cache_bound_exceeded")
    plan = material.original122_retained_selection_plan(original_raw, clock_references=clock_references)
    cases = plan["cases"]
    if len(cases) != 122 or len({case["case_id"] for case in cases}) != 122:
        raise ValueError("exact122_selection_plan_required")
    for case in cases:
        material.selection_queries(case, maximum_rows=maximum_rows_per_query)
    rules = [material.selection_rules(case) for case in cases]
    measurements = [dict(case_id=case["case_id"], tables={name: dict(rows=0, item_bytes=0)
        for name in material.TABLE_NAMES}) for case in cases]
    digests = [{name: _hash() for name in material.TABLE_NAMES} for _ in cases]
    parent_symbols, parent_bytes, cache = {}, 0, {}
    wanted = {case["symbol"] for case in cases}
    # Order parent membership is deliberately NOT filtered by submission date;
    # the real event query's subselect has only the symbol predicate.
    names = ("broker_orders",) + tuple(n for n in material.TABLE_NAMES if n != "broker_orders")
    for name in names:
        table_index = material.TABLE_NAMES.index(name)
        for row in _source_rows(name, manifest, source_reader, cache):
            if name == "broker_orders" and row["symbol"] in wanted:
                parent_bytes += len(material.canonical([row["id"], row["symbol"]]))
                if parent_bytes > CASE_LIMIT:
                    raise ValueError("file_sizing_parent_identity_bytes_exceeded")
                parent_symbols[row["id"]] = row["symbol"]
            selected = [i for i, case in enumerate(cases)
                if material.source_selection_matches(rules[i][table_index], row, parent_symbols=parent_symbols)]
            if not selected:
                continue
            reference = material._source_reference(name, row, manifest, source_reader, cache)
            for index in selected:
                measured = measurements[index]["tables"][name]
                if measured["rows"] >= maximum_rows_per_query:
                    raise ValueError("case_row_bound_exceeded_no_truncation")
                measured["item_bytes"] += len(material.canonical(_item(name, cases[index], row, reference)))
                _append_hash(digests[index][name], measured["rows"], row)
                measured["rows"] += 1
    for index, (case, measured) in enumerate(zip(cases, measurements)):
        size = len(_prefix(case)) + len(_suffix("0" * 64))
        for table_index, name in enumerate(material.TABLE_NAMES):
            table = measured["tables"][name]
            table["source_rows_sha256"] = _finish_hash(digests[index][name])
            size += len(_table_prefix(name, table_index)) + table["item_bytes"] + max(0, table["rows"] - 1) + 1
        measured["bytes"] = size
        if size > CASE_LIMIT:
            raise ValueError("single_case_64MiB_exceeded_no_cap_raise")
    descriptors = _descriptors(plan, measurements)
    manifest_bytes = len(material.canonical(_manifest(plan, descriptors, source_sha, "0" * 64)))
    if source_guard(source_plan) is not True:
        raise ValueError("source_sizing_acceptance_lost")
    return dict(version=MEASURE_VERSION, selection_plan=plan, cases=measurements,
        source_manifest_sha256=source_sha, maximum_rows_per_query=maximum_rows_per_query,
        manifest_bytes=manifest_bytes, measured_total_bytes=sum(m["bytes"] for m in measurements) + manifest_bytes,
        maximum_case_bytes=max(m["bytes"] for m in measurements), disk_cap="UNBOUND_MAIN_SEAL_REQUIRED",
        target_observed=False, permission_granted=False, actual_predicates="UNRUN")


def validate_disk_seal(measure, measure_raw, *, expected_sha256, disk_cap, approval_guard):
    if (sha256(measure_raw).hexdigest() != expected_sha256 or json.loads(measure_raw) != measure
            or measure.get("version") != MEASURE_VERSION or measure.get("permission_granted") is not False
            or approval_guard(measure, disk_cap) is not True):
        raise ValueError("actual_sizing_and_Main_disk_seal_required")
    total = measure["measured_total_bytes"]
    if type(total) is not int or total <= 0 or type(disk_cap) is not int or not total <= disk_cap <= 2 * total:
        raise ValueError("finite_disk_cap_must_fit_and_not_exceed_twice_measured_total")
    if len(measure["cases"]) != 122 or measure["maximum_case_bytes"] > CASE_LIMIT:
        raise ValueError("exact122_bounded_measurement_required")
    if (len({c["case_id"] for c in measure["cases"]}) != 122
            or measure["maximum_case_bytes"] != max(c["bytes"] for c in measure["cases"])
            or total != sum(c["bytes"] for c in measure["cases"]) + measure["manifest_bytes"]):
        raise ValueError("exact_case_totals_and_disk_measurement_required")
    for case in measure["cases"]:
        if (type(case["bytes"]) is not int or not 0 < case["bytes"] <= CASE_LIMIT
                or set(case["tables"]) != set(material.TABLE_NAMES)):
            raise ValueError("all_eight_bounded_measured_tables_required")
        for table in case["tables"].values():
            if type(table["rows"]) is not int or not 0 <= table["rows"] <= measure["maximum_rows_per_query"]:
                raise ValueError("measured_query_row_bound_changed")


def _private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("owned0700_directory_required")


def _reserve(path, required):
    if shutil.disk_usage(path).free - required < FREE_RESERVE:
        raise ValueError("five_GiB_free_reserve_required_STOP")


def _new_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
        os.close(fd)
        raise ValueError("private0600_file_required")
    return os.fdopen(fd, "wb")


@contextmanager
def _output(path):
    output, primary = _new_file(path), None
    try:
        yield output
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            output.close()
        except BaseException as exc:
            if primary is None:
                raise
            primary.add_note("shard_output_cleanup_type=" + type(exc).__name__)


def _publish(path, destination):
    os.link(path, destination, follow_symlinks=False)  # No overwrite/retry.
    path.unlink()
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _encoded_rows(session, name, query, maximum_rows, held):
    result, primary = None, None
    try:
        result = session.execute(query.execution_options(stream_results=True, yield_per=1))
        for index, value in enumerate(result.mappings()):
            held()
            if index >= maximum_rows:
                raise ValueError("case_row_bound_exceeded_no_truncation")
            yield json.loads(material.canonical(material.encode_source_row(name, dict(value))))
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if result is not None:
            try:
                result.close()
            except BaseException as exc:
                if primary is None:
                    raise
                primary.add_note("retained_cursor_cleanup_type=" + type(exc).__name__)


def materialize_original122_sharded(original_raw, *, factory, target_capability, source_manifest_raw,
        source_plan, source_reader, clock_references, read_guard, held_shared_read_lock,
        measure_raw, measure_sha256, disk_cap, disk_approval_guard, output_directory):
    """Existing Main-only RRRO selection/reselection, no commits or boot stage."""
    from sqlalchemy.orm import sessionmaker
    from tools.l12_private_full23_lifecycle import require_private_factory, verify_private_session

    measure = json.loads(measure_raw)
    validate_disk_seal(measure, measure_raw, expected_sha256=measure_sha256, disk_cap=disk_cap,
        approval_guard=disk_approval_guard)
    plan = material.original122_retained_selection_plan(original_raw, clock_references=clock_references)
    source_sha = sha256(source_manifest_raw).hexdigest()
    if (plan != measure["selection_plan"] or source_sha != measure["source_manifest_sha256"]
            or source_sha != source_plan["manifest_sha256"] or type(factory) is not sessionmaker):
        raise ValueError("exact_measurement_source_plan_factory_required")
    maximum_rows = measure["maximum_rows_per_query"]
    for case in plan["cases"]:
        material.selection_queries(case, maximum_rows=maximum_rows)
    manifest = json.loads(source_manifest_raw)
    if manifest["limits"]["chunk_bytes"] > SOURCE_LIMIT:
        raise ValueError("source_part_cache_bound_exceeded")
    require_private_factory(factory, target_capability=target_capability)
    directory = Path(output_directory)
    _private_directory(directory.parent)
    _reserve(directory.parent, disk_cap)
    directory.mkdir(mode=0o700)  # Must be fresh; never adopt prior output.
    _private_directory(directory)
    descriptors, cache, written, session, primary = [], {}, 0, None, None

    def held():
        if held_shared_read_lock() is not True:
            raise ValueError("original_close_shared_read_exclusion_lost")

    def guard():
        held()
        if read_guard(factory, target_capability, source_plan) is not True:
            raise ValueError("actual_copy_source_or_shared_read_exclusion_UNKNOWN")

    try:
        guard()
        session = factory()
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        session.execute(text("SET LOCAL statement_timeout = '750ms'"))
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        verify_private_session(session, target_capability=target_capability)
        for index, (case, measured) in enumerate(zip(plan["cases"], measure["cases"])):
            guard()
            if case["case_id"] != measured["case_id"]:
                raise ValueError("case_measurement_order_changed")
            temporary = directory / f"{index:03d}.json.incomplete"
            size, digest, first = 0, sha256(), {}
            with _output(temporary) as output:
                def emit(raw):
                    nonlocal size, written
                    if size + len(raw) > CASE_LIMIT or written + len(raw) > disk_cap:
                        raise ValueError("case_or_disk_bytes_exceeded_no_partial_success")
                    _reserve(directory, len(raw))
                    if output.write(raw) != len(raw):
                        raise OSError("shard_short_write_STOP")
                    digest.update(raw)
                    size, written = size + len(raw), written + len(raw)

                emit(_prefix(case))
                for table_index, (name, query) in enumerate(material.selection_queries(case, maximum_rows=maximum_rows)):
                    guard()
                    emit(_table_prefix(name, table_index))
                    actual_hash, source_hash, count = _hash(), _hash(), 0
                    iterator = _encoded_rows(session, name, query, maximum_rows, held)
                    try:
                        for row in iterator:
                            reference = material._source_reference(name, row, manifest, source_reader, cache)
                            source = cache["part"]["rows"][reference["row_ordinal_1based"] - 1]
                            _append_hash(actual_hash, count, row)
                            _append_hash(source_hash, count, source)
                            if count:
                                emit(b",")
                            emit(material.canonical(_item(name, case, source, reference, restored=row)))
                            count += 1
                    finally:
                        iterator.close()
                    expected = measured["tables"][name]
                    if count != expected["rows"] or _finish_hash(source_hash) != expected["source_rows_sha256"]:
                        raise ValueError("file_measurement_vs_actual_selection_changed")
                    first[name] = _finish_hash(actual_hash)
                    emit(b"]")
                for name, query in material.selection_queries(case, maximum_rows=maximum_rows):
                    guard()
                    value, count = _hash(), 0
                    iterator = _encoded_rows(session, name, query, maximum_rows, held)
                    try:
                        for row in iterator:
                            _append_hash(value, count, row)
                            count += 1
                    finally:
                        iterator.close()
                    if _finish_hash(value) != first[name]:
                        raise ValueError("case_materialization_readback_changed")
                emit(_suffix(sha256(material.canonical(first)).hexdigest()))
                if size != measured["bytes"]:
                    raise ValueError("exact_shard_size_vs_file_measurement_changed")
                output.flush()
                os.fsync(output.fileno())
            destination = directory / f"{index:03d}.json"
            _publish(temporary, destination)
            descriptors.append(dict(case_id=case["case_id"], reference=destination.name, bytes=size, sha256=digest.hexdigest()))
        guard()
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_failure = None
        if session is not None:
            for cleanup in (session.rollback, session.close):
                try:
                    cleanup()
                except BaseException as exc:
                    if primary is not None:
                        primary.add_note("retained_read_cleanup_type=" + type(exc).__name__)
                    elif cleanup_failure is None:
                        cleanup_failure = exc
        if primary is None and cleanup_failure is not None:
            raise cleanup_failure
    # This is unreachable on any query/source/reselect/write/cleanup failure.
    guard()
    if len(descriptors) != 122:
        raise ValueError("all122_required_before_manifest")
    complete = _manifest(plan, descriptors, source_sha, measure_sha256)
    raw = material.canonical(complete)
    if len(raw) != measure["manifest_bytes"] or written + len(raw) > disk_cap:
        raise ValueError("complete_manifest_size_changed")
    _reserve(directory, len(raw))
    temporary = directory / "manifest.incomplete"
    with _output(temporary) as output:
        if output.write(raw) != len(raw):
            raise OSError("manifest_short_write_STOP")
        output.flush()
        os.fsync(output.fileno())
    _publish(temporary, directory / "manifest.json")
    return dict(manifest=str(directory / "manifest.json"), sha256=sha256(raw).hexdigest(),
        bytes=written + len(raw), original_cases=122, permission_granted=False, actual_predicates="UNRUN")


def _read_private(path, expected_size, expected_sha, *, load):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != expected_size):
            raise ValueError("private_shard_file_identity_or_size_changed")
        digest, chunks = sha256(), [] if load else None
        while True:
            raw = os.read(fd, 1024 * 1024)
            if not raw:
                break
            digest.update(raw)
            if load:
                chunks.append(raw)
        after, path_after = os.fstat(fd), path.lstat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_uid, value.st_mode, value.st_nlink)
        if (digest.hexdigest() != expected_sha or identity(after) != identity(info)
                or identity(path_after) != identity(info)):
            raise ValueError("private_shard_hash_or_identity_changed")
        return json.loads(b"".join(chunks)) if load else None
    finally:
        os.close(fd)


def iter_complete_original122(manifest_path, *, manifest_sha256, source_guard):
    """Preflight ALL122 hashes, then load/yield one case; never collect a batch."""
    path = Path(manifest_path)
    if path.name != "manifest.json":
        raise ValueError("literal_complete_manifest_name_required")
    _private_directory(path.parent)
    size = path.lstat().st_size
    if size > 1024 * 1024:
        raise ValueError("bounded_complete_manifest_required")
    manifest = _read_private(path, size, manifest_sha256, load=True)
    cases = manifest["cases"]
    if (manifest["version"] != MANIFEST_VERSION or manifest["original_cases"] != 122
            or manifest["omitted_cases"] != 0 or manifest["permission_granted"] is not False
            or manifest["real_objects_constructed"] is not False or manifest["source_sha256"] != material.EXPORT_SHA
            or manifest["status"] != "COMPLETE122_AFTER_ALL_RESELECTS_AND_ROLLBACK_CLOSE"
            or len(cases) != 122 or len({c["case_id"] for c in cases}) != 122
            or source_guard(manifest) is not True):
        raise ValueError("accepted_complete_original122_manifest_required")
    expected = {"manifest.json"} | {f"{i:03d}.json" for i in range(122)}
    if {entry.name for entry in path.parent.iterdir()} != expected:
        raise ValueError("partial_or_foreign_shard_directory")
    for index, item in enumerate(cases):
        if (item["reference"] != f"{index:03d}.json" or type(item["bytes"]) is not int
                or not 0 < item["bytes"] <= CASE_LIMIT):
            raise ValueError("shard_path_or_case_bound_changed")
        _read_private(path.parent / item["reference"], item["bytes"], item["sha256"], load=False)
    for item in cases:
        if source_guard(manifest) is not True:
            raise ValueError("shard_source_acceptance_lost")
        record = _read_private(path.parent / item["reference"], item["bytes"], item["sha256"], load=True)
        if (record["case"]["case_id"] != item["case_id"] or record["permission_granted"] is not False
                or record["real_objects_constructed"] is not False or record["version"] != SHARD_VERSION):
            raise ValueError("shard_case_identity_changed")
        yield record
        del record


def iter_decoded_original122(manifest_path, *, manifest_sha256, source_guard):
    from tools.l12_original122_typed_decoders import decode_materialized_case

    for record in iter_complete_original122(manifest_path, manifest_sha256=manifest_sha256, source_guard=source_guard):
        yield decode_materialized_case(record)
