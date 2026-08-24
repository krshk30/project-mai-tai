"""§257 — the attach had been SUCCEEDING all along, and one kwarg recorded every success as a
refusal.

⛔⭐⭐ THIS IS A SEAM DEFECT, NOT A UNIT DEFECT. Both sides were tested. The adapter's payload
builder had `tests/unit/test_webull_attach_protection.py::test_the_pair_has_NO_master_leg` and
friends; the OMS's success branch had
`test_it_REMEMBERS_the_base_id_so_the_pair_can_later_be_RELEASED`. Each passed. **Each was fed a
fixture standing in for the other**, and the joint between them — *what the real adapter actually
returns on a successful placement* — was never once executed. `_submit_exit_pair_blocking` built
its `ExecutionReport` with `price=`, but the field is `fill_price`, so the constructor raised **on
the success path, after `place_order` had already returned a `combo_order_id`**.

The chain that produced "0 ATTACHED in 7 days":

  1. `place_order` succeeds — Webull creates the pair and returns `combo_order_id`.
  2. `ExecutionReport(price=None, ...)` raises `TypeError: unexpected keyword argument 'price'`.
  3. `submit_order`'s `except Exception` returns `self._reject(request, "TypeError(...)")` —
     ⛔ a NON-EMPTY list holding ONE report whose `event_type` is `"rejected"`.
  4. The OMS branch `if any(r.event_type not in ("rejected",) for r in reports)` is therefore
     False, so `[WEBULL-PROTECT-ATTACHED]` never logs AND
     `self._webull_protect_base[(account, symbol)] = coid` never runs — losing the ONLY handle
     that exists on broker-created legs. A pair we placed and can never release.
  5. Attempts 2-5 then place INTO that live pair and earn `ORDER_NOT_SUPPORT_REVERSE_OPTION`:
     we were fighting our own resting protection and reading it as the broker refusing us.

⛔ A WRONG REASON IS WORSE THAN A MISSING ONE. `THE POSITION IS HELD WITHOUT PROTECTION` was the
accepted story. The position was protected; our record of it was not.

⛔⭐ The lesson is the fixture rule: a test whose stand-in returns something production never
returns proves only that the stand-in is self-consistent. The assertion that closes this class is
the one below that runs the REAL constructor and feeds its output to the REAL predicate.
"""

from __future__ import annotations

import sys
import types
from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter


def _oms_success(reports) -> bool:
    """The OMS success predicate, transcribed from `oms/service.py`.

    If that line moves, this is the test that should have to move with it.
    """
    return any(getattr(r, "event_type", "") not in ("rejected",) for r in reports)


class _Resp:
    def __init__(self, body):
        self.body = body


def _reg_sdk(monkeypatch, op_cls):
    for pkg in ("webull", "webull.trade", "webull.trade.trade", "webull.trade.trade.v3"):
        monkeypatch.setitem(sys.modules, pkg, types.ModuleType(pkg))
    mod = types.ModuleType("webull.trade.trade.v3.order_opration_v3")
    mod.OrderOperationV3 = op_cls
    monkeypatch.setitem(sys.modules, "webull.trade.trade.v3.order_opration_v3", mod)


class _PlacingOp:
    """Webull ACCEPTS the pair and hands back a combo_order_id — the live 08-21 behaviour."""

    placed: list = []

    def __init__(self, _client):
        pass

    def place_order(self, account_id, legs, client_combo_order_id=None):
        _PlacingOp.placed.append((account_id, legs, client_combo_order_id))
        return _Resp({"combo_order_id": "WB-COMBO-777"})


def _adapter() -> WebullBrokerAdapter:
    import threading

    a = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    a.settings = None
    a.region_id = "us"
    a.host = "api.webull.com"
    a.app_key = "ak"
    a.app_secret = "as"
    a.accounts_by_name = {"live:orb": WebullAccountConfig(account_id="ACC1")}
    a._client = object()
    a._client_lock = threading.Lock()
    a._instrument_cache = {}
    a._instrument_lock = threading.Lock()
    return a


def _pair_request() -> OrderRequest:
    return OrderRequest(
        client_order_id="schwab_1m_v2-SUGP-protect-abc123def456",
        broker_account_name="live:orb",
        strategy_code="schwab_1m_v2",
        symbol="SUGP",
        side="sell",
        intent_type="close",
        quantity=Decimal("1"),
        reason="webull attach protection after bare resting fill",
        metadata={
            "webull_exit_only_pair": "true",
            "bracket_target_price": "3.0600",
            "bracket_stop_price": "2.8500",
            "source": "oms_v2_webull_protect",
            "market_session": "rth",
        },
        order_type="limit",
        time_in_force="day",
    )


# ------------------------------------------------------------------ half 1: the report CONSTRUCTS
@pytest.mark.asyncio
async def test_a_successful_placement_returns_an_ACCEPTED_report_not_a_TypeError(monkeypatch):
    """⛔⭐⭐ THE FIX. The success path must survive being taken.

    Before §257 this raised `TypeError: unexpected keyword argument 'price'` INSIDE the try, and
    `submit_order` converted the broker's success into our reject.
    """
    _PlacingOp.placed = []
    _reg_sdk(monkeypatch, _PlacingOp)

    reports = await _adapter().submit_order(_pair_request())

    assert len(reports) == 1
    rep = reports[0]
    assert rep.event_type == "accepted", (
        f"the broker created the pair; we recorded {rep.event_type!r} because of {rep.reason!r}"
    )
    assert "TypeError" not in (rep.reason or ""), (
        "a constructor error on the success path is indistinguishable, downstream, from the "
        "venue refusing us"
    )
    assert rep.broker_order_id == "WB-COMBO-777", "the combo id is the pair's identity"
    assert rep.fill_price is None, "an accepted pair has no fill yet"
    assert _PlacingOp.placed, "the placement must actually have happened"


def test_the_report_field_is_fill_price_and_price_is_NOT_a_field():
    """⛔ Pin the field name itself — this is the whole defect, one identifier wide."""
    assert "fill_price" in ExecutionReport.__dataclass_fields__
    assert "price" not in ExecutionReport.__dataclass_fields__, (
        "if `price` ever becomes a real field, the original bug stops raising and starts "
        "silently writing to the wrong column instead"
    )


# ------------------------------------------------------------------ half 2: the SEAM to the OMS
@pytest.mark.asyncio
async def test_the_REAL_report_satisfies_the_REAL_OMS_success_predicate(monkeypatch):
    """⛔⭐⭐ THE JOINT THAT BROKE. Neither unit test could see this, by construction.

    The OMS stores `_webull_protect_base[(account, symbol)] = coid` only inside
    `if any(r.event_type not in ("rejected",) ...)`. Run the REAL adapter and put its REAL output
    through that REAL predicate: this is the assertion whose absence let a rejected-shaped success
    pass two green test files for seven days.
    """
    _PlacingOp.placed = []
    _reg_sdk(monkeypatch, _PlacingOp)

    reports = await _adapter().submit_order(_pair_request())

    assert _oms_success(reports), (
        "the OMS success branch is what stores the base coid — the ONLY handle on legs the "
        "broker created and never lists. Failing this predicate is what made a placed pair "
        "unreleasable and blocked the software ladder from coexisting with it"
    )


@pytest.mark.asyncio
async def test_a_GENUINE_broker_refusal_still_reads_as_rejected(monkeypatch):
    """⛔ A FAILING CONTROL VOIDS THE PROBE.

    If everything now reads accepted, the test above proves nothing. A real transport failure must
    still come back rejected, and must still carry the broker's own words.
    """

    class _RefusingOp(_PlacingOp):
        def place_order(self, account_id, legs, client_combo_order_id=None):
            raise RuntimeError("STOP_LOSS_PRICE_LT_MARKETPRICE")

    _reg_sdk(monkeypatch, _RefusingOp)
    reports = await _adapter().submit_order(_pair_request())

    assert len(reports) == 1 and reports[0].event_type == "rejected"
    assert not _oms_success(reports), "a real refusal must NOT store a base coid"
    assert "STOP_LOSS_PRICE_LT_MARKETPRICE" in (reports[0].reason or ""), (
        "⛔ DO NOT TRUNCATE THE BROKER'S OWN WORDS — the CODE names the required relation"
    )
