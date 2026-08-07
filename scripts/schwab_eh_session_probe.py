"""PROBE P -- does Schwab accept a native OCO / STOP leg in the EXTENDED-HOURS session?

⛔ PREVIEW ONLY. PLACES NOTHING. The only endpoint referenced in this file is /previewOrder.
There is no POST to /orders anywhere by construction, so a typo cannot place an order.
Mirrors scripts/schwab_oco_preview.py (the 2026-07-21 STEP-1 shape validation).

WHY A MATRIX AND NOT ONE ORDER. A bare reject is not an answer -- it could mean "STOP not
allowed in this session", "not shortable", "bad tick", or "harness broken". Every EH case here
is paired with the SAME shape in session=NORMAL as a control. If a control rejects, the harness
is wrong and no conclusion may be drawn from that row.

All prices are far from market so nothing is marketable even in principle.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

TOKEN_STORE = "/var/lib/macd-webhook-server/data/schwab_tokens.json"
BASE_URL = "https://api.schwabapi.com"
SYMBOL = "AAPL"

BUY_STOP = 900.00      # far ABOVE market -- never triggers
SELL_STOP = 50.00      # far BELOW market -- never triggers
TARGET_LIMIT = 918.00
PROTECT_STOP = 855.00


def access_token() -> str:
    data = json.loads(Path(TOKEN_STORE).read_text())
    tok = data.get("access_token")
    if not tok:
        sys.exit("no access_token in token store")
    print("token store updated_at=%s expires_at=%s\n" % (data.get("updated_at"), data.get("expires_at")))
    return tok


def account_hash(token: str) -> str:
    code, body = call(token, "GET", "/trader/v1/accounts/accountNumbers")
    if code != 200 or not body:
        sys.exit("cannot read account numbers: %s %s" % (code, body))
    return body[0]["hashValue"]


def call(token: str, method: str, path: str, body: dict | None = None):
    headers = {"Authorization": "Bearer %s" % token, "Accept": "application/json"}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode()
    req = urllib.request.Request(BASE_URL + path, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw
    except Exception as exc:  # never swallow -- a dead harness must not read as a clean reject
        return -1, "HARNESS-ERROR %s: %s" % (type(exc).__name__, exc)


def leg(instruction: str, order_type: str, price: float, session: str) -> dict:
    d: dict = {
        "session": session,
        "duration": "DAY",
        "orderType": order_type,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": instruction,
            "quantity": 1,
            "instrument": {"symbol": SYMBOL, "assetType": "EQUITY"},
        }],
    }
    if order_type == "LIMIT":
        d["price"] = round(price, 2)
    else:
        d["stopPrice"] = round(price, 2)
    return d


def bracket(session: str) -> dict:
    """TRIGGER(buy stop) -> OCO[target LIMIT, protective STOP] -- today's entry-bracket shape."""
    p = leg("BUY", "STOP", BUY_STOP, session)
    p["orderStrategyType"] = "TRIGGER"
    p["childOrderStrategies"] = [{
        "orderStrategyType": "OCO",
        "childOrderStrategies": [
            leg("SELL", "LIMIT", TARGET_LIMIT, session),
            leg("SELL", "STOP", PROTECT_STOP, session),
        ],
    }]
    return p


def exit_only_oco(session: str) -> dict:
    """Bare OCO exit pair against an existing position -- the shape #646 Part 1 would need."""
    return {
        "orderStrategyType": "OCO",
        "childOrderStrategies": [
            leg("SELL", "LIMIT", TARGET_LIMIT, session),
            leg("SELL", "STOP", PROTECT_STOP, session),
        ],
    }


CASES = [
    ("C1 control  single BUY STOP        NORMAL", lambda: leg("BUY", "STOP", BUY_STOP, "NORMAL")),
    ("P1 QUESTION single BUY STOP        AM    ", lambda: leg("BUY", "STOP", BUY_STOP, "AM")),
    ("C2 control  single SELL LIMIT      AM    ", lambda: leg("SELL", "LIMIT", TARGET_LIMIT, "AM")),
    ("P2 QUESTION single SELL STOP       AM    ", lambda: leg("SELL", "STOP", SELL_STOP, "AM")),
    ("C3 control  TRIGGER->OCO bracket   NORMAL", lambda: bracket("NORMAL")),
    ("P3 QUESTION TRIGGER->OCO bracket   AM    ", lambda: bracket("AM")),
    ("C4 control  exit-only OCO pair     NORMAL", lambda: exit_only_oco("NORMAL")),
    ("P4 QUESTION exit-only OCO pair     AM    ", lambda: exit_only_oco("AM")),
]


def summarize(code, body) -> str:
    if code == -1:
        return "HARNESS-ERROR -- no conclusion may be drawn: %s" % body
    if isinstance(body, dict):
        val = body.get("orderValidationResult") or {}
        rejects = val.get("rejects") or []
        warns = val.get("warns") or []
        if rejects:
            msgs = "; ".join(str(r.get("message", r)) for r in rejects)
            return "REJECT(%s): %s" % (code, msgs[:300])
        if code in (200, 201):
            note = ""
            if warns:
                note = "  [warns: %s]" % "; ".join(str(w.get("message", w)) for w in warns)[:160]
            return "ACCEPTED(%s)%s" % (code, note)
        msg = body.get("message") or body.get("error") or json.dumps(body)[:300]
        return "HTTP %s: %s" % (code, str(msg)[:300])
    return "HTTP %s: %s" % (code, str(body)[:300])


def main() -> int:
    print("=" * 78)
    print("PROBE P -- Schwab EH session acceptance.  PREVIEW ONLY, PLACES NOTHING.")
    print("=" * 78)
    tok = access_token()
    acct = account_hash(tok)
    print("account hash resolved (…%s)\n" % acct[-6:])
    path = "/trader/v1/accounts/%s/previewOrder" % quote(acct, safe="")

    results = {}
    for label, build in CASES:
        code, body = call(tok, "POST", path, build())
        line = summarize(code, body)
        results[label.split()[0]] = line
        print("%s  ->  %s" % (label, line))
        if code == -1 or (isinstance(body, dict) and body.get("orderValidationResult", {}).get("rejects")):
            raw = json.dumps(body)[:700] if isinstance(body, dict) else str(body)[:700]
            print("      raw: %s" % raw)
    print()
    print("-" * 78)
    print("READ IT LIKE THIS")
    print("  Any control (C1-C4) that is not ACCEPTED => the harness is wrong for that shape;")
    print("  draw NO conclusion from its paired question row.")
    print("  P1/P2 answer 'is a STOP leg legal in the AM session at all'.")
    print("  P3/P4 answer 'is the OCO wrapper legal in AM' -- P4 is the shape #646 Part 1 needs.")
    print("-" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
