"""Bounded cursor fingerprints; detection is not proof of an unseen interval."""

import os

CURSOR_BYTES = 256


def cursor_anchor(descriptor: int, offset: int) -> bytes:
    size = min(CURSOR_BYTES, offset)
    value = os.pread(descriptor, size, offset - size)
    if len(value) != size:
        raise ValueError("log changed during bounded cursor read")
    return value


def cursor_rewritten(descriptor: int, offset: int, previous: bytes | None) -> bool:
    return os.fstat(descriptor).st_size < offset or (
        previous is not None and cursor_anchor(descriptor, offset) != previous)
