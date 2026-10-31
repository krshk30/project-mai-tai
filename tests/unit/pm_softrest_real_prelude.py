"""SOURCE UNRUN: C1 02:27 fixture-only real LINE/country preconditions.

Immutable CONTROLLED raw responses, actual parsers, producer projection,
SessionLineRestoration attestation/rebuild/admit/publication and real readers.
No database, provider network, Redis, actor loop or admission callback stub.
"""
import asyncio
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from project_mai_tai.atr_country_route import AtrCountryRouteBook
from project_mai_tai.market_data import keepwatch_history as history_module
from project_mai_tai.market_data.keepwatch_history import (
    history_coverage, project_history, read_keepwatch_history,
)
from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient
from project_mai_tai.momentum_live_sec import CountryRecord, CountryRouter
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.session_line_restore import history_fingerprint
from tests.unit.wbexit3_narrow_boot_fixture import RecordingRefuser

DATA = Path(__file__).parents[1] / "fixtures/pm_softrest_real_prelude"


def inputs(symbol, at):
    manifest = json.loads((DATA / "inputs.json").read_text())
    assert manifest["label"] == "CONTROLLED_NOT_NATIVE_HISTORY_OR_COUNTRY"
    assert symbol == manifest["symbol"] and at.isoformat() == manifest["observation_utc"]
    raw = {}
    for key in ("schwab", "massive"):
        value = (DATA / manifest[key]["file"]).read_bytes()
        assert hashlib.sha256(value).hexdigest() == manifest[key]["sha256"]
        raw[key] = value
    return manifest, raw


class RawSchwab:
    def __init__(self, payload, manifest):
        self.payload, self.manifest, self.calls = payload, manifest, []

    def __call__(self, url):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert parsed.path.endswith("/marketdata/v1/pricehistory")
        assert query["symbol"] == [self.manifest["symbol"]]
        assert query["startDate"] == [str(self.manifest["anchor_ms"])]
        assert query["endDate"] == [str(self.manifest["through_ms"] + 59_999)]
        self.calls.append({"path": parsed.path, "query": query})
        return deepcopy(self.payload)  # Controlled RAW external response only.


class RawMassive:
    def __init__(self, raw, manifest):
        self.raw, self.manifest, self.calls = raw, manifest, []

    def get_aggs(self, symbol, multiplier, timespan, **kwargs):
        assert (symbol, multiplier, timespan) == (self.manifest["symbol"], 1, "minute")
        assert kwargs == {"from_": self.manifest["anchor_ms"],
            "to": self.manifest["through_ms"] + 59_999, "adjusted": True,
            "sort": "asc", "limit": 50_000, "raw": True}
        self.calls.append({"symbol": symbol, **kwargs})
        return SimpleNamespace(data=self.raw)


async def _prepare(settings, manifest, raw):
    symbol, now_ms = manifest["symbol"], manifest["now_ms"]
    denied = RecordingRefuser("CONTROLLED prelude owner phone")
    bot = SchwabV2BotService(settings=settings, session_factory=None, owner_phone_sender=denied)
    strategy = bot.strategy
    assert strategy.settings is settings and strategy._now_ms() == now_ms
    assert settings.strategy_schwab_1m_v2_line_chart_restoration_enabled is True
    assert settings.strategy_schwab_1m_v2_country_route_enabled is True
    assert settings.strategy_schwab_1m_v2_keepwatch1_enabled is True
    assert bot.session_factory is bot.redis is bot.rest_client is bot.streamer is None
    # Explicit CONTROLLED scanner membership/readability operands. No held-only
    # subscription is promoted to entry membership; no native DB fact is claimed.
    bot._watchlist = {symbol}
    bot._entry_watch_symbols = {symbol}
    bot._boot_exclusion_sources_readable = True
    bot._line_epoch = 0  # Deterministic fresh fixture epoch, before any observation.
    bot._keepwatch.update(manifest["anchor_ms"], {symbol}, now_ms, {})
    bot._sync_line_epochs()
    state = strategy.watchlist_state(symbol)
    ledger = bot._line_sessions[symbol]
    assert not strategy.line_buy_ready(symbol)

    country = manifest["country"]
    router = CountryRouter(SimpleNamespace())  # No start/reload/SEC/network.
    router._records[symbol] = CountryRecord(symbol, country["cik"], country["business_country"],
        country["incorporation"], country["is_us"], datetime.fromisoformat(country["as_of"]),
        country["status"], datetime.fromisoformat(country["lookup_day"]).date(), country["source_sha256"])
    book = AtrCountryRouteBook(settings, router.route_for)

    async def external_delivery_forbidden(*args, **kwargs):
        raise AssertionError("CONTROLLED raw history reader must not emit data")
    schwab = SchwabV2RestClient(settings, on_chart_bar=external_delivery_forbidden,
                                on_quote=external_delivery_forbidden)
    schwab_wire = RawSchwab(json.loads(raw["schwab"]), manifest)
    schwab._authorized_get = schwab_wire  # Only the existing raw response boundary.
    massive_wire = RawMassive(raw["massive"], manifest)
    massive = SimpleNamespace(api_key="CONTROLLED_NOT_A_CREDENTIAL", _rest_client=massive_wire)
    # Only the reader module's business clock binding changes, not global time,
    # monotonic deadlines or the source's completeness/ownership predicates.
    with patch.object(history_module, "time", SimpleNamespace(time_ns=lambda: now_ms * 1_000_000)):
        history = read_keepwatch_history(schwab, massive, symbol,
            manifest["anchor_ms"], manifest["through_ms"])
    assert len(schwab_wire.calls) == len(massive_wire.calls) == 1
    assert history.started_ns == history.completed_ns == now_ms * 1_000_000
    assert history.massive_sha256 == manifest["massive"]["sha256"]
    pins = bot._keepwatch_pins_for(symbol, manifest["anchor_ms"])
    bars = project_history(history, pins, now_ms=now_ms)
    assert bars and bars[0].timestamp_ms == manifest["anchor_ms"]
    assert bars[-1].timestamp_ms == manifest["through_ms"]
    assert tuple(bar.timestamp_ms for bar in bars) == tuple(manifest["closed_ids"])
    assert history.empty_minutes == ()
    for bar in bars:
        bot._observe_line_bar(symbol, bar)
        strategy.on_observed_bar(symbol, bar, observation_phase="replay")
    proof = history_coverage(history, tuple(ledger._bars[bar.timestamp_ms] for bar in bars), canonical=True)
    ledger.attest(proof)
    assert proof.complete and proof.prefix_complete and proof.start_ms == ledger.anchor_ms
    assert proof.closed_ids == tuple(manifest["closed_ids"])
    assert proof.bars_sha256 == history_fingerprint(tuple(ledger._bars[stamp] for stamp in proof.closed_ids))
    assert ledger.prepare() is not None
    # Real typed provider proof is the explicit fixture preimage, matching the
    # existing accepted producer-pin fixture. Entry gates/readers remain real.
    bot._keepwatch_proven[symbol] = history
    bot._keepwatch_record_halt_binding(symbol, ledger, history)
    assert await bot._rebuild_session_line(symbol, ledger)
    published = bot._line_published[symbol]
    assert state.atr_state == "short"  # Real controlled SELL math, no invented wait-for-SELL release.
    assert ledger.admit(published) is not None
    assert published.request.epoch == ledger.epoch
    assert published.request.revision == ledger.revision
    assert published.request.current_bar_ms == manifest["through_ms"] == state.bars[-1].timestamp_ms
    assert bot._line_snapshot_current(symbol) and bot._entry_membership_allowed(symbol)
    assert bot._keepwatch_entry_allowed(symbol) and strategy.line_buy_ready(symbol)
    assert strategy._line_readiness == bot._line_buy_ready
    assert strategy._line_version_reader == bot._line_version and bot._line_version(symbol)
    # Bind the actual current opportunity after historical reconstruction.
    # A reconstructed pre-07 bar cannot pin this new controlled 07:40 arm to
    # the earlier session's venue. Country routing stayed ON/unready during
    # replay and no replay draft was admitted.
    assert book.routes == {} and not state.atr_country_route
    strategy.configure_country_routing(book)
    selected = strategy._country_entry_selection(state)
    assert selected.broker == "schwab" and selected.account == settings.strategy_schwab_1m_v2_account_name
    assert selected.segment_id == state.fanout_segment_id > 0
    assert state.atr_country_route == selected.metadata()
    assert not strategy._pending_intents and not strategy._pending_webull_fanout_intents
    assert not bot._tasks and not bot._eh_stream_emit_tasks and not bot._keepwatch_native_reads
    assert denied.calls == []
    bot._controlled_pm_prelude_receipt = {"label": manifest["label"], "clock_ms": now_ms,
        "coverage_sha256": proof.bars_sha256, "source_sha256": history.massive_sha256,
        "anchor_ms": ledger.anchor_ms, "closed_ids": list(proof.closed_ids),
        "epoch": ledger.epoch, "revision": ledger.revision, "version": bot._line_version(symbol),
        "membership": "CONTROLLED_SCANNER_SELECTED", "country": selected.metadata(),
        "raw_schwab_requests": schwab_wire.calls, "raw_massive_requests": massive_wire.calls,
        "DB_provider_Redis_actors": "NONE"}
    return strategy, state


def prepare_real_line_country(settings, *, symbol, at):
    """Join a bounded setup thread, including its actual rebuild executor.

    armed() is also used inside async tests; never nest asyncio.run in their
    loop or publish a replacement synthetic readiness boolean. Runner closes
    its default executor before this non-daemon thread physically terminates.
    """
    manifest, raw = inputs(symbol, at)
    result, failure = [], []
    def setup():
        try:
            with asyncio.Runner() as runner:
                result.append(runner.run(_prepare(settings, manifest, raw)))
        except BaseException as exc:
            failure.append(exc)
    thread = Thread(target=setup, name="CONTROLLED-pm-real-prelude", daemon=False)
    thread.start()
    primary = None
    while thread.is_alive():
        try:
            thread.join(0.05)
        except BaseException as exc:
            primary = primary or exc
    if primary is not None:
        for exc in failure:
            primary.add_note("setup terminal failure: " + repr(exc))
        raise primary
    if failure:
        raise failure[0]
    assert len(result) == 1
    return result[0]
