"""Private evidence append/fsync/readback adapter; never fabricates durability.

Main owns its runtime claim and storage guarantees. Success means the specified
POSIX calls completed and exact bytes read back, not proof against power loss.
"""

from hashlib import sha256
import os
from pathlib import Path
import stat


class PrivateJournal:
    def __init__(self, root):
        path = Path(root)
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("canonical_private_journal_directory_required")
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise ValueError("journal_symlink_parent_refused")
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(self.fd)
            raise ValueError("owned_0700_journal_directory_required")
        # A previous partial record must never be mistaken for a fresh journal.
        try:
            if os.listdir(self.fd):
                raise ValueError("fresh_empty_journal_directory_required")
        except BaseException:
            os.close(self.fd)
            raise
        self.ordinal, self.last_receipt, self.closed = 0, None, False
        self.root = path

    def __call__(self, raw):
        if self.closed or type(raw) is not bytes or not 0 < len(raw) <= 1024 * 1024:
            raise ValueError("bounded_open_private_journal_required")
        name = f"{self.ordinal:08d}.json"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("journal_short_write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.fd)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != len(raw)):
                raise ValueError("journal_readback_identity_mismatch")
            observed = bytearray()
            while len(observed) <= len(raw):
                part = os.read(fd, len(raw) + 1 - len(observed))
                if not part:
                    break
                observed.extend(part)
            if bytes(observed) != raw:
                raise ValueError("journal_readback_bytes_mismatch")
        finally:
            os.close(fd)
        self.last_receipt = dict(ordinal=self.ordinal, filename=name, bytes=len(raw),
            sha256=sha256(raw).hexdigest(), file_fsync_returned=True,
            directory_fsync_returned=True, exact_readback=True, mode="0600", nlink=1,
            storage_power_loss_guarantee="NOT_ESTABLISHED_BY_SYSTEM_CALL_RETURN")
        self.ordinal += 1
        return raw

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self.fd)
