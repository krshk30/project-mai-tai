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

from project_mai_tai.broker_adapters.protocols import ExecutionReport
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


def test_webull_exception_paths_stay_UNKNOWN_rather_than_guess() -> None:
    """⛔⭐ The honest gap, pinned so nobody 'tidies' it into a guess.

    `_reject` is one helper behind six call sites. The pre-flight guards (no config, no instrument
    id) are unambiguously client — nothing was sent. The callers that wrap an exception are NOT
    classifiable from there: the exception may be a transport failure (client) or may wrap a
    Webull HTTP refusal (broker). They stay "unknown" until someone reads that path and can say.
    """
    src = (_ADAPTERS / "webull.py").read_text(encoding="utf-8")
    assert 'origin: str = "unknown"' in src, "the helper must default to unknown, not to a guess"
    assert src.count('origin="client"') == 2, "only the two pre-flight guards are classifiable"
    assert '_reject(request, self._exc_reason(exc), origin=' not in src, (
        "an exception-wrapping site was labelled by assumption"
    )
