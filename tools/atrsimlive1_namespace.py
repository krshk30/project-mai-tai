"""Exact day/full-binding private cache namespace. Old generations stay intact."""
from datetime import date
import os
from pathlib import Path
import stat

from atrsimlive1_incremental_cache import CacheRefusal, CacheStore, fingerprint


def namespace_name(binding):
    day = binding.get('trading_day')
    if type(day) is not str or date.fromisoformat(day).isoformat() != day:
        raise CacheRefusal('canonical_cache_day_required')
    return day + '-' + fingerprint(binding)


def namespace_root(parent, binding, *, retention_enabled=False):
    store = CacheStore(parent, binding, retention_enabled=retention_enabled)
    name = namespace_name(binding)
    with store.directory() as directory:
        if stat.S_IMODE(os.fstat(directory).st_mode) != 0o700:
            raise CacheRefusal('namespace_parent_exact_0700_required')
        try:
            os.mkdir(name, 0o700, dir_fd=directory)
        except FileExistsError:
            pass  # Only exact existing day+binding may be reopened.
        try:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        except OSError as error:
            raise CacheRefusal('namespace_directory_refused') from error
        try:
            opened = os.fstat(child)
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid()
                    or stat.S_IMODE(opened.st_mode) != 0o700
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                    or current.st_uid != opened.st_uid or current.st_mode != opened.st_mode):
                raise CacheRefusal('owned_exact_namespace_identity_required')
            os.fsync(directory)
        finally:
            os.close(child)
    return Path(parent) / name
