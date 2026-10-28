"""One service-owned phone task; position/native polling never awaits it.

Bounded FIFO references arbitrate both watchdogs. Failed attempts rejoin at the
tail; a fixed poll order cannot repeatedly overtake an older pending hold.
Only EXACT True means publisher acceptance, not device delivery or permission.
"""

import asyncio
import logging
from collections import OrderedDict

from project_mai_tai.brokerdecides1 import MAX_SYMBOLS

LOGGER = logging.getLogger(__name__)
MAX_PENDING = 2 * MAX_SYMBOLS


def schedule_alert(service, delivery, *, now_ms, title, body, event_id, still_valid=lambda observations: True):
    if getattr(service, "_brokerdecides1_alert_stopping", False):
        return "STOPPING_NOT_CONSUMED"
    waiting = getattr(service, "_brokerdecides1_alert_waiting", None)
    if waiting is None:
        waiting = OrderedDict()
        service._brokerdecides1_alert_waiting = waiting
    previous = waiting.get(event_id)
    if previous is not None and previous[0] is not delivery:
        return "UNKNOWN_EVENT_IDENTITY_PENDING_RETAINED"
    overflow = previous is None and len(waiting) >= MAX_PENDING
    if not overflow:
        # Replacing a descriptor refreshes its evidence, never its FIFO position.
        waiting[event_id] = (delivery, title, body, still_valid)
    active = getattr(service, "_brokerdecides1_alert_task", None)
    if active is not None and not active.done():
        return "UNKNOWN_QUEUE_FULL_PENDING_RETAINED" if overflow else "BUSY_PENDING_RETAINED"
    selected = None
    observations = {}  # One dispatch scan only; never reuse native/status evidence next poll.
    for pending_id, pending in tuple(waiting.items()):
        candidate, _, _, valid = pending
        try:
            eligible = not candidate.delivered and not candidate.inflight and valid(observations) is True
        except Exception:
            eligible = False
        if not eligible:
            del waiting[pending_id]  # Drop only the descriptor, NEVER consume a hold.
            continue
        selected = (pending_id, pending)
        break
    if selected is None:
        return "UNKNOWN_QUEUE_FULL_PENDING_RETAINED" if overflow else "PENDING_NOT_CURRENT"
    event_id, (delivery, title, body, _) = selected
    sender = getattr(service, "_brokerdecides1_owner_phone", None)
    del waiting[event_id]
    delivery.last_attempt_ms = now_ms
    if not callable(sender):
        LOGGER.error("[BROKERDECIDES1-ALERT-PHONE] event_id=%s outcome=UNKNOWN sender_unconfigured=1", event_id)
        return "PENDING_UNCONFIGURED"
    delivery.inflight = True

    async def deliver():
        try:
            accepted = await asyncio.wait_for(sender(title, body, priority="urgent", tags="rotating_light"), 20)
            if accepted is True:
                delivery.delivered = True
            else:
                LOGGER.error("[BROKERDECIDES1-ALERT-PHONE] event_id=%s outcome=UNKNOWN publisher_unconfirmed=1", event_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error("[BROKERDECIDES1-ALERT-PHONE] event_id=%s outcome=UNKNOWN failure_type=%s", event_id, type(exc).__name__)
        finally:
            delivery.inflight = False

    task = asyncio.create_task(deliver(), name="brokerdecides1-alert-phone")
    service._brokerdecides1_alert_task = task

    def finished(done):
        try:
            done.exception()
        except asyncio.CancelledError:
            pass
        # Also covers cancellation before the coroutine's first instruction.
        delivery.inflight = False
        if getattr(service, "_brokerdecides1_alert_task", None) is done:
            service._brokerdecides1_alert_task = None

    task.add_done_callback(finished)
    return "SCHEDULED_NOT_DELIVERED"


async def stop_alert_transport(service):
    service._brokerdecides1_alert_stopping = True
    waiting = getattr(service, "_brokerdecides1_alert_waiting", None)
    if waiting is not None:
        waiting.clear()  # Holds remain pending; shutdown does not assert delivery.
    task = getattr(service, "_brokerdecides1_alert_task", None)
    if task is None:
        return "STOPPED"
    if not task.done():
        task.cancel()
    _, pending = await asyncio.wait({task}, timeout=2)
    if pending:
        LOGGER.error("[BROKERDECIDES1-ALERT-PHONE] outcome=UNKNOWN owned_task_did_not_stop=1")
        return "UNKNOWN_TASK_DID_NOT_STOP"  # Retain the owned reference, no false reap.
    if getattr(service, "_brokerdecides1_alert_task", None) is task:
        service._brokerdecides1_alert_task = None
    return "STOPPED"
