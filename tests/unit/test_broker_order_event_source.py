"""Q1 — `broker_order_events.event_source`: who refused, us or the broker.

⛔⭐⭐ EVERY REJECT COUNT BEFORE THIS COLUMN IS CONTAMINATED. `event_type="rejected"` is written
both when the BROKER refused a request we sent and when WE abandoned one that never left the
process. Collapsed into one word, "the broker is rejecting us" and "we are aborting our own
orders" read identically — and they point at completely different code. The 08-19 mirror
investigation turned on exactly this: 720 `rth_resting_mirror` orders looked like Webull refusing
us, and every one was our own adapter guard aborting client-side.

⛔ The discipline these tests exist to hold is the DEFAULT. An unlabelled site must land on
"unknown", never on "broker" — a site nobody has classified must not silently acquire the label
that carries blame. That is how the original contamination happened, one convenient default at a
time. An honest gap is countable; a confident wrong label is not.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.oms.store import OmsStore


# ------------------------------------------------------------------ the default
def test_an_unlabelled_report_is_UNKNOWN_never_broker() -> None:
    """⛔⭐⭐ THE LOAD-BEARING ASSERTION OF THE WHOLE CHANGE."""
    r = ExecutionReport(event_type="rejected", client_order_id="c1")
    assert r.origin == "unknown"
    assert r.origin != "broker", "an unclassified site must never inherit blame by default"


def test_origin_is_carried_verbatim_when_set() -> None:
    for origin in ("broker", "client", "unknown"):
        r = ExecutionReport(event_type="rejected", client_order_id="c1", origin=origin)
        assert r.origin == origin


# ------------------------------------------------------------------ the store carries it through
class _StubSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        pass


class _StubOrder:
    id = "order-1"


@pytest.mark.parametrize("origin", ["broker", "client", "unknown"])
def test_append_order_event_writes_the_reports_origin(origin: str) -> None:
    store = object.__new__(OmsStore)
    session = _StubSession()
    report = ExecutionReport(
        event_type="rejected",
        client_order_id="c1",
        quantity=Decimal("1"),
        origin=origin,
    )
    ev = store.append_order_event(
        session, order=_StubOrder(), report=report, payload={}
    )
    assert ev.event_source == origin


def test_the_store_does_not_INFER_an_origin() -> None:
    """⛔ The store has no way to know who refused. A guess there is indistinguishable from the
    truth downstream, which is the entire defect this column removes."""
    store = object.__new__(OmsStore)
    report = ExecutionReport(event_type="rejected", client_order_id="c1")
    ev = store.append_order_event(
        _StubSession(), order=_StubOrder(), report=report, payload={}
    )
    assert ev.event_source == "unknown", "the store invented a classification it cannot know"


def test_a_report_object_WITHOUT_an_origin_attribute_lands_on_unknown() -> None:
    """⛔⭐⭐ THE CASE THE `getattr` FALLBACK ACTUALLY GUARDS — and the one my first test missed.

    An `ExecutionReport` always HAS `origin`, so passing one never exercises the default; a mutant
    changing `getattr(report, "origin", "unknown")` to `"broker"` survived until this existed. The
    fallback is for a duck-typed or older report object that predates the field. It must land on
    "unknown" like every other unclassified thing — the default must not be blame."""

    class _LegacyReport:  # no `origin` attribute at all
        event_type = "rejected"
        reported_at = None

    store = object.__new__(OmsStore)
    ev = store.append_order_event(
        _StubSession(), order=_StubOrder(), report=_LegacyReport(), payload={}
    )
    assert ev.event_source == "unknown"


# ------------------------------------------------------------------ the name
def test_the_column_is_NOT_called_source() -> None:
    """⛔⭐ `payload["metadata"]["source"]` already exists with an unrelated meaning
    (e.g. "native_oco_child_leg"). A column called `source` would rebuild the very ambiguity this
    column exists to remove — two different "source"s one join apart."""
    from project_mai_tai.db.models import BrokerOrderEvent

    cols = set(BrokerOrderEvent.__table__.columns.keys())
    assert "event_source" in cols
    assert "source" not in cols


# ------------------------------------------------------------------ no unlabelled reject sites
_ADAPTERS = Path(__file__).resolve().parents[2] / "src" / "project_mai_tai" / "broker_adapters"


def test_every_schwab_rejection_site_states_its_origin() -> None:
    """⛔⭐ THE GUARD THAT KEEPS THIS TRUE. A new rejection path added without an origin lands on
    "unknown" — safe, but silently uncountable. The classification is mechanical and the reviewer
    should have to make it deliberately:

        HTTP >= 400            -> "broker"   the venue answered, and the answer was no
        anything before that   -> "client"   it never left the process

    Scoped to the Schwab adapter because that is the account whose reject counts drive decisions.
    """
    src = (_ADAPTERS / "schwab.py").read_text(encoding="utf-8").split("\n")
    unlabelled = [
        i + 1
        for i, line in enumerate(src)
        if re.search(r'event_type="rejected"', line)
        and not any('origin="' in nxt for nxt in src[i + 1 : i + 3])
    ]
    assert not unlabelled, f"rejection sites with no origin= label at lines {unlabelled}"


def test_the_schwab_split_is_not_all_one_word() -> None:
    """⛔ A control. If every site were labelled "client" the column would be useless and the
    tests above would still pass — both classes must actually appear."""
    src = (_ADAPTERS / "schwab.py").read_text(encoding="utf-8")
    assert 'origin="broker"' in src and 'origin="client"' in src


def test_schwab_status_order_report_is_broker_origin() -> None:
    """A Schwab order returned by the venue must not remain in the unknown population."""
    from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter

    adapter = object.__new__(SchwabBrokerAdapter)
    request = OrderRequest(
        client_order_id="coid-status",
        broker_account_name="live:schwab_1m_v2",
        strategy_code="schwab_1m_v2",
        symbol="DAIC",
        side="buy",
        intent_type="open",
        quantity=Decimal("1"),
        reason="test",
    )
    report = adapter._execution_report_from_order(
        request=request,
        order={
            "orderId": "12345",
            "status": "REJECTED",
            "quantity": 1,
            "enteredTime": "2026-08-28T14:30:00Z",
            "statusDescription": "venue refused",
        },
        event_type="rejected",
        broker_order_id="12345",
    )

    assert report.origin == "broker"
    assert report.reason != request.reason


def test_every_schwab_status_order_call_site_uses_the_classified_helper() -> None:
    src = (_ADAPTERS / "schwab.py").read_text(encoding="utf-8")
    assert src.count("self._execution_report_from_order(") == 3
    helper = src[src.index("    def _execution_report_from_order(") :]
    assert 'origin="broker"' in helper


def test_webull_exception_paths_are_DERIVED_never_guessed() -> None:
    """⛔⭐ The honest gap has been CLOSED BY READING THE PATH — which is what its own pin asked for.

    The previous version of this test held the exception sites at "unknown" and said why:
    *"They stay unknown until someone reads that path and can say."* That happened on
    2026-08-21. The exception already carries the evidence — `_exc_reason` reads `http_status`
    and `error_code` off it — so the classification is the SAME mechanical rule this file
    already documents for Schwab:

        HTTP >= 400 (or a venue error_code)  -> "broker"   the venue answered, and it was no
        neither                              -> "unknown"  transport/SDK; still not classifiable

    ⛔ The invariant that mattered is preserved and made STRONGER: an exception site must not
    carry a LITERAL label (that would be the guess the old test was guarding against), it must
    call the derivation. Pinned syntactically here and behaviourally below.
    """
    src = (_ADAPTERS / "webull.py").read_text(encoding="utf-8")
    assert 'origin: str = "unknown"' in src, "the helper must still default to unknown"
    assert src.count('origin="client"') == 3, (
        "only the three pre-flight guards are literal-client: missing account config, "
        "invalid raw BUY stop-limit relationship, and missing instrument id"
    )
    assert '_reject(request, self._exc_reason(exc), origin="' not in src, (
        "an exception-wrapping site was labelled by a LITERAL — that is the guess"
    )
    assert src.count("origin=self._origin_from_exc(exc)") == 4, (
        "every exception-wrapping reject site must DERIVE its origin"
    )


def test_origin_from_exc_labels_broker_only_on_venue_evidence() -> None:
    """★ The behavioural half. A syntactic guard cannot tell a derivation from a constant.

    ⛔ The 'no evidence' case is the one that matters: it must stay UNKNOWN. A classifier that
    returned "broker" for everything would satisfy the source-grep above and reintroduce exactly
    the contamination the column exists to remove.
    """
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    f = WebullBrokerAdapter._origin_from_exc

    class _Exc(Exception):
        def __init__(self, **kw: object) -> None:
            super().__init__("boom")
            for k, v in kw.items():
                setattr(self, k, v)

    # the venue answered — today's live case was http 417
    assert f(_Exc(http_status=417)) == "broker"
    assert f(_Exc(http_status=400)) == "broker"
    assert f(_Exc(error_code="STOP_PRICE_MUST_BE_GREAT_THAN_MARKET_PRICE")) == "broker"
    # ⛔ no venue evidence -> the honest gap, NOT a guess
    assert f(_Exc()) == "unknown"
    assert f(RuntimeError("Webull combo MASTER must be LIMIT or MARKET")) == "unknown"
    assert f(_Exc(http_status=None)) == "unknown"
    assert f(_Exc(http_status="not-a-number")) == "unknown"
    # a 2xx/3xx is not a refusal
    assert f(_Exc(http_status=200)) == "unknown"


def test_a_local_refusal_does_not_wear_the_brokers_name() -> None:
    """★ §196. For a week, 723 of ~728 mirror rejects read `Webull order rejected:
    RuntimeError(...)` — our own guard, prefixed with the venue's name. Only 5 were Webull's.
    A sentence the broker never said is worse than no sentence, because it stops the
    investigation."""
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    r = WebullBrokerAdapter._exc_reason(
        RuntimeError("Webull combo MASTER must be LIMIT or MARKET (a buy-STOP master rejects)")
    )
    assert not r.startswith("Webull order rejected:"), (
        "a locally-raised refusal is still being presented as the broker's words"
    )
    assert "LOCAL refusal" in r

    class _VenueExc(Exception):
        http_status = 417
        error_code = "STOP_PRICE_MUST_BE_GREAT_THAN_MARKET_PRICE"
        error_msg = "STOP_PRICE_MUST_BE_GREAT_THAN_MARKET_PRICE"

    v = WebullBrokerAdapter._exc_reason(_VenueExc())
    assert v.startswith("Webull order rejected:"), "a real venue refusal must keep the venue's name"
    assert "417" in v


def test_status_poll_never_substitutes_our_intent_text_on_a_refusal() -> None:
    """★ §196, the live SUGP case. Webull answered REJECTED and gave no failure_reason; the
    old code put our intent string in `reject_reason`, where it read as the venue's verdict."""
    from project_mai_tai.broker_adapters.protocols import OrderRequest
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    req = OrderRequest(
        client_order_id="schwab_1m_v2-SUGP-open-a95522e458d7",
        symbol="SUGP",
        side="buy",
        quantity=Decimal("2"),
        order_type="STOP_LIMIT",
        broker_account_name="live:orb",
        strategy_code="schwab_1m_v2",
        intent_type="open",
        reason="schwab_1m_v2 ATR Flip fan-out webull (rth_resting_mirror)",
    )
    f = WebullBrokerAdapter._status_reason

    # the venue gave no words -> say SO, never echo ours back
    r = f({}, req, "rejected", "6QHGQMTTQC5G2LGQI8UJ316V0B")
    assert "ATR Flip fan-out" not in r, "our intent text is being presented as the broker's reason"
    assert "no failure_reason" in r and "6QHGQMTTQC5G2LGQI8UJ316V0B" in r

    # the venue DID give words -> use them verbatim
    assert f({"failure_reason": "STOP_PRICE_MUST_BE_GREAT_THAN_MARKET_PRICE"}, req, "rejected", "x") \
        == "STOP_PRICE_MUST_BE_GREAT_THAN_MARKET_PRICE"
    assert f({"failureReason": "CAMEL_CASE_TOO"}, req, "rejected", "x") == "CAMEL_CASE_TOO"

    # a NON-refusal keeps our label — it is a description, not a verdict
    assert f({}, req, "filled", "x") == "schwab_1m_v2 ATR Flip fan-out webull (rth_resting_mirror)"


def test_the_status_poll_is_WIRED_to_the_helper_and_states_its_origin() -> None:
    """★ §181a — a test covering the HELPER but not the WIRING cannot see a dead call site.
    Both halves of the fix live at the poll's ExecutionReport, so both are pinned here."""
    src = (_ADAPTERS / "webull.py").read_text(encoding="utf-8")
    assert "reason = self._status_reason(item, request, event_type, broker_order_id)" in src, (
        "the status poll is no longer routed through _status_reason"
    )
    # ⛔ Strip comments before grepping for CODE. The first version of this assertion matched
    # my own docstring, which quotes the old expression verbatim — the third self-matching
    # guard of the day, after the truncation guard matched its own pattern line and then its
    # own success message. A guard that names what it hunts must exclude itself.
    code_lines = [ln for ln in src.split(chr(10)) if not ln.lstrip().startswith("#")]
    code = chr(10).join(code_lines)
    assert 'item.get("failureReason") or request.reason' not in code, (
        "the intent-text fallback is back on a status-poll reason"
    )
    # Q1/§195: the poll builds the venue's own report of its book -> `broker`, per protocols.py
    assert 'origin="broker",' in src, "the status-poll report stopped stating its origin"
