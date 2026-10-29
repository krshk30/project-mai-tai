"""Bounded file/process mechanics for the separate TODAY simulation job.

No application, DB or provider imports. This module grants no evidence/engine
qualification: the producer must supply the agreed canonical per-table facts.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import math
import os
from pathlib import Path
import selectors
import stat
import subprocess
import tempfile
import time
from zoneinfo import ZoneInfo
from atrsimlive1_progress import StageForwarder


SCHEMA = "ATRSIMLIVE1_TODAY_V1"
KEYS = ("FIRSTBAR", "FIRSTBAR_X3", "AGG_ENTRY", "LIVE:ACTUAL",
        "TODAY:SUPPLIED", "VOLBODY:QUEUE_AWARE", "VOLBODY:OPTIMISTIC")
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 10000
ET = ZoneInfo("America/New_York")


class ChildFailure(ValueError):
    """A physically ended failed child with a bounded, safe diagnostic token."""
    def __init__(self, token):
        super().__init__('today_child_failed')
        self.child_diagnostic = token


def instant(value):
    if not isinstance(value, str):
        raise ValueError("aware_timestamp_required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("aware_timestamp_required")
    return result.astimezone(timezone.utc)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_today_key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("nonfinite_today_number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("today_numeric_overflow")
    return number


def _finite_integer(value):
    number = int(value)
    try:
        finite = math.isfinite(float(number))
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("today_numeric_overflow")
    return number


def plain_text(value, *, limit=2048):
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _json_values(value):
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif type(value) is float:
        _finite_float(value)
    elif type(value) is int:
        _finite_integer(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("literal_json_string_key_required")
            _json_values(key)
            _json_values(item)
    elif isinstance(value, list):
        for item in value:
            _json_values(item)


def decode(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("today_file_byte_cap")
    text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
    value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant,
                       parse_float=_finite_float, parse_int=_finite_integer)
    _json_values(value)
    return value


def validate_percent_fields(display):
    optional = {"pnl_pct", "pnl_pct_label_known", "sizing_status", "percent_basis"}
    if not optional.intersection(display):
        return  # Earlier immutable documents retain their original contract.
    if not optional <= display.keys() or type(display["pnl_pct_label_known"]) is not bool:
        raise ValueError("exact_optional_percent_contract_required")
    for name, limit in (("sizing_status", 256), ("percent_basis", 512)):
        if (not plain_text(display[name], limit=limit)
                or any(ord(character) < 32 for character in display[name])):
            raise ValueError("bounded_percent_basis_required")
    if display["pnl_pct_label_known"]:
        value = display["pnl_pct"]
        try:
            if type(value) not in (str, int):
                raise ValueError("finite_known_percent_required")
            percent = Decimal(str(value))
            if not percent.is_finite() or not math.isfinite(float(percent)):
                raise ValueError("finite_known_percent_required")
        except (ArithmeticError, ValueError) as exc:
            raise ValueError("finite_known_percent_required") from exc
        if display["pnl_pct_label"] != format(percent, "+.2f") + "%":
            raise ValueError("numeric_percent_label_must_agree")
    elif display["pnl_pct"] is not None or display["pnl_pct_label"] != "UNKNOWN":
        raise ValueError("unknown_percent_cannot_claim_value")
    if (display["sizing_status"].startswith(("UNKNOWN_UNITS_NORMALIZED", "UNKNOWN_POST_ALLOCATION"))
            and (display["quantity"] != "UNKNOWN" or display["pnl_label_known"] is not False)):
        raise ValueError("normalized_percent_cannot_claim_units_or_usd")


def validate(document, now):
    _json_values(document)
    if now.tzinfo is None:
        raise ValueError("aware_now_required")
    day = now.astimezone(ET).date().isoformat()
    if isinstance(document, dict) and document.get('schema') == 'ATRSIMLIVE1_TODAY_COMPACT_V2':
        from project_mai_tai.atr_simulation.today_file import validate_today_document
        from atrsimlive1_output import encode_compact
        checked = validate_today_document(document, now=now)
        if (checked['file_status'] != 'AVAILABLE'
                or any(table['display_status'] in ('INVALID_TABLE', 'TABLE_NOT_PUBLISHED')
                       for table in checked['tables'].values())):
            raise ValueError('exact_compact_today_contract_required')
        if sum(len(table['rows']) for table in document['tables'].values()) > MAX_ROWS:
            raise ValueError('today_row_cap')
        encode_compact(document)  # Exact compact target; original 16MiB cap remains.
        return document
    if (not isinstance(document, dict) or document.get("schema") != SCHEMA
            or document.get("trading_day") != day
            or document.get("timezone") != "America/New_York"
            or document.get("native_execution_qualified") is not False
            or not isinstance(document.get("producer"), dict)
            or not isinstance(document.get("tables"), dict)
            or set(document["tables"]) != set(KEYS)):
        raise ValueError("exact_today_contract_required")
    generated = instant(document["generated_at"])
    if generated > now:
        raise ValueError("future_generation")
    source = document.get("source_asof")
    if source is not None and instant(source) > generated:
        raise ValueError("future_source_asof")
    count, identities = 0, set()
    for key, table in document["tables"].items():
        if (not isinstance(table, dict)
                or table.get("read_status") not in {"AVAILABLE", "UNKNOWN", "UNAVAILABLE"}
                or not plain_text(table.get("source_id"))
                or not isinstance(table.get("issues"), list)
                or any(not plain_text(issue, limit=256) for issue in table["issues"])
                or not isinstance(table.get("rows"), list)
                or type(table.get("truncated")) is not bool):
            raise ValueError("exact_today_table_required")
        for label in ("updated_at", "source_asof"):
            value = table.get(label)
            if value is not None and instant(value) > generated:
                raise ValueError("future_table_clock")
        if table["read_status"] == "AVAILABLE":
            if (table.get("updated_at") is None or table.get("source_asof") is None
                    or table["issues"] or table["truncated"]):
                raise ValueError("available_table_evidence_required")
        elif not table["issues"]:
            raise ValueError("unknown_table_requires_reason")
        for label in ("skipped_count", "unknown_count"):
            value = table.get(label)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("literal_nonnegative_count_required")
        if not plain_text(table.get("known_pnl_label")):
            raise ValueError("literal_pnl_label_required")
        if table["read_status"] != "AVAILABLE" and table["known_pnl_label"] != "UNKNOWN":
            raise ValueError("unknown_world_cannot_claim_full_economics")
        for row in table["rows"]:
            count += 1
            if count > MAX_ROWS:
                raise ValueError("today_row_cap")
            if (not isinstance(row, dict) or not isinstance(row.get("identity"), dict)
                    or row["identity"].get("trading_day") != day
                    or not isinstance(row.get("row"), dict)):
                raise ValueError("wrong_day_or_noncanonical_row")
            if (not plain_text(row["row"].get("coverage_note"), limit=512)
                    or any(ord(character) < 32 for character in row["row"]["coverage_note"])):
                raise ValueError("visible_per_row_coverage_note_required")
            display = row["row"]
            fields = ("ticker", "path", "quantity", "entry_time", "entry_price", "exit_time",
                      "exit_price", "pnl_label", "pnl_pct_label", "summary")
            if (any(not plain_text(display.get(field))
                    for field in fields) or type(display.get("pnl_label_known")) is not bool):
                raise ValueError("canonical_display_fields_required")
            if display["pnl_label_known"]:
                value = display.get("pnl")
                if (type(value) not in (str, int)
                        or not Decimal(str(value)).is_finite()
                        or not math.isfinite(float(Decimal(str(value))))):
                    raise ValueError("finite_known_row_amount_required")
            elif display.get("pnl") is not None or display["pnl_label"] != "UNKNOWN":
                raise ValueError("unknown_row_economics_required")
            validate_percent_fields(display)
            identity = (key, json.dumps(row["identity"], sort_keys=True), row.get("outcome_id"))
            if identity in identities:
                raise ValueError("duplicate_today_outcome")
            identities.add(identity)
    return document


def private_directory(directory):
    directory = Path(directory)
    # Walk existing components without resolving a symlink into an allowed root.
    for part in (directory, *directory.parents):
        if part.is_symlink():
            raise ValueError("today_directory_symlink")
    info = directory.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise ValueError("owned_nonwritable_today_directory_required")
    return directory


@contextmanager
def exclusive_run(directory):
    directory = private_directory(directory)
    fd = os.open(directory / ".run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("owned_regular_lock_required")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def publish(document, destination, now, *, deadline_monotonic=None):
    if deadline_monotonic is not None:
        if (type(deadline_monotonic) not in (int, float)
                or not math.isfinite(deadline_monotonic)):
            raise ValueError('finite_publication_deadline_required')
        if time.monotonic() >= deadline_monotonic:
            raise TimeoutError('refresh_publication_deadline')
    validate(document, now)
    raw = json.dumps(document, ensure_ascii=False, allow_nan=False,
                     separators=(",", ":"), sort_keys=True).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("today_file_byte_cap")
    destination = Path(destination)
    directory = private_directory(destination.parent)
    mode = 0o600
    if destination.exists() or destination.is_symlink():
        previous = destination.lstat()
        if (not stat.S_ISREG(previous.st_mode) or previous.st_uid != os.geteuid()
                or previous.st_mode & 0o022):
            raise ValueError("owned_regular_today_file_required")
        mode = stat.S_IMODE(previous.st_mode)
    fd, name = tempfile.mkstemp(prefix=".current-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise TimeoutError('refresh_publication_deadline')
        os.replace(name, destination)
        name = None
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if name is not None:
            os.unlink(name)
    return len(raw)


def bounded_child(command, *, seconds=240, closure_observer=None):
    """Bounded owned process, capped output, actual join even on timeout/refusal.

    The caller constructs its literal installed child command, never input JSON.
    Nothing reaches current.json until successful child closure and validation.
    """
    if type(seconds) not in (int, float) or not 0 < seconds <= 240:
        raise ValueError("bounded_today_wall_required")
    if closure_observer is not None and not callable(closure_observer):
        raise ValueError('explicit_child_closure_observer_required')
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    buffers = {"out": bytearray(), "err": bytearray()}
    progress = StageForwarder()
    selector = selectors.DefaultSelector()
    try:
        for stream, kind in ((process.stdout, "out"), (process.stderr, "err")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, kind)
        while selector.get_map():
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("today_wall_deadline")
            for event, _ in selector.select(min(remaining, .25)):
                chunk = os.read(event.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(event.fileobj)
                    continue
                target = buffers[event.data]
                target.extend(chunk)
                if len(target) > (MAX_BYTES if event.data == "out" else 65536):
                    raise ValueError("today_child_output_cap")
                if event.data == "err":
                    progress.feed(chunk)
        process.wait(timeout=max(.001, seconds - (time.monotonic() - started)))
        if process.returncode != 0:
            from atrsimlive1_failure import failure_token
            diagnostic = failure_token(bytes(buffers['out']))
            if diagnostic is not None:
                raise ChildFailure(diagnostic)
            raise ValueError("today_child_failed")
        return bytes(buffers["out"]), time.monotonic() - started
    finally:
        selector.close()
        if process.poll() is None:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()
        if closure_observer is not None:
            # Observer receives the ACTUAL owned process, after wait/poll and
            # pipe closure, including timeout cleanup. It cannot waive failure.
            closure_observer(process)
