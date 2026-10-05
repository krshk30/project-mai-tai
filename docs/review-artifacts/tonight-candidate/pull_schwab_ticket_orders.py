"""Four exact GETs; no token refresh, broker mutation, or unbounded history."""

import json
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.settings import Settings


def redact(value):
    if isinstance(value, dict):
        return {
            key: "REDACTED" if key.lower() in {
                "accountnumber", "accounthash", "accountid"
            } else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def main():
    adapter = SchwabBrokerAdapter(
        Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    )
    now = datetime.now(timezone.utc)
    if not adapter._access_token or (
        adapter._access_token_expires_at is not None
        and adapter._access_token_expires_at <= now
    ):
        raise SystemExit("UNKNOWN token unavailable/expired; no refresh attempted")
    account = adapter.accounts_by_name["live:schwab_1m_v2"]
    result = {"read_at": now.isoformat(), "orders": []}
    for order_id in (
        "1008165370341", "1008165371009", "1008168139729", "1008171127230"
    ):
        path = (
            f"/trader/v1/accounts/{quote(account.account_hash, safe='')}/orders/"
            f"{order_id}"
        )
        request = urllib.request.Request(
            adapter.base_url + path,
            headers={"Authorization": "Bearer " + adapter._access_token,
                     "Accept": "application/json"}, method="GET"
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise SystemExit("UNKNOWN order response exceeds 1 MiB cap")
            result["orders"].append({
                "request": f"GET /trader/v1/accounts/REDACTED/orders/{order_id}",
                "http_status": response.status, "response_bytes": len(raw),
                "body": redact(json.loads(raw)),
            })
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
