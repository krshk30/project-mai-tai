"""Compact public stage clocks; no SQL, input values, DSNs or raw errors."""
import re
import sys
import time

MAX_STAGE_BYTES = 60000  # Below the existing child's unchanged 65536-byte stderr cap.
_written = 0
_LINE = re.compile(rb"[0-9]{13} (?:Q[0-9]+|COLLECT|IDENTITY|ROSTER|SOURCE:[a-z_]+|PAGE:[a-z_]+:[A-Z0-9.-]+|WINDOW_CENSUS|CONDITION_CENSUS|QUOTE_CENSUS|TERMINAL:[A-Z][A-Z0-9.-]{0,15}|WINDOW:(?:trades|quotes):[A-Z][A-Z0-9.-]{0,15}|QUOTE_CARRY:[A-Z][A-Z0-9.-]{0,15}|INCREMENTAL_(?:FINGERPRINT|ROWS):(?:trades|quotes):[A-Z][A-Z0-9.-]{0,15}|INCREMENTAL_TERMINAL:[A-Z][A-Z0-9.-]{0,15}|CACHE_SAVE|CACHE_LOAD|CACHE_WRITE|ROLLBACK|DB_CLOSED|HASH:[A-Z0-9.-]+|MODEL:[A-Z0-9.-]+|ORACLE:[A-Z0-9.-]+|FIRSTBAR|CORE|AGG|COMBINE|ENCODE|RENDER)[+-](?::[0-9]+)?\n")


class StageForwarder:
    """Forward only our literal compact clocks, including before a timeout.

    Raw child exceptions/SQL/credentials remain private. The caller enforces
    its existing 65536-byte total child-stderr bound before feeding bytes.
    """
    def __init__(self):
        self.pending = bytearray()

    def feed(self, raw):
        self.pending.extend(raw)
        while b"\n" in self.pending:
            line, _, remaining = self.pending.partition(b"\n")
            self.pending[:] = remaining
            line += b"\n"
            if _LINE.fullmatch(line):
                sys.stderr.write(line.decode("ascii"))
                sys.stderr.flush()


def stage(code, phase, rows=None):
    global _written
    if (not isinstance(code, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code) is None
            or phase not in ("+", "-")
            or rows is not None and (type(rows) is not int or rows < 0)):
        raise ValueError("stage_literal_shape")
    suffix = "" if rows is None else ":" + str(rows)
    line = str(time.time_ns() // 1000000) + " " + code + phase + suffix + "\n"
    count = len(line.encode("ascii"))
    if _written + count > MAX_STAGE_BYTES:
        raise ValueError("stage_stderr_budget")
    sys.stderr.write(line)
    sys.stderr.flush()
    _written += count
