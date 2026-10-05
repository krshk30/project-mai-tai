"""Before/after ownership from the complete census, isolated SQLite and no HTTP."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import UUID

import pytest

sys.path.insert(0, str(Path.cwd()))

from project_mai_tai.broker_adapters.atr_buy_readback import schwab_buy_readback
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, _request, old_buy_proven_clear
from tests.unit.test_rpg1_runtime import feedback
from tests.unit.test_rpgstuck1_startup import (
    BROKER, RECORDED, real_bot_startup, real_oms_startup, startup_harness, tokenless_open,
)


async def replay():
    with pytest.MonkeyPatch.context() as monkeypatch:
        h = await startup_harness(monkeypatch, with_deferred=True)
        journal = HandoffJournal(h.factory)
        token = UUID("ff6464ff-d65e-5657-8f47-1dc8d7b053c3")
        body = next(row["body"] for row in BROKER["orders"] if row["body"]["status"] == "REJECTED")
        h.adapter.override = schwab_buy_readback(_request(journal.read(token)["old"]), body)
        before = {str(token): job for token, job in journal.jobs()}
        await real_bot_startup(monkeypatch, h)
        await real_oms_startup(monkeypatch, h)
        proof_stage = {str(token): job for token, job in journal.jobs()}
        h.strategy._entries_held = True
        await feedback(h)
        await feedback(h)
        h.strategy._entries_held = False
        after = {str(token): job for token, job in journal.jobs()}
        assert len(before) == len(proof_stage) == len(after) == 8
        result = []
        for row in RECORDED["tickets"]:
            token, old = row["id"], row["payload"]["old"]
            state = h.strategy.watchlist_state(old["symbol"])
            blocked = h.strategy._rpg_entry_owned(state, account=old["broker_account_name"])
            refusal = h.service._rpg_open_refusal(tokenless_open(state.symbol, old["broker_account_name"]))
            assert not blocked and refusal is None and old_buy_proven_clear(after[token])
            result.append({"id": token, "symbol": old["symbol"], "account": old["broker_account_name"],
                "before_phase": before[token]["phase"], "before_reason": before[token]["reason"],
                "before_old_clear": old_buy_proven_clear(before[token]),
                "proof_stage_phase": proof_stage[token]["phase"], "proof_stage_reason": proof_stage[token]["reason"],
                "after_phase": after[token]["phase"], "after_reason": after[token]["reason"],
                "old_clear": old_buy_proven_clear(after[token]), "entry_owned": blocked, "tokenless_refusal": refusal,
                "old_client": after[token]["old"]["client_order_id"], "reads": after[token]["reads"]})
        assert len(h.adapter.reads) == 1 and not h.adapter.opens and not h.adapter.cancels
        Path("docs/review-artifacts/rpgstuck1/startup-dispositions.json").write_text(
            json.dumps({"limits": "RPG ownership permission only; current gates, NFQ, broker policy still apply. "
                "Local handoffs ended through a controlled entry hold; no orders placed in this collector.",
                "recorded_read_at": RECORDED["read_at"], "broker_read_at": BROKER["read_at"],
                "dispositions": result}, indent=2) + "\n")


asyncio.run(replay())
