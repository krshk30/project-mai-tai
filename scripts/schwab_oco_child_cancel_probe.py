#!/usr/bin/env python3
"""PROBE: will Schwab accept a DELETE against an OCO CHILD leg?

⛔ STANDALONE. Imports NOTHING from project_mai_tai and is imported by nothing.
   Not wired to v2, the OMS, or any production path. Running it changes no config.

THE ONE UNKNOWN THIS BUYS
-------------------------
`release_native_oco_for_close` harvests each working SELL child's own `orderId` from the broker
order tree and issues `DELETE /trader/v1/accounts/{hash}/orders/{orderId}` against each
(`schwab.py:222-262`). That loop has NEVER executed in production -- all 221 calls returned before
reaching it. Everything else in the 16:01 sequence is already proven: the harvest, the post-cancel
re-read, PM-limit acceptance before 16:05 (DAIC 08-25 submitted 16:02:14 -> filled 16:05:05;
CELU 08-27 submitted 16:04:56 -> filled 16:05:00) and 31 filled session=PM orders overall.

⛔ SAFETY PROPERTIES -- each is load-bearing, do not "improve" any of them away
  1. QTY is hard-capped at 1 share. Not a parameter.
  2. EVERY step is a separate subcommand. Nothing chains. The operator runs each one and looks.
  3. NO LOOP, NO RETRY ANYWHERE. Every HTTP call is made exactly once. A failed call ends the
     process. (This is the 220-rejects-in-14-minutes hole; it does not get to reappear in a test.)
  4. Writes require --i-have-go on the command line. Reads never do.
  5. `cancel` STOPS on the first non-2xx and refuses to touch the second leg.
  6. `exit-pm` RE-VERIFIES the legs are gone itself and refuses to place if any leg still works --
     so a mistaken invocation cannot sell shares an exit leg still reserves.
  7. Any unexpected shape -- wrong qty, unexpected child count, unrecognised status -- aborts.
  8. Raw broker status code + body are printed for every call. No summarising.

RUN ON THE VPS as a user that can read the token store.

  python3 schwab_oco_child_cancel_probe.py preflight  --symbol F
  python3 schwab_oco_child_cancel_probe.py place      --symbol F --i-have-go   # RTH, near the bell
  python3 schwab_oco_child_cancel_probe.py status     --entry <entryOrderId>
  python3 schwab_oco_child_cancel_probe.py cancel     --entry <entryOrderId> --i-have-go  # 16:01 ET
  python3 schwab_oco_child_cancel_probe.py verify     --entry <entryOrderId>
  python3 schwab_oco_child_cancel_probe.py exit-pm    --symbol F --limit <px> --i-have-go
  python3 schwab_oco_child_cancel_probe.py order      --order <exitOrderId>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

TOKEN_STORE = "/var/lib/macd-webhook-server/data/schwab_tokens.json"
BASE_URL = "https://api.schwabapi.com"
ET = ZoneInfo("America/New_York")

QTY = 1  # ⛔ hard-capped. Buying an answer, not a position.

# Exit legs deliberately FAR from market so neither triggers before 16:01 and there is
# something left to cancel. ⚠ The consequence, stated rather than hidden: for the life of
# the probe the share is effectively unprotected. It is one share of a cheap name, and the
# exposure window is whatever gap you leave between `place` and `cancel` -- keep it short.
TARGET_MULT = 1.60
PROTECT_MULT = 0.40

ACCEPTED = {"WORKING", "QUEUED", "ACCEPTED", "PENDING_ACTIVATION", "AWAITING_PARENT_ORDER"}
GONE = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "REPLACED"}


def now_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " ET"


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"\n⛔ ABORT: {msg}", flush=True)
    sys.exit(1)


def token() -> str:
    data = json.loads(Path(TOKEN_STORE).read_text())
    tok = str(data.get("access_token") or "")
    if not tok:
        die(f"no access_token in {TOKEN_STORE}")
    print(f"  token store: updated_at={data.get('updated_at')} expires_at={data.get('expires_at')}")
    return tok


def call(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    """ONE HTTP call. No retry, ever. Returns (status_code, raw_body_text)."""
    url = BASE_URL + path
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", f"Bearer {token()}")
    req.add_header("Accept", "application/json")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    print(f"  --> {method} {path}  @ {now_et()}")
    if body is not None:
        print(f"      request body: {json.dumps(body)}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.getcode()
            loc = r.headers.get("Location", "")
            if loc:
                raw = raw + f"\n      [Location: {loc}]"
    except urllib.error.HTTPError as exc:
        raw = (exc.read().decode("utf-8", "replace") if exc.fp else "")
        code = exc.code
    except Exception as exc:  # noqa: BLE001
        die(f"transport failure, NOT retrying: {exc!r}")
    print(f"  <== HTTP {code}  @ {now_et()}")
    print(f"      RAW BODY: {raw if raw.strip() else '(empty)'}")
    return code, raw


def account_hash() -> str:
    code, raw = call("GET", "/trader/v1/accounts/accountNumbers")
    if code != 200:
        die(f"accountNumbers returned HTTP {code}")
    rows = json.loads(raw)
    if not isinstance(rows, list) or len(rows) != 1:
        die(f"expected exactly ONE account, got {len(rows) if isinstance(rows, list) else rows!r} "
            "-- refusing to guess which account is the real-money one")
    h = str(rows[0].get("hashValue") or "")
    if not h:
        die("no hashValue")
    print(f"  account {rows[0].get('accountNumber')} -> hash ok")
    return h


def quote_px(symbol: str) -> tuple[float, float]:
    code, raw = call("GET", f"/marketdata/v1/quotes?symbols={quote(symbol, safe='')}")
    if code != 200:
        die(f"quote returned HTTP {code}")
    q = json.loads(raw).get(symbol, {}).get("quote", {})
    bid, ask = float(q.get("bidPrice") or 0), float(q.get("askPrice") or 0)
    if bid <= 0 or ask <= 0:
        die(f"no two-sided market for {symbol}: bid={bid} ask={ask}")
    print(f"  {symbol}: bid={bid} ask={ask}")
    return bid, ask


def sell_children(tree: dict) -> list[dict]:
    """Every SELL leg in the tree, with its own orderId and status."""
    out: list[dict] = []

    def walk(node: dict) -> None:
        legs = node.get("orderLegCollection") or []
        leg = legs[0] if legs else {}
        if str(leg.get("instruction") or "").upper() == "SELL":
            out.append({
                "orderId": str(node.get("orderId") or ""),
                "status": str(node.get("status") or "").upper(),
                "orderType": str(node.get("orderType") or ""),
                "qty": node.get("quantity"),
            })
        for child in node.get("childOrderStrategies") or []:
            walk(child)

    walk(tree)
    return out


# ----------------------------------------------------------------- subcommands

def cmd_preflight(a) -> None:
    print(f"\n=== PREFLIGHT (read-only, places nothing) — {now_et()} ===")
    h = account_hash()
    bid, ask = quote_px(a.symbol)
    code, raw = call("GET", f"/trader/v1/accounts/{quote(h, safe='')}?fields=positions")
    if code != 200:
        die(f"positions read HTTP {code}")
    positions = (json.loads(raw).get("securitiesAccount") or {}).get("positions") or []
    held = [p for p in positions
            if str((p.get("instrument") or {}).get("symbol") or "").upper() == a.symbol.upper()]
    if held:
        die(f"{a.symbol} is ALREADY HELD on this account — pick a symbol we do not hold, "
            "so the probe's own share is unambiguous")
    print(f"\n  entry would be: LIMIT BUY {QTY} {a.symbol} @ {round(ask * 1.01, 2)} (ask +1%, marketable)")
    print(f"  target leg    : LIMIT SELL @ {round(ask * TARGET_MULT, 2)}   (far — must not trigger)")
    print(f"  protect leg   : STOP  SELL @ {round(ask * PROTECT_MULT, 2)}  (far — must not trigger)")
    print(f"  cost of the probe: ~${round(ask * QTY, 2)}")
    print("\n  ✅ preflight clean. Nothing was placed.")


def cmd_place(a) -> None:
    if not a.i_have_go:
        die("place is a WRITE. Re-run with --i-have-go when the operator is watching.")
    et = datetime.now(ET)
    # ⛔ RTH only, stated as one comparison. An earlier version of this guard had a hole at
    # 09:00-09:29 (pre-market) where it warned but did not refuse — a marketable LIMIT placed
    # then does not fill, and the probe would sit holding an unfilled entry with no children.
    minutes = et.hour * 60 + et.minute
    if et.weekday() >= 5 or not (9 * 60 + 30 <= minutes < 16 * 60):
        die(f"it is {et.strftime('%a %H:%M')} ET — outside RTH (Mon-Fri 09:30-16:00). "
            "A marketable entry will not fill. Refusing to place.")
    if minutes < 15 * 60 + 30:
        print(f"  ⚠ it is {et.strftime('%H:%M')} ET — the share will sit with FAR exit legs until "
              "you cancel at 16:01. Placing nearer the bell shortens that exposure.")
    print(f"\n=== PLACE 1-SHARE BRACKET — {now_et()} ===")
    h = account_hash()
    _bid, ask = quote_px(a.symbol)
    payload = {
        "session": "NORMAL", "duration": "DAY",
        "orderType": "LIMIT", "price": round(ask * 1.01, 2),
        "orderStrategyType": "TRIGGER",
        "orderLegCollection": [{"instruction": "BUY", "quantity": QTY,
                                "instrument": {"symbol": a.symbol, "assetType": "EQUITY"}}],
        "childOrderStrategies": [{
            "orderStrategyType": "OCO",
            "childOrderStrategies": [
                {"session": "NORMAL", "duration": "DAY", "orderType": "LIMIT",
                 "price": round(ask * TARGET_MULT, 2), "orderStrategyType": "SINGLE",
                 "orderLegCollection": [{"instruction": "SELL", "quantity": QTY,
                                         "instrument": {"symbol": a.symbol, "assetType": "EQUITY"}}]},
                {"session": "NORMAL", "duration": "DAY", "orderType": "STOP",
                 "stopPrice": round(ask * PROTECT_MULT, 2), "orderStrategyType": "SINGLE",
                 "orderLegCollection": [{"instruction": "SELL", "quantity": QTY,
                                         "instrument": {"symbol": a.symbol, "assetType": "EQUITY"}}]},
            ],
        }],
    }
    code, _raw = call("POST", f"/trader/v1/accounts/{quote(h, safe='')}/orders", payload)
    if code not in (200, 201):
        die(f"placement refused HTTP {code} — nothing to clean up, stop here.")
    print("\n  ✅ accepted. Read the Location header above for the entry orderId, then run:")
    print("     status --entry <entryOrderId>")


def cmd_status(a) -> None:
    print(f"\n=== STATUS — {now_et()} ===")
    h = account_hash()
    code, raw = call("GET", f"/trader/v1/accounts/{quote(h, safe='')}/orders/{quote(a.entry, safe='')}")
    if code != 200:
        die(f"order read HTTP {code}")
    tree = json.loads(raw)
    print(f"\n  parent status={tree.get('status')} filledQty={tree.get('filledQuantity')}")
    kids = sell_children(tree)
    for k in kids:
        print(f"  SELL child orderId={k['orderId']} status={k['status']} type={k['orderType']} qty={k['qty']}")
    if float(tree.get("filledQuantity") or 0) != QTY:
        print(f"  ⚠ entry not (yet) filled for exactly {QTY} share — do NOT cancel until it is.")
    if len(kids) != 2:
        print(f"  ⚠ expected 2 SELL children, saw {len(kids)} — cancel would abort on this.")


def cmd_cancel(a) -> None:
    if not a.i_have_go:
        die("cancel is a WRITE. Re-run with --i-have-go when the operator is watching.")
    print(f"\n=== CANCEL OCO CHILDREN — THE UNKNOWN — {now_et()} ===")
    h = account_hash()
    code, raw = call("GET", f"/trader/v1/accounts/{quote(h, safe='')}/orders/{quote(a.entry, safe='')}")
    if code != 200:
        die(f"pre-cancel read HTTP {code}")
    tree = json.loads(raw)
    if float(tree.get("filledQuantity") or 0) != QTY:
        die(f"entry filledQuantity={tree.get('filledQuantity')}, expected {QTY} — refusing to cancel")
    kids = sell_children(tree)
    working = [k for k in kids if k["status"] in ACCEPTED]
    unknown = [k for k in kids if k["status"] not in ACCEPTED | GONE | {"FILLED"}]
    if unknown:
        die(f"unrecognised child status {unknown} — refusing to proceed on a guess")
    already = [k for k in kids if k["status"] == "FILLED"]
    if already:
        die(f"a SELL child has already FILLED {already} — the position is closed. There is nothing "
            "to cancel and nothing to exit. Stop and check the account.")
    if len(working) != 2:
        die(f"expected exactly 2 working SELL children, saw {len(working)}: {working}. "
            "Refusing — an unexpected leg count is exactly the case to stop on.")
    print(f"\n  two working children: {[k['orderId'] for k in working]}")
    t0 = time.monotonic()
    results = []
    for i, k in enumerate(working, 1):
        print(f"\n  --- DELETE child {i}/2, orderId={k['orderId']} ---")
        code, raw = call("DELETE",
                         f"/trader/v1/accounts/{quote(h, safe='')}/orders/{quote(k['orderId'], safe='')}")
        results.append((k["orderId"], code, raw))
        if not (200 <= code < 300):
            print(f"\n⛔⛔ DELETE REFUSED on child {i}: HTTP {code}")
            print(f"    VERBATIM BODY: {raw}")
            print("\n⛔ STOPPING. The second leg was NOT touched. No PM order will be placed.")
            print("⛔ THE LEGS ARE STILL WORKING — tell the operator NOW so he can close by hand.")
            sys.exit(2)
        print(f"  ✅ child {i} DELETE accepted: HTTP {code}")
        if i == 1:
            print("  (next: does leg 2 still exist? `verify` answers whether ONE call took both.)")
    print(f"\n  both DELETEs accepted in {time.monotonic() - t0:.2f}s. Run `verify --entry {a.entry}`.")
    for oid, code, raw in results:
        print(f"    {oid}: HTTP {code} body={raw.strip() or '(empty)'}")


def cmd_verify(a) -> None:
    print(f"\n=== VERIFY GONE — {now_et()} ===")
    h = account_hash()
    code, raw = call("GET", f"/trader/v1/accounts/{quote(h, safe='')}/orders/{quote(a.entry, safe='')}")
    if code != 200:
        die(f"re-read HTTP {code}")
    kids = sell_children(json.loads(raw))
    for k in kids:
        print(f"  SELL child orderId={k['orderId']} status={k['status']}")
    still = [k for k in kids if k["status"] in ACCEPTED]
    if still:
        print(f"\n  ⚠ STILL WORKING: {still} — the shares remain reserved. Do NOT run exit-pm.")
        sys.exit(2)
    print("\n  ✅ no working SELL legs remain.")


def cmd_exit_pm(a) -> None:
    if not a.i_have_go:
        die("exit-pm is a WRITE. Re-run with --i-have-go when the operator is watching.")
    print(f"\n=== PM LIMIT EXIT — {now_et()} ===")
    h = account_hash()
    # ⛔ Re-verify independently: never sell shares a working leg still reserves.
    code, raw = call("GET", f"/trader/v1/accounts/{quote(h, safe='')}/orders/{quote(a.entry, safe='')}")
    if code != 200:
        die(f"pre-exit re-read HTTP {code}")
    kids = sell_children(json.loads(raw))
    still = [k for k in kids if k["status"] in ACCEPTED]
    if still:
        die(f"a SELL leg is STILL WORKING {still} — refusing to place a second sell against "
            "reserved shares. This guard is the whole point.")
    # ⛔⭐⭐ "NO WORKING LEGS" HAS TWO CAUSES AND ONLY ONE IS SAFE. Either the legs lapsed/cancelled
    # (we still hold the share) or a leg FILLED (the share is already SOLD). They look identical
    # to the working-leg check above. Selling on the second is a naked short on a real account.
    filled = [k for k in kids if k["status"] == "FILLED"]
    if filled:
        die(f"a SELL child has already FILLED {filled} — the share is SOLD. Refusing to place "
            "another sell; that would be a naked short. Check the position before doing anything.")
    # ⛔ Read the book before pricing a REAL sell. Read-only; it only refuses an obviously
    # wrong limit. A fat-fingered --limit on a live sell is the cheap mistake to make impossible.
    bid, ask = quote_px(a.symbol)
    if float(a.limit) > bid:
        print(f"  ⚠ limit {a.limit} is ABOVE the bid {bid} — it will rest, not fill.")
    if not (bid * 0.5 <= float(a.limit) <= ask * 1.5):
        die(f"limit {a.limit} is wildly off the book (bid={bid} ask={ask}) — refusing. "
            "Re-check the price you meant.")
    payload = {
        "session": "PM", "duration": "DAY", "orderType": "LIMIT", "price": float(a.limit),
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{"instruction": "SELL", "quantity": QTY,
                                "instrument": {"symbol": a.symbol, "assetType": "EQUITY"}}],
    }
    code, _raw = call("POST", f"/trader/v1/accounts/{quote(h, safe='')}/orders", payload)
    if code not in (200, 201):
        die(f"PM exit REFUSED HTTP {code} — the share is still held. Tell the operator.")
    print("\n  ✅ PM exit accepted. Note the time above (before/after 16:05 matters).")
    print("     Follow with: order --order <exitOrderId>")


def cmd_order(a) -> None:
    print(f"\n=== ORDER READ — {now_et()} ===")
    h = account_hash()
    code, raw = call("GET", f"/trader/v1/accounts/{quote(h, safe='')}/orders/{quote(a.order, safe='')}")
    if code != 200:
        die(f"order read HTTP {code}")
    o = json.loads(raw)
    print(f"\n  status={o.get('status')} filledQuantity={o.get('filledQuantity')} "
          f"enteredTime={o.get('enteredTime')} closeTime={o.get('closeTime')}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, needs in (
        ("preflight", cmd_preflight, ("symbol",)),
        ("place", cmd_place, ("symbol", "go")),
        ("status", cmd_status, ("entry",)),
        ("cancel", cmd_cancel, ("entry", "go")),
        ("verify", cmd_verify, ("entry",)),
        ("exit-pm", cmd_exit_pm, ("symbol", "limit", "go")),
        ("order", cmd_order, ("order",)),
    ):
        s = sub.add_parser(name)
        if "symbol" in needs:
            s.add_argument("--symbol", required=True)
        if "entry" in needs:
            s.add_argument("--entry", required=True, help="entry (parent) broker orderId")
        if "order" in needs:
            s.add_argument("--order", required=True)
        if "limit" in needs:
            s.add_argument("--limit", required=True, type=float)
        if "go" in needs:
            s.add_argument("--i-have-go", action="store_true")
        s.set_defaults(func=fn)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
