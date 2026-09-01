"""A Webull resting entry fills BARE — this is what puts a real stop at the broker afterwards.

⛔⭐⭐ WHY IT MUST EXIST. Webull refuses a stop-limit master carrying a bracket (Probe W shape B,
2026-08-13: HTTP 417 `invalid order_type`). So a resting Webull entry cannot bring its protection
with it. Without this attach step the position runs on SOFTWARE-ONLY stops for its entire life —
nothing sitting at the broker at all.

⛔⭐⭐ THE SHAPE PARSES — IT IS **NOT** BROKER-PROVEN (corrected 2026-08-19, B6):
    [STOP_PROFIT, STOP_LOSS] with NO master      -> HTTP 200 (PREVIEW ONLY)  <- what we send
    [OCO, OCO]                                   -> 417 invalid combo_type
    [STOP_LOSS_PROFIT] (one leg, both prices)    -> 417 invalid combo_type
⛔ `preview_order` does NOT validate position backing — it returned 200 for this payload while the
account was FLAT. Probe W4 proved the shape PARSES, never that it PLACES. The 417s remain
informative; the 200 does not. And the live record disagrees with the old label: this is the #689
attach path, which has NEVER once succeeded (zero `[WEBULL-PROTECT-ATTACHED]` ever).

⛔ TWO WAYS THIS LOSES MONEY QUIETLY, both pinned below:
   1. the attach fails and nobody is told  -> holding with no stop
   2. the two legs are not linked          -> stop fills, target survives, account goes SHORT
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.oms import service as svc

RTH_NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)  # Tuesday 10:00 ET


@pytest.fixture(autouse=True)
def _inject_rth_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "utcnow", lambda: RTH_NOW)


# ------------------------------------------------------------------ the payload the broker accepts
def _req(**md):
    meta = {"bracket_target_price": "5.10", "bracket_stop_price": "4.75"}
    meta.update(md)
    return SimpleNamespace(
        client_order_id="coid-1",
        symbol="TEST",
        side="sell",
        quantity=Decimal("2"),
        metadata=meta,
        time_in_force="day",
    )


def _adapter() -> WebullBrokerAdapter:
    a = object.__new__(WebullBrokerAdapter)
    return a


def test_the_pair_has_NO_master_leg() -> None:
    """The whole point: it protects a position we already hold, so there is no entry to lead it."""
    legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req())
    assert len(legs) == 2
    assert {leg["combo_type"] for leg in legs} == {"STOP_PROFIT", "STOP_LOSS"}
    assert all(leg["combo_type"] != "MASTER" for leg in legs)


def test_it_uses_the_EXACT_combo_tags_the_broker_accepted() -> None:
    """⛔ `OCO`/`OCO` and a single `STOP_LOSS_PROFIT` were both 417'd by Webull on 2026-08-13.
    Only STOP_PROFIT + STOP_LOSS passed. Changing these tags breaks every attach."""
    legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req())
    tags = [leg["combo_type"] for leg in legs]
    assert "OCO" not in tags and "STOP_LOSS_PROFIT" not in tags


def test_both_legs_are_SELLs_with_the_right_prices() -> None:
    legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req())
    by = {leg["combo_type"]: leg for leg in legs}
    assert by["STOP_PROFIT"]["side"] == "SELL"
    assert by["STOP_PROFIT"]["order_type"] == "LIMIT"
    assert by["STOP_PROFIT"]["limit_price"] == "5.10"
    assert by["STOP_LOSS"]["side"] == "SELL"
    assert by["STOP_LOSS"]["order_type"] == "STOP_LOSS"
    assert by["STOP_LOSS"]["stop_price"] == "4.75"


def test_neither_leg_id_can_exceed_the_40_CHAR_BROKER_CAP() -> None:
    """⛔ Webull 417s a client_order_id over 40 chars, and the attach's own base is
    `<strategy>-<SYM>-protect-<12hex>` = 39 for a 5-char symbol. A bare f"{coid}T" lands EXACTLY on
    the cap and goes OVER it for anything longer -- so the pair could never place, on the one path
    that is a bare position's only protection. Pin the cap, not the arithmetic that happens to fit
    today.
    """
    for base in (
        "schwab_1m_v2-XHG-protect-0123456789ab",  # 3-char symbol
        "schwab_1m_v2-ABCDE-protect-0123456789ab",  # 5-char symbol -> 39
        "x" * 40,  # already AT the cap
        "y" * 80,
    ):  # absurd, must still be bounded
        req = _req()
        req.client_order_id = base
        legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), req)
        ids = [leg["client_order_id"] for leg in legs]
        for cid in ids:
            assert len(cid) <= 40, f"{cid!r} is {len(cid)} chars — the broker will 417 it"
        assert ids[0] != ids[1], "the two legs must never collide on one id"


def test_half_a_pair_is_REFUSED_rather_than_sent() -> None:
    """⛔ One leg alone is an unpaired sell reserving the shares — the E5/NXTC oversell shape."""
    for missing in ("bracket_target_price", "bracket_stop_price"):
        req = _req()
        del req.metadata[missing]
        with pytest.raises(RuntimeError, match="missing metadata"):
            WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), req)


def test_the_legs_share_ONE_combo_id_so_the_broker_links_them() -> None:
    """⛔ THE SHORT-POSITION GUARD. One `client_combo_order_id` is what makes the broker cancel the
    survivor when one leg fills. Without it a filled stop leaves the target working against shares
    we no longer own."""
    src = inspect.getsource(WebullBrokerAdapter._submit_exit_pair_blocking)
    assert "client_combo_order_id=request.client_order_id" in src


def test_the_pair_routes_to_the_combo_endpoint_only_when_asked() -> None:
    """Every existing path must be byte-identical unless the caller sets the flag."""
    src = inspect.getsource(WebullBrokerAdapter.submit_order)
    assert "_is_exit_only_pair(request)" in src
    gate = inspect.getsource(WebullBrokerAdapter._is_exit_only_pair)
    assert "webull_exit_only_pair" in gate


# ----------------------------------------------------------------------------- the OMS attach path
class _Adapter:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def submit_order(self, request):
        self.calls.append(request)
        o = self.outcomes.pop(0) if self.outcomes else "accepted"
        if isinstance(o, Exception):
            raise o
        return [SimpleNamespace(event_type=o, reason="scripted")]


def _svc(adapter):
    s = object.__new__(svc.OmsRiskService)
    s.settings = SimpleNamespace(
        oms_v2_cw_target_pct=2.0,
        oms_v2_cw_hard_stop_pct=5.0,
        oms_webull_protect_attempts=3,
        oms_webull_protect_interval_seconds=0.0,
    )
    s.logger = logging.getLogger("test-attach")
    s.broker_adapter = adapter
    s._webull_protect_base = {}
    return s


def _run(s):
    return asyncio.run(
        s._attach_webull_protection(
            broker_account_name="live:orb",
            symbol="TEST",
            quantity=1,
            entry_price=5.0,
            strategy_code="schwab_1m_v2",
        )
    )


def test_it_attaches_and_stops(caplog: pytest.LogCaptureFixture) -> None:
    a = _Adapter(["accepted"])
    with caplog.at_level(logging.INFO):
        _run(_svc(a))
    assert len(a.calls) == 1
    assert "[WEBULL-PROTECT-ATTACHED]" in caplog.text
    md = a.calls[0].metadata
    assert md["bracket_target_price"] == "5.1000"  # +2%
    assert md["bracket_stop_price"] == "4.7500"  # -5%


def test_it_REMEMBERS_the_base_id_so_the_pair_can_later_be_RELEASED() -> None:
    """⛔ THE LEGS ARE UNQUERYABLE. They are broker-created and never land in `broker_orders`, so
    this coid is the only handle that will ever exist on them. Forget it and the pair can be placed
    but never cancelled — which means the software ladder can never sell into it, and we are back to
    the 58-reject XHG storm with no way to tell from the outside.

    A mutation that dropped this line left every other attach test green.
    """
    a = _Adapter(["accepted"])
    s = _svc(a)
    _run(s)
    assert s._webull_protect_base[("live:orb", "TEST")] == a.calls[0].client_order_id


def test_a_FAILED_attach_records_NO_base_id() -> None:
    """Nothing is resting, so there is nothing to release. A recorded id here would send cancels at
    an order that never existed and then latch the release as done."""
    a = _Adapter(["rejected", "rejected", "rejected"])
    s = _svc(a)
    _run(s)
    assert ("live:orb", "TEST") not in s._webull_protect_base


def test_it_RETRIES_a_refusal() -> None:
    a = _Adapter(["rejected", "rejected", "accepted"])
    _run(_svc(a))
    assert len(a.calls) == 3, "must keep trying — an unprotected position is the harm"


def test_a_raise_does_not_end_it() -> None:
    a = _Adapter([RuntimeError("network"), "accepted"])
    _run(_svc(a))
    assert len(a.calls) == 2


def test_total_failure_WARNS_and_says_the_position_is_unprotected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⛔ Silence here means holding with no stop and nobody knowing."""
    a = _Adapter(["rejected", "rejected", "rejected"])
    with caplog.at_level(logging.WARNING):
        _run(_svc(a))
    assert "[WEBULL-PROTECT-FAILED]" in caplog.text
    assert "NO BROKER-SIDE STOP" in caplog.text
    assert "TEST" in caplog.text


def test_only_a_BARE_fill_triggers_it() -> None:
    """⛔ A bracketed entry already has protection live at the fill; a second pair would reserve the
    shares twice and draw an oversell refusal."""
    whole = inspect.getsource(svc)
    seg = whole.split("[OMS-V2-MANAGED-OPEN]")[1][:1500]
    assert "native_oco_bracket" in seg and '!= "true"' in seg
    assert "fanout_leg" in seg


def test_the_attach_runs_OFF_the_fill_path() -> None:
    """It sleeps between retries; blocking the fill path with it would delay real executions."""
    src = inspect.getsource(svc.OmsRiskService._spawn_webull_protection)
    assert "ensure_future" in src


def test_non_rth_spawn_is_refused_before_a_task_or_broker_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _Adapter(["accepted"])
    service = _svc(adapter)
    monkeypatch.setattr(svc, "_is_regular_market_session", lambda: False)

    task = service._spawn_webull_protection(
        broker_account_name="live:orb",
        symbol="TEST",
        quantity=1,
        entry_price=5.0,
        strategy_code="schwab_1m_v2",
    )

    assert task is None
    assert adapter.calls == []
    assert "_webull_protect_tasks" not in service.__dict__


def test_non_rth_direct_attach_is_refused_before_broker_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _Adapter(["accepted"])
    service = _svc(adapter)
    monkeypatch.setattr(svc, "_is_regular_market_session", lambda: False)

    asyncio.run(
        service._attach_webull_protection(
            broker_account_name="live:orb",
            symbol="TEST",
            quantity=1,
            entry_price=5.0,
            strategy_code="schwab_1m_v2",
        )
    )

    assert adapter.calls == []


# ==================================================================================================
# §167 — ONE COUNTED LINE PER BARE WEBULL FILL.
#
# ⛔⭐⭐ The exposure must be counted where it is CREATED. #689's attach has never once succeeded, so
# a bare fill is uncovered from the instant it fills, and the count cannot be read off
# [WEBULL-PROTECT-FAILED]: two attach sequences can interleave on ONE position (STKH 08-14 —
# 1/3 2/3 1/3 3/3 FAILED 2/3 3/3 FAILED, one fill, two FAILED lines), so that count runs ~0.6
# positions per line. One line per FILL is 1:1 by construction.
# ==================================================================================================


def _counter_svc():
    s = svc.OmsRiskService.__new__(svc.OmsRiskService)
    s.logger = logging.getLogger("test-bare-fill")
    return s


class _ManagedFillStore:
    def __init__(self) -> None:
        self.row = None

    def get_open_managed_position(self, _session, **_kwargs):
        return self.row

    def create_managed_position(self, _session, **kwargs) -> None:
        self.row = SimpleNamespace(**kwargs)


def _managed_fill_svc():
    service = svc.OmsRiskService.__new__(svc.OmsRiskService)
    service.settings = SimpleNamespace(
        oms_v2_exit_management_enabled=True,
        oms_settlement_probe_enabled=False,
    )
    service.store = _ManagedFillStore()
    service._managed_v2_symbols = set()
    service.logger = logging.getLogger("test-webull-premarket-fill")
    spawned: list[dict[str, object]] = []
    service._spawn_webull_protection = lambda **kwargs: spawned.append(kwargs)
    return service, spawned


def _apply_bare_webull_fill(service) -> None:
    service._apply_managed_position_after_fill(
        session=object(),
        strategy_code="schwab_1m_v2",
        broker_account_name="live:orb",
        symbol="TEST",
        side="buy",
        intent_type="open",
        quantity=Decimal("1"),
        price=Decimal("5.00"),
        metadata={"fanout_leg": "webull"},
        entry_client_order_id="entry-1",
    )


def test_premarket_fill_emits_exactly_one_counted_marker_and_does_not_attach(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime as _dt

    service, spawned = _managed_fill_svc()
    monkeypatch.setattr(svc, "utcnow", lambda: _dt(2026, 9, 1, 12, 0, tzinfo=UTC))  # 08:00 ET

    with caplog.at_level(logging.WARNING):
        _apply_bare_webull_fill(service)

    lines = [r.getMessage() for r in caplog.records if "WEBULL-PREMARKET-UNPROTECTED" in r.getMessage()]
    assert len(lines) == 1
    assert "unprotected_fills_this_session=1" in lines[0]
    assert "session_et=2026-09-01" in lines[0]
    assert "WEBULL-BARE-FILL" not in caplog.text
    assert spawned == []


def test_rth_fill_keeps_the_existing_bare_count_and_attach(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime as _dt

    service, spawned = _managed_fill_svc()
    monkeypatch.setattr(svc, "utcnow", lambda: _dt(2026, 9, 1, 14, 0, tzinfo=UTC))  # 10:00 ET

    with caplog.at_level(logging.WARNING):
        _apply_bare_webull_fill(service)

    assert "[WEBULL-BARE-FILL]" in caplog.text
    assert "WEBULL-PREMARKET-UNPROTECTED" not in caplog.text
    assert len(spawned) == 1


def test_premarket_counter_is_one_line_per_fill_with_a_running_session_count(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime as _dt

    service = _counter_svc()
    monkeypatch.setattr(svc, "utcnow", lambda: _dt(2026, 9, 1, 12, 0, tzinfo=UTC))

    with caplog.at_level(logging.WARNING):
        counts = [
            service._count_premarket_unprotected_webull_fill(
                symbol=symbol,
                broker_account_name="live:orb",
                quantity=1,
                entry_price=1.0,
            )
            for symbol in ("AAA", "BBB", "CCC")
        ]

    lines = [r.getMessage() for r in caplog.records if "WEBULL-PREMARKET-UNPROTECTED" in r.getMessage()]
    assert counts == [1, 2, 3]
    assert len(lines) == 3
    assert "unprotected_fills_this_session=3" in lines[-1]


def test_a_bare_fill_emits_one_counted_line(caplog: pytest.LogCaptureFixture) -> None:
    s = _counter_svc()
    with caplog.at_level(logging.WARNING):
        n = s._count_bare_webull_fill(
            symbol="XHG", broker_account_name="live:orb", quantity=1, entry_price=2.4487
        )
    assert n == 1
    assert "[WEBULL-BARE-FILL]" in caplog.text
    assert "XHG" in caplog.text and "live:orb" in caplog.text
    assert "NO BROKER-SIDE BRACKET" in caplog.text
    assert "n=1 bare fill(s) this session" in caplog.text


def test_the_count_is_one_per_FILL_not_per_attach_attempt() -> None:
    """⛔ 1:1 by construction — three fills, three counts, regardless of what the attach does."""
    s = _counter_svc()
    counts = [
        s._count_bare_webull_fill(
            symbol=sym, broker_account_name="live:orb", quantity=1, entry_price=1.0
        )
        for sym in ("AAA", "BBB", "CCC")
    ]
    assert counts == [1, 2, 3]


def test_the_count_RESETS_on_a_new_ET_session(monkeypatch) -> None:
    """⛔ A since-boot counter reads as a day's exposure to anyone who does not know when the process
    started — the ambiguity the seed census had to add a denominator to fix."""
    s = _counter_svc()
    from datetime import UTC, datetime as _dt

    monkeypatch.setattr(svc, "utcnow", lambda: _dt(2026, 8, 19, 18, 0, tzinfo=UTC))
    assert (
        s._count_bare_webull_fill(
            symbol="AAA", broker_account_name="live:orb", quantity=1, entry_price=1.0
        )
        == 1
    )
    assert (
        s._count_bare_webull_fill(
            symbol="BBB", broker_account_name="live:orb", quantity=1, entry_price=1.0
        )
        == 2
    )
    monkeypatch.setattr(svc, "utcnow", lambda: _dt(2026, 8, 20, 18, 0, tzinfo=UTC))
    assert (
        s._count_bare_webull_fill(
            symbol="CCC", broker_account_name="live:orb", quantity=1, entry_price=1.0
        )
        == 1
    ), "a new ET session must restart the count"


def test_the_session_date_is_ON_the_line(caplog: pytest.LogCaptureFixture, monkeypatch) -> None:
    """The denominator is only readable if the line says which session it counts."""
    from datetime import UTC, datetime as _dt

    monkeypatch.setattr(svc, "utcnow", lambda: _dt(2026, 8, 19, 18, 0, tzinfo=UTC))
    with caplog.at_level(logging.WARNING):
        _counter_svc()._count_bare_webull_fill(
            symbol="AAA", broker_account_name="live:orb", quantity=1, entry_price=1.0
        )
    assert "2026-08-19" in caplog.text


def test_the_line_is_a_WARNING_not_info(caplog: pytest.LogCaptureFixture) -> None:
    """⛔ It records an UNCOVERED real-money position. INFO is where this would go unread."""
    with caplog.at_level(logging.WARNING):
        _counter_svc()._count_bare_webull_fill(
            symbol="AAA", broker_account_name="live:orb", quantity=1, entry_price=1.0
        )
    assert any(
        r.levelno >= logging.WARNING and "WEBULL-BARE-FILL" in r.getMessage()
        for r in caplog.records
    )


def test_the_counted_line_sits_on_the_BARE_branch_at_the_fill() -> None:
    """⛔ It must fire on the same condition the attach does — a bracketed fill is already covered."""
    # ⛔ Anchored on the BARE-BRANCH CONDITION rather than on a log marker, and the window is sized
    # to span the comment block between the branch and the two calls. The first version of this test
    # used a 2000-char window off `[OMS-V2-MANAGED-OPEN]` and failed — NOT because the marker is
    # ambiguous (it occurs exactly once) but because the window was too short to reach the calls.
    # Anchoring on the condition ties the assertion to the thing it actually claims to check.
    # ⛔⭐ THE ANCHOR MUST BE UNIQUE, AND MINE STOPPED BEING SO. I first anchored on
    # `[OMS-V2-MANAGED-OPEN]` with too small a window; "fixed" it by anchoring on the
    # native_oco_bracket predicate; then P12 added a method containing that SAME predicate, so
    # `.index()` began matching the method instead of the fill branch and the test broke on a
    # substring-not-found. An ambiguity fix that rebuilds the ambiguity. `[OMS-V2-MANAGED-OPEN]`
    # occurs exactly once — verified — so it is the unique anchor; the window is simply sized to
    # reach the calls.
    whole = inspect.getsource(svc)
    assert whole.count("[OMS-V2-MANAGED-OPEN]") == 1, "the anchor must stay unique"
    idx = whole.index("[OMS-V2-MANAGED-OPEN]")
    seg = whole[idx : idx + 3000]
    assert "_count_bare_webull_fill" in seg, "the count must sit on the bare branch"
    assert seg.index("_count_bare_webull_fill") < seg.index("_spawn_webull_protection"), (
        "the exposure must be counted BEFORE the attach is attempted, never conditional on it"
    )
