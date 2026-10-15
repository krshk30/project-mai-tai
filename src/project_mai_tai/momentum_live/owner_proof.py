from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import asyncio
import json
import os
from pathlib import Path
import re
import time
from typing import Awaitable, Callable

from project_mai_tai.events import MarketDataSubscriptionEvent, MarketDataSubscriptionPayload
from project_mai_tai.momentum_live.admission import ACTIVE_STATES, EpisodeLedger, Refusal, STRATEGY_CODE


def episode_payload(book: EpisodeLedger, episode_id: str) -> dict:
    episode = book.episodes[episode_id]
    row = asdict(episode)
    row.update(source_ids=sorted(episode.source_ids), route=episode.route.value if episode.route else None,
               filled=str(episode.filled), exited=str(episode.exited))
    return row


def owner_proof(book: EpisodeLedger, *, observed_ns: int, producer_instance_id: str,
                account_names: tuple[str, str], bindings: dict[str, str],
                entry_admission_enabled: bool) -> dict:
    """Pure bounded proof construction; caller supplies restored OMS ownership."""
    if (not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", producer_instance_id)
            or type(observed_ns) is not int or observed_ns <= 0
            or type(entry_admission_enabled) is not bool
            or len(set(account_names)) != 2 or any(not name.startswith("live:") for name in account_names)):
        raise Refusal("proof_producer_or_account_invalid")
    identities = sorted({identity for values in book._active_by_symbol.values() for identity in values})
    if len(identities) > 2:
        raise Refusal("proof_owner_capacity")
    episodes = []
    for identity in identities:
        episode = book.episodes[identity]
        account = bindings.get(identity)
        if (account not in account_names or episode.state not in ACTIVE_STATES
                or not episode.filled.is_finite() or not episode.exited.is_finite()
                or not Decimal(0) <= episode.exited <= episode.filled):
            raise Refusal("proof_episode_owner_invalid")
        episodes.append({"episode_id": identity, "symbol": episode.symbol, "account_name": account,
                         "state": episode.state, "filled_quantity": str(episode.filled),
                         "exited_quantity": str(episode.exited)})
    return {"version": 1, "source_service": "oms", "producer_instance_id": producer_instance_id,
            "strategy_code": STRATEGY_CODE, "observed_ns": observed_ns, "restore_ready": book.ready,
            "entry_admission_enabled": bool(entry_admission_enabled), "account_names": sorted(account_names),
            "episodes": episodes, "quote_symbols": sorted({episode["symbol"] for episode in episodes})}


async def publish_owner_proof(redis, prefix: str, proof: dict) -> None:
    await redis.xadd(f"{prefix}:momentum-live-owner-proof", {"data": json.dumps(proof)},
                     maxlen=64, approximate=False)


async def replace_quote_owner(redis, prefix: str, symbols: set[str], *, maxlen: int) -> None:
    """Existing owner-union control stream; this is a request, not feed coverage."""
    if len(symbols) > 2 or any(not re.fullmatch(r"[A-Z]{1,5}", symbol) for symbol in symbols):
        raise Refusal("quote_owner_scope_invalid")
    event = MarketDataSubscriptionEvent(source_service="oms", payload=MarketDataSubscriptionPayload(
        consumer_name="momentum-live", mode="replace", symbols=sorted(symbols)))
    await redis.xadd(f"{prefix}:market-data-subscriptions", {"data": event.model_dump_json()},
                     maxlen=maxlen, approximate=True)


def current_process_identity() -> str:
    """Linux process start ticks bind the proof to the guard's actual OMS PID."""
    pid = os.getpid()
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    # comm may contain spaces or parentheses; fields after its final ')' start at 3.
    fields = stat.rsplit(")", 1)[1].split()
    ticks = int(fields[19])
    if ticks <= 0:
        raise Refusal("proof_process_start_unavailable")
    return f"{pid}:{ticks}"


class OwnerProofPublisher:
    """One cadence task, started only after durable restore; never a quote callback."""

    def __init__(self, redis, prefix: str, book: EpisodeLedger, *,
                 producer_instance_id: str, account_names: tuple[str, str],
                 bindings: dict[str, str], enabled: Callable[[], bool],
                 replace_owner: Callable[[set[str]], Awaitable[None]],
                 state_lock: asyncio.Lock, clock_ns: Callable[[], int] = time.time_ns):
        self.redis = redis
        self.prefix = prefix
        self.book = book
        self.identity = producer_instance_id
        self.accounts = account_names
        self.bindings = bindings
        self.enabled = enabled
        self.replace_owner = replace_owner
        self.state_lock = state_lock
        self.clock_ns = clock_ns
        self.last_success_ns: int | None = None

    async def publish_once(self) -> None:
        async with self.state_lock:
            await self.publish_locked()

    async def publish_locked(self) -> None:
        """Caller holds state_lock; also used by a cold-quote reservation."""
        if not self.book.ready:
            raise Refusal("proof_restore_not_ready")
        proof = owner_proof(self.book, observed_ns=self.clock_ns(),
            producer_instance_id=self.identity, account_names=self.accounts,
            bindings=self.bindings, entry_admission_enabled=self.enabled())
        # Replace (including empty) before proof. Failure cannot mint a fresh
        # guard receipt for an unregistered or uncleared quote owner.
        async with asyncio.timeout(1):
            await self.replace_owner(set(proof["quote_symbols"]))
            await publish_owner_proof(self.redis, self.prefix, proof)
        self.last_success_ns = proof["observed_ns"]

    async def run(self) -> None:
        # Initial failure must reach startup: an old OMS must never look restored.
        await self.publish_once()
        while True:
            await asyncio.sleep(1)
            try:
                await self.publish_once()
            except (Refusal, OSError, TimeoutError):
                # No new success timestamp or synthetic fresh proof on failure.
                continue
