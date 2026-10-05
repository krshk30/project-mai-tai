"""Recorded RETO runtime exhaustion, without a restart or periodic unknown work."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from uuid import UUID

import pytest
from sqlalchemy import event as sql_event

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback, schwab_buy_readback
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, old_buy_proven_clear
from tests.unit.test_rpgstuck1_startup import BROKER, startup_harness, real_bot_startup

RETO = UUID("ff6464ff-d65e-5657-8f47-1dc8d7b053c3")


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["rejected-zero", "unknown"])
@pytest.mark.parametrize("eligibility", ["eligible", "already-claimed", "accepted-row", "no-rebuy"])
async def test_r20_recorded_reto_runtime_exhaustion_wakes_one_proof_then_quiet(monkeypatch, answer, eligibility):
    h = await startup_harness(monkeypatch)
    await real_bot_startup(monkeypatch, h)
    journal = HandoffJournal(h.factory)
    job = journal.read(RETO)
    changes = dict(phase="waiting", reads=29, next_read_at=0, reason="CONTROLLED rewind recorded RETO")
    if eligibility == "already-claimed":
        changes["terminal_rejection_probe_started_at"] = h.clock[0].timestamp()
    elif eligibility == "no-rebuy":
        changes["no_rebuy"] = True
    journal.change(RETO, job["revision"], **changes)
    if eligibility == "accepted-row":
        with h.factory() as session:
            session.get(BrokerOrder, UUID(job["original_order_id"])).status = "accepted"
            session.commit()
    body = deepcopy(next(row["body"] for row in BROKER["orders"] if row["body"]["status"] == "REJECTED"))

    async def broker_read(request):
        h.adapter.reads.append(request)
        assert request.client_order_id == job["old"]["client_order_id"]
        return (schwab_buy_readback(request, body) if answer == "rejected-zero"
                else AtrBuyReadback("unknown", "CONTROLLED exact broker response unknown"))

    monkeypatch.setattr(h.adapter, "read_atr_resting_buy_after_cancel", broker_read)
    statements, scans, ticks, phases, probes = [], [], [], [], []
    sql_event.listen(h.factory.kw["bind"], "before_cursor_execute", lambda *args: statements.append(args[2]))
    original_scan, original_probe = h.service._rpg_retry_jobs, h.service._rpg_probe_rejected_old

    def scan(**kwargs):
        scans.append(kwargs["include_unknown"])
        return original_scan(**kwargs)

    async def probe(token, current):
        probes.append(token)
        return await original_probe(token, current)

    stop = asyncio.Event()
    quiet = []

    async def pause(stop_event, seconds):
        rows, h.service.redis.entries = h.service.redis.entries, []
        for _, data in rows:
            if data.get("event_type") == "atr_reprice_tick":
                if data["token"] == str(RETO):
                    ticks.append(data)
                await h.service._handle_stream_message({"data": json.dumps(data)})
                if data["token"] == str(RETO):
                    phases.append(journal.read(RETO)["phase"])
        if seconds is None:
            if not quiet:
                quiet.append((len(statements), len(ticks), len(scans)))
            else:
                assert (len(statements), len(ticks), len(scans)) == quiet[0]
            quiet.append(None)
            if len(quiet) > 180:
                stop_event.set()
        h.clock[0] += timedelta(seconds=1)

    monkeypatch.setattr(h.service, "_rpg_retry_jobs", scan)
    monkeypatch.setattr(h.service, "_rpg_probe_rejected_old", probe)
    monkeypatch.setattr(h.service, "_rpg_retry_pause", pause)
    await h.service._run_rpg_retry_loop(stop)
    eligible = eligibility == "eligible"
    proven = eligible and answer == "rejected-zero"
    job = journal.read(RETO)
    assert phases[:2] == ["waiting", "held_unknown"]
    assert len(ticks) == (3 if eligible else 2)
    assert probes == ([RETO] if eligible else [])
    assert len(h.adapter.reads) == (2 if eligible else 1)  # Normal read30 plus one proof-only GET.
    assert job["reads"] == 30
    assert job["phase"] == ("refused" if proven else "held_unknown")
    assert old_buy_proven_clear(job) is proven
    if eligible:
        assert job["terminal_rejection_probe_started_at"] is not None
        assert job["terminal_rejection_probe_completed_at"] is not None
        assert job["terminal_rejection_probe_proven"] is proven
    assert scans[0] is True and all(not startup for startup in scans[1:])
    assert len(quiet) == 181 and not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
async def test_held_notice_replay_claims_once_and_preserves_unknown_ownership(monkeypatch):
    h = await startup_harness(monkeypatch)
    journal = HandoffJournal(h.factory)
    job = journal.read(RETO)
    journal.change(RETO, job["revision"], blocked_notice_at=None,
        terminal_rejection_probe_started_at=h.clock[0].timestamp())
    notices = []
    monkeypatch.setattr(h.service.logger, "info", lambda *args: notices.append(args))
    for _ in range(10):
        await h.service._rpg_advance(RETO)
    assert len(notices) == 1
    job = journal.read(RETO)
    assert job["blocked_notice_at"] is not None and job["phase"] == "held_unknown"
    assert not old_buy_proven_clear(job) and not h.adapter.reads and not h.adapter.opens
