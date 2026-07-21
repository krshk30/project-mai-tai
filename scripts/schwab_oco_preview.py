"""Schwab OCO/TRIGGER combo shape probe — PREVIEW ONLY, places NOTHING.

Mirrors scripts/webull_otoco_preview.py (Webull OTOCO validation, 2026-07-20).
The ONLY write endpoint referenced in this file is `/previewOrder`. There is no
POST to `/orders` anywhere here by construction, so a typo cannot place an order.

Answers the STEP-1 gate item 0 for Schwab:
  does this account accept `orderStrategyType=TRIGGER` with a nested OCO child pair?

Run on the VPS as trader with the service env loaded.
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

# Far-from-market so nothing here is remotely marketable even in principle.
SYMBOL = "AAPL"
ENTRY_STOP = 900.00   # buy-stop trigger far ABOVE market
TARGET_LIMIT = 918.00  # +2% off the entry
PROTECT_STOP = 855.00  # -5% off the entry


def access_token() -> str:
    data = json.loads(Path(TOKEN_STORE).read_text())
    tok = data.get("access_token")
    if not tok:
        sys.exit("no access_token in token store")
    print(f"token store updated_at={data.get('updated_at')} expires_at={data.get('expires_at')}")
    return tok


def call(token: str, method: str, path: str, body: dict | None = None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode()
    req = urllib.request.Request(BASE_URL + path, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode() or "{}"
            return resp.getcode(), json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = (exc.read().decode() if exc.fp else "") or "{}"
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw[:600]}
    except Exception as exc:
        return 599, {"error": str(exc)}


def leg(instruction: str, qty: int = 1) -> dict:
    return {
        "instruction": instruction,
        "quantity": qty,
        "instrument": {"symbol": SYMBOL, "assetType": "EQUITY"},
    }


def single_control() -> dict:
    """Control: the shape the adapter builds today. Proves preview works at all."""
    return {
        "session": "NORMAL",
        "duration": "DAY",
        "orderType": "STOP",
        "stopPrice": ENTRY_STOP,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [leg("BUY")],
    }


def oco_pair() -> dict:
    """Bare OCO exit pair (assumes shares held) — the E5 core."""
    return {
        "orderStrategyType": "OCO",
        "childOrderStrategies": [
            {
                "session": "NORMAL",
                "duration": "DAY",
                "orderType": "LIMIT",
                "price": TARGET_LIMIT,
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [leg("SELL")],
            },
            {
                "session": "NORMAL",
                "duration": "DAY",
                "orderType": "STOP",
                "stopPrice": PROTECT_STOP,
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [leg("SELL")],
            },
        ],
    }


def trigger_bracket() -> dict:
    """Full bracket: entry TRIGGERs the OCO exit pair (the operator's ticket)."""
    return {
        "session": "NORMAL",
        "duration": "DAY",
        "orderType": "STOP",
        "stopPrice": ENTRY_STOP,
        "orderStrategyType": "TRIGGER",
        "orderLegCollection": [leg("BUY")],
        "childOrderStrategies": [oco_pair()],
    }


def main() -> None:
    token = access_token()

    code, accounts = call(token, "GET", "/trader/v1/accounts/accountNumbers")
    print(f"\naccountNumbers -> HTTP {code}")
    if code != 200 or not isinstance(accounts, list) or not accounts:
        print(json.dumps(accounts, indent=2)[:800])
        sys.exit("cannot resolve account hash")
    hashes = [a.get("hashValue") for a in accounts if a.get("hashValue")]
    print(f"  {len(hashes)} account(s); using the first")
    acct = quote(hashes[0], safe="")

    for name, payload in (
        ("SINGLE (control)", single_control()),
        ("OCO exit pair", oco_pair()),
        ("TRIGGER + OCO bracket", trigger_bracket()),
    ):
        code, resp = call(token, "POST", f"/trader/v1/accounts/{acct}/previewOrder", payload)
        verdict = "ACCEPTED" if code in (200, 201) else "REJECTED"
        print(f"\n=== {name} -> HTTP {code} [{verdict}] ===")
        text = json.dumps(resp, indent=2)
        print(text[:1200])


if __name__ == "__main__":
    main()
