"""Separate reviewed seed read; the existing VOLBODY seven-day port is unchanged."""

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json

import sqlalchemy as sa

from .firstbar_binding import FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION
from .firstbar_reference import CSV_SHA256, SELECTOR_SHA256, SPLIT_SHA256, ReferenceSeed
from .models import consumers, rows, runs
from .ui_reader import CONSUMER_LIMIT, ROW_LIMIT, SimulationUIReader, unavailable_storage


SEED_START = date(2026, 9, 22)
SEED_END = date(2026, 10, 23)
ORDERED_KEYS_SHA256 = "ad69e2256eacbc9cdd108e0b90af26bb032241d8f92e016a2c624c9083272162"
SEED_BINDING = {
    "version": "FIRSTBAR_REVIEWED173_20261023",
    "population_label": "replay, in-sample",
    "selected_signals": 173,
    "reference_exclusions": 7,
    "start_day": SEED_START.isoformat(),
    "end_day": SEED_END.isoformat(),
    "csv_sha256": CSV_SHA256,
    "split_sha256": SPLIT_SHA256,
    "selector_sha256": SELECTOR_SHA256,
    "ordered_keys_sha256": ORDERED_KEYS_SHA256,
}


def reference_seed_binding(seed):
    """Bind the byte-validated metadata seed, not an HTTP-supplied permission flag."""
    if not isinstance(seed, ReferenceSeed):
        raise ValueError("actual_reference_seed_required")
    keys = [case.reference_key for case in seed.cases]
    checksum = hashlib.sha256(json.dumps(keys, separators=(",", ":"),
        ensure_ascii=True).encode()).hexdigest()
    manifest = seed.manifest
    if (len(keys) != 173 or len(seed.reference_exclusions) != 7
            or manifest.expected_signals != 173 or checksum != ORDERED_KEYS_SHA256
            or (manifest.csv_source.sha256, manifest.split_source.sha256,
                manifest.selector_source.sha256) != (CSV_SHA256, SPLIT_SHA256, SELECTOR_SHA256)):
        raise ValueError("reviewed_reference_seed_binding_required")
    return deepcopy(SEED_BINDING)


def validate_firstbar_scope(source_id, start_day, end_day, limit):
    from .ui_reader import SOURCE_ID_PATTERN
    import re

    if (not isinstance(source_id, str) or re.fullmatch(SOURCE_ID_PATTERN, source_id) is None
            or not isinstance(start_day, date) or isinstance(start_day, datetime)
            or not isinstance(end_day, date) or isinstance(end_day, datetime)
            or not 0 <= (end_day - start_day).days <= 31
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= ROW_LIMIT
            or not SEED_START <= start_day <= end_day <= SEED_END):
        raise ValueError("bounded_reviewed_firstbar_seed_scope_required")


def seed_run_is_bound(run):
    if not isinstance(run, dict) and not hasattr(run, "keys"):
        return False
    coverage = run.get("coverage")
    refs = run.get("source_refs")
    if (not isinstance(coverage, dict) or not isinstance(refs, (list, tuple))
            or any(not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str)
                   for ref in refs)):
        return False
    return (run.get("policy_version") == FIRSTBAR_RESULT_VERSION
        and coverage.get("engine_dependency_head") == FIRSTBAR_ENGINE_HEAD
        and coverage.get("reference_seed_binding") == SEED_BINDING
        and {CSV_SHA256, SPLIT_SHA256, SELECTOR_SHA256}.issubset({ref["sha256"] for ref in refs}))


def _capture_payload_decoder(connection):
    """Capture the connection's configured JSON TEXT loader, not a new policy.

    Only the bound loader/encoding escape the checkout; decoding happens after
    rollback and connection return. This preserves configured json.loads hooks.
    """
    from psycopg.pq import Format

    try:
        raw = connection.connection.driver_connection
        loader_type = raw.adapters.get_loader(114, Format.TEXT)
        loader = loader_type(114, raw)
        encoding = raw.info.encoding
        if not callable(loader.load) or not isinstance(encoding, str):
            raise ValueError("firstbar_native_json_loader_required")
        return loader.load, encoding
    except (AttributeError, TypeError, KeyError) as error:
        raise ValueError("firstbar_native_json_loader_required") from error


def _decode_payload_text(value, decode, encoding):
    # SQL NULL stays None, exactly as the native JSON column result did.
    if value is None:
        return None
    if isinstance(value, str):
        value = value.encode(encoding, "strict")
    elif not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("firstbar_text_payload_required")
    return decode(value)


class FirstBarSeedUIReader(SimulationUIReader):
    """Lazy, SELECT-only, source-bound historical seed reader; never a writer."""

    def read(self, source_id, start_day, end_day, *, limit=ROW_LIMIT):
        validate_firstbar_scope(source_id, start_day, end_day, limit)
        try:
            engine = self._get_engine()
            if engine is None:
                return unavailable_storage("FIRSTBAR_DATABASE_UNCONFIGURED_OR_NOT_PG")
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    if connection.dialect.name != "postgresql":
                        return unavailable_storage("FIRSTBAR_READER_REQUIRES_PG")
                    connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    connection.exec_driver_sql("SET LOCAL statement_timeout = '750ms'")
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
                    run = connection.execute(sa.select(runs).where(
                        runs.c.source_id == source_id).limit(1)).mappings().one_or_none()
                    if not seed_run_is_bound(run):
                        return unavailable_storage("FIRSTBAR_REVIEWED_SEED_BINDING_UNKNOWN")
                    decode, encoding = _capture_payload_decoder(connection)
                    page_text = connection.execute(sa.select(sa.cast(rows.c.payload, sa.Text)).where(
                        rows.c.source_id == source_id, rows.c.day >= start_day, rows.c.day <= end_day)
                        .order_by(rows.c.day, rows.c.row_key).limit(limit + 1)).scalars().all()
                    workers = connection.execute(sa.select(consumers).order_by(
                        consumers.c.consumer_id).limit(CONSUMER_LIMIT + 1)).mappings().all()
                    stored_run = dict(run)
                    stored_workers = [dict(item) for item in workers[:CONSUMER_LIMIT]]
                    workers_truncated = len(workers) > CONSUMER_LIMIT
                finally:
                    transaction.rollback()
            # The entire bounded text result is fetched before release. Decode ALL
            # limit+1 rows (including overflow) only after rollback and pool checkin.
            page = [_decode_payload_text(value, decode, encoding) for value in page_text]
            return {"run": stored_run, "rows": page[:limit], "truncated": len(page) > limit,
                "consumers": stored_workers, "workers_truncated": workers_truncated,
                "read_status": "AVAILABLE" if page else "NO_ROWS_UNKNOWN",
                "read_issues": [] if page else ["NO_ROWS_UNKNOWN"]}
        except (sa.exc.SQLAlchemyError, OSError, TimeoutError, ImportError, ValueError, TypeError, KeyError):
            return unavailable_storage("FIRSTBAR_STORAGE_UNAVAILABLE")
