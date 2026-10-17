"""Per-sync Webull SDK delegate observations, explicitly NOT wire counters."""

from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import threading


@dataclass
class SdkFrame:
    invocations: int = 0
    closed: bool = False
    reliable: bool = True
    lock: object = field(default_factory=threading.Lock)

    def note(self):
        with self.lock:
            if not self.closed:
                self.invocations += 1

    def finish(self):
        with self.lock:
            self.closed = True
            return self.invocations if self.reliable else "UNKNOWN"


CURRENT: ContextVar[SdkFrame | None] = ContextVar("sync_webull_sdk_diagnostics", default=None)


def note_sdk_invocation():
    frame = None
    try:
        frame = CURRENT.get()
        if frame is not None:
            frame.note()
    except Exception:
        # Diagnostic failure never replaces the delegate result or exception.
        if frame is not None:
            frame.reliable = False


def log_logical_accounts(logger, names):
    if CURRENT.get() is None:
        return
    try:
        logger.info(
            "[OMS-BROKER-SYNC-ACCOUNTS] phase=start logical_accounts=%s physical_http_reads=PENDING",
            json.dumps(list(names), ensure_ascii=True, separators=(",", ":")),
        )
    except Exception:
        pass
