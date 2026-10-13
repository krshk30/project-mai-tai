"""Bounded cursor fingerprints; detection is not proof of an unseen interval."""

import os

CURSOR_BYTES = 256


class LogReadRace(ValueError):
    """Observed bounded append/short cursor race; only read_live may retry once."""


def check_read_coverage(descriptor: int, offset: int, covered: int, bound: int, label: str) -> None:
    size = os.fstat(descriptor).st_size
    if size - offset > bound:
        raise ValueError(f"{label} log backlog bound")
    if size != covered:
        raise LogReadRace(f"{label} log changed beyond read coverage")


def cursor_anchor(descriptor: int, offset: int) -> bytes:
    size = min(CURSOR_BYTES, offset)
    value = os.pread(descriptor, size, offset - size)
    if len(value) != size:
        if os.fstat(descriptor).st_size < offset:
            raise LogReadRace("log shortened during bounded cursor read")
        raise ValueError("log changed during bounded cursor read")
    return value


def cursor_rewritten(descriptor: int, offset: int, previous: bytes | None) -> bool:
    return os.fstat(descriptor).st_size < offset or (
        previous is not None and cursor_anchor(descriptor, offset) != previous)
