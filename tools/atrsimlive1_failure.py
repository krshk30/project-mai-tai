"""Private failure receipts. Never stringify arbitrary errors, rows or SQL.

Only reviewed source literals/schema names can enter a message. Other messages
are explicitly redacted. A failure token contains exactly class and file SHA.
"""
from collections import deque
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import traceback

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'ATRSIMLIVE1_PRIVATE_EXCEPTION_V1'
MAX_BYTES = 16384
LITERALS_SHA256 = '92cf7e65b9e29d38ae5aedadc8d12dd2e997e4f8255fa6b4b6a174b0694499fe'
CLASSES = frozenset(('AssertionError', 'AttributeError', 'CacheRefusal', 'ChildProcessError',
    'FileExistsError', 'FileNotFoundError', 'IndexError', 'IntegrityError', 'InterfaceError',
    'KeyError', 'MemoryError', 'OSError', 'OperationalError', 'OverflowError', 'PermissionError',
    'ProgrammingError', 'QueryCanceled', 'RecursionError', 'RuntimeError', 'TimeoutError',
    'TypeError', 'UnicodeDecodeError', 'UnicodeError', 'ValueError', 'ZeroDivisionError', 'OTHER'))


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate_failure_key')
        result[key] = value
    return result


def exception_class(error):
    name = type(error).__name__
    return name if name in CLASSES else 'OTHER'


@lru_cache(maxsize=1)
def literals():
    path = ROOT / 'tools/atrsimlive1_failure_literals.json'
    assert not path.is_symlink()
    raw = path.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError('failure_literal_file_cap')
    if hashlib.sha256(raw).hexdigest() != LITERALS_SHA256:
        raise ValueError('failure_literal_hash_mismatch')
    value = json.loads(raw, object_pairs_hook=_pairs)
    if set(value) != {'schema', 'codes', 'keys', 'types', 'functions'} or value['schema'] != 'ATRSIMLIVE1_FAILURE_LITERALS_V1':
        raise ValueError('failure_literal_contract')
    for key in ('codes', 'keys', 'types'):
        if not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key]):
            raise ValueError('failure_literal_shape')
    if not isinstance(value['functions'], dict):
        raise ValueError('failure_literal_shape')
    return value


def describe(error, role):
    try:
        contract = literals()
        contract_status = 'VERIFIED'
    except Exception:
        contract = dict(codes=[], keys=[], types=[], functions={})
        contract_status = 'UNAVAILABLE_REDACTED'
    message = 'REDACTED_UNTRUSTED_MESSAGE'
    args = error.args
    if len(args) == 1 and type(args[0]) is str and args[0] in contract['codes']:
        message = args[0]
    elif type(error) is KeyError and len(args) == 1 and type(args[0]) is str and args[0] in contract['keys']:
        message = "missing schema key: " + args[0]
    elif (type(error) is AttributeError and error.name in contract['keys']
            and type(error.obj).__name__ in contract['types']):
        message = type(error.obj).__name__ + ' missing attribute: ' + error.name
    message = message.encode('utf-8')[:2048].decode('utf-8', errors='ignore')
    frames = deque(maxlen=20)
    for frame, line in traceback.walk_tb(error.__traceback__):
        code = frame.f_code
        filename, function = 'EXTERNAL_SOURCE', 'EXTERNAL_FUNCTION'
        try:
            relative = str(Path(code.co_filename).resolve().relative_to(ROOT))
        except (ValueError, OSError):
            relative = None
        if relative in contract['functions']:
            filename = relative
            if code.co_name in contract['functions'][relative]:
                function = code.co_name
            else:
                function = 'UNRECOGNIZED_SOURCE_FUNCTION'
        frames.append(dict(file=filename, line=int(line), function=function))
    return dict(schema=SCHEMA, role=role, actual_captured_at=datetime.now(timezone.utc).isoformat(),
                exception_class=exception_class(error), message=message, frames=list(frames),
                literal_contract_status=contract_status)


def write_failure(error, directory, *, role):
    if role not in ('child', 'parent'):
        raise ValueError('failure_role_required')
    directory = Path(directory)
    if (not directory.is_absolute() or '..' in directory.parts
            or any(p.is_symlink() for p in (directory, *directory.parents))):
        raise ValueError('private_failure_directory_required')
    before = directory.stat()
    if before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o700:
        raise ValueError('owned_0700_failure_directory_required')
    packet = json.dumps(describe(error, role), allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
    if len(packet) > MAX_BYTES:
        raise ValueError('failure_receipt_byte_cap')
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = os.fstat(directory_fd)
        if ((current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or current.st_uid != os.geteuid() or stat.S_IMODE(current.st_mode) != 0o700):
            raise ValueError('private_failure_directory_changed')
        partial, final = '.' + role + '-exception.json.partial', role + '-exception.json'
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        with os.fdopen(fd, 'wb') as stream:
            created = os.fstat(stream.fileno())
            if (not stat.S_ISREG(created.st_mode) or created.st_uid != os.geteuid()
                    or stat.S_IMODE(created.st_mode) != 0o600 or created.st_nlink != 1):
                raise ValueError('private_0600_failure_file_required')
            stream.write(packet)
            stream.flush()
            os.fsync(stream.fileno())
        readback = os.open(partial, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        with os.fdopen(readback, 'rb') as stream:
            info = os.fstat(stream.fileno())
            actual = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if ((info.st_dev, info.st_ino) != (created.st_dev, created.st_ino)
                or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or actual != packet):
            raise ValueError('failure_receipt_readback_mismatch')
        os.link(partial, final, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        os.fsync(directory_fd)
        os.unlink(partial, dir_fd=directory_fd)  # Only this unpublished staging link; final receipt stays.
        os.fsync(directory_fd)
        info = os.stat(final, dir_fd=directory_fd, follow_symlinks=False)
        if ((info.st_dev, info.st_ino) != (created.st_dev, created.st_ino)
                or info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('failure_final_identity_mismatch')
        return dict(exception_class=exception_class(error), evidence_sha256=hashlib.sha256(actual).hexdigest())
    finally:
        os.close(directory_fd)


def failure_token(raw):
    """A failed child may supply two bounded safe fields, never a path/message."""
    if not isinstance(raw, bytes) or len(raw) > 512:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if (not isinstance(value, dict) or set(value) != {'exception_class', 'evidence_sha256'}
            or not isinstance(value['exception_class'], str) or value['exception_class'] not in CLASSES
            or value['evidence_sha256'] is not None and (not isinstance(value['evidence_sha256'], str)
                or re.fullmatch('[0-9a-f]{64}', value['evidence_sha256']) is None)):
        return None
    return value
