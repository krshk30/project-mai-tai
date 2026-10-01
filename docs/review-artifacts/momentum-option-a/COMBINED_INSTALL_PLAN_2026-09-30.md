# 2026-10-01 Mode A-only gateway install plan

Status: **REVIEW REQUIRED BEFORE THE 16:05 ET EXECUTION.** The operator's
2026-10-01 GO by paste names the gateway phase at box SHA
`01a64e9b7552b673e7db6f6e6c787b77b16f6f22`. The later `origin/main`
may differ only under `docs/`; a code/config diff or dirty box refuses the
install. This refresh changes the reviewed combined plan at `778565cd` into
the remaining Mode A gateway phase only. Mode B's OMS, strategy, ORB Schwab,
FLAGGATE and restart-gate installs already happened on 2026-09-30; do not
repeat them. The old Momentum-paper service remains STOPPED tonight. The
operator's GO authorizes one gateway restart at or after 16:05 ET today, one
rollback if a mandatory content proof is unproven **without Redis eviction
or stream loss**. An eviction/stream loss is UNKNOWN: stop, page, and do not
run a rollback loop on top of it. The GO also covers a guard start after
the 20:00:06 ET log rotation, and a conditional paper start at 05:15 ET on
2026-10-02. Independent review of this refreshed plan must finish first.
No restart before 16:05 ET, OMS/strategy/v2/ORB/orb-schwab restart, env/flag
change, WBREAD1, watch install, gate edit, Momentum replay, or protocol
threshold change is authorized here. The old gateway's final restart must
finish before the 20:00 ET rotation. The separate untimestamped-traceback
checker defect is not a reason to alter this live gateway phase.
The earlier `b01b93b1` proof is unsafe and the 16:05 automation is PAUSED;
do not resume it on that head. This revision itself is not an execution
authorization until independently reviewed.

Read-only live dry run at 09:21-09:25 ET on the old gateway (no restart):
`evicted_keys=18` before/after; Redis memory 1.186 GB before and 1.179 GB
after. Snapshot `XRANGE count=1` returned 5,512,965 bytes and its next
single-batch read 5,513,135 bytes (6,871 ms stream-ID interval). A 100-event
market-data page returned 31,946 bytes; 25 heartbeats 15,758 bytes; 17
retained subscription events 4,690 bytes; one strategy-state event 3,027
bytes. The old gateway's owner hash was empty, as expected before Option A.
The strict flat helper first found NXL bot exposure on both accounts (rc=1),
then found both bot books clear on a later fresh read (rc=0). Neither read
authorizes a later restart: flatness must be proved again immediately before
it. At 09:43 ET the full forward proof was dry-run read-only for 180 seconds
against the **old** gateway with explicitly synthetic timestamps and no
restart: 21,675 market events scanned; both required symbols NXL/VEEA had
post-bound ticks; 24 snapshot intervals from stream IDs, p95 7.989 s,
maximum 11.392 s; latest heartbeat healthy with active=2/expected=2.
The old gateway has no migrated owner hash, so content (4) was NOT_PROVEN
and the synthetic run correctly exited nonzero. No rollback was executed.
Redis `evicted_keys` remained 18, all eight preexisting `mai_tai` streams
remained present, and used memory after the run was 1,181,659,008 bytes.
This is a bounded-read safety test, **not** post-restart acceptance proof.
Rollback replay writes were not dry-run on the live system; its distinct
read patterns (single exact subscription event, heartbeat page, 100-tick
page and one snapshot per call) were probed read-only.

## 1. Read-only preflight and hard stops

1. Confirm the exact SHA and reviewed PR heads/pins; verify the box checkout is
   clean, no other install is in progress, all current service identities and
   `NRestarts` are recorded, and Redis, gateway, v2, scanner/strategy, OMS and
   broker connections are healthy. Confirm the installed watch and both cron
   SHA guards remain untouched. A Git merge is not an install.
2. Obtain fresh direct broker reads and prove **bot books flat** on both live
   accounts: zero open managed rows, zero nonzero virtual positions, and zero
   net bot fills today per account/symbol. A broker holding is nonblocking
   only when these bot-book checks are all proven clear. Print its symbol and
   quantity as `MANUAL_HOLDING_RECORDED_NOT_BLOCKING`; never call the broker
   account flat. Record the published
   armed-segment set, but do not block on a nonzero set: v2's 15:45 ET entry
   window has ended, v2 is not restarted, and its arm state is untouched.
   Repeat broker and bot-book checks immediately before the gateway restart,
   any rollback restart, and the next-day paper start. At the paper start,
   record the flat read but do not block on it: paper restarts no trading
   service. Record time, source,
   account and denominator for every read. A database zero alone is not
   broker flatness. Open bot books or unproven broker holdings at 16:05 wait
   five minutes for a fresh read, through the 19:15 ET restart cutoff.
   Unreadable/stale/ambiguous is UNKNOWN and also waits five minutes for a
   fresh read; restart only after a proven-clear read. Rollback pre-checks
   still refuse on UNKNOWN. The deployed Webull
   `list_account_positions` can return `[]` on a non-429 error and its
   `_positions_blocking` can stop on a bad or partial page; neither is a
   valid direct-flat proof. Use the strict read below: every holding must be
   legible; malformed, unreadable, or incomplete pages are UNKNOWN.
   Exit 0 proves bot books clear (with any manual holdings printed); exit 1
   means bot exposure; exit 2 means UNKNOWN. Create the read-only helper once
   with O_EXCL and journal its hash before invoking it:

   ```bash
   REPO=/home/trader/project-mai-tai
   FLAT_CHECK=/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py
   sudo bash -c 'set -C; cat > "$1"' bash "$FLAT_CHECK" <<'PY'
   import asyncio
   import sys
   from datetime import datetime, time, timedelta, timezone
   from decimal import Decimal
   from urllib.parse import quote
   from zoneinfo import ZoneInfo
   from sqlalchemy import case, func, select
   from sqlalchemy.orm import Session
   from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
   from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
   from project_mai_tai.db.models import BrokerAccount, Fill, OmsManagedPosition, VirtualPosition
   from project_mai_tai.db.session import build_engine
   from project_mai_tai.settings import Settings
   from webull.trade.request.get_account_positions_request import AccountPositionsRequest

   def webull_rows(adapter):
       account = adapter.accounts_by_name.get("live:orb")
       if account is None:
           raise ValueError("live:orb Webull account mapping absent")
       client = adapter._get_client()
       cursor = None
       seen = set()
       rows = []
       for page in range(1, 21):
           request = AccountPositionsRequest()
           request.set_account_id(account.account_id)
           if hasattr(request, "set_page_size"):
               request.set_page_size(50)
           if cursor:
               if not hasattr(request, "set_last_instrument_id"):
                   raise ValueError("Webull pagination setter absent")
               request.set_last_instrument_id(cursor)
           response = client.get_response(request)
           status = getattr(response, "status_code", 200)
           if not 200 <= int(status) < 300:
               raise ValueError(f"Webull page {page} HTTP {status}")
           body = adapter._body(response)
           if not isinstance(body, dict):
               raise ValueError(f"Webull page {page} malformed")
           holdings = body.get("holdings", body.get("positions"))
           if not isinstance(holdings, list) or any(not isinstance(row, dict) for row in holdings):
               raise ValueError(f"Webull page {page} holdings missing/malformed")
           for holding in holdings:
               instrument = holding.get("instrument")
               symbol = (holding.get("symbol") or holding.get("ticker")
                         or (instrument.get("symbol") if isinstance(instrument, dict) else None))
               raw_quantity = next((holding[key] for key in ("quantity", "qty", "position", "shares")
                                    if key in holding), None)
               if not isinstance(symbol, str) or not symbol or raw_quantity is None:
                   raise ValueError(f"Webull page {page} holding symbol/quantity unreadable")
               quantity = Decimal(str(raw_quantity))
               if not quantity.is_finite():
                   raise ValueError(f"Webull page {page} holding quantity nonfinite")
               if quantity:
                   rows.append(("live:orb", symbol.upper(), quantity))
           has_next = body.get("has_next", body.get("hasNext", False))
           if not isinstance(has_next, bool):
               raise ValueError(f"Webull page {page} has_next malformed")
           if not has_next:
               return rows, page
           if not holdings or page == 20:
               raise ValueError("Webull pagination incomplete")
           next_cursor = adapter._first_str(holdings[-1], "instrument_id", "instrumentId")
           if not next_cursor or next_cursor in seen:
               raise ValueError("Webull pagination cursor missing/repeated")
           seen.add(next_cursor)
           cursor = next_cursor
       raise ValueError("Webull pagination cap reached")

   async def check():
       settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
       schwab = SchwabBrokerAdapter(settings)
       account = schwab.accounts_by_name.get("live:schwab_1m_v2")
       if account is None:
           raise ValueError("live:schwab_1m_v2 Schwab account mapping absent")
       status, _headers, response = await schwab._authorized_request_json(
           "GET", f"/trader/v1/accounts/{quote(account.account_hash, safe='')}?fields=positions")
       if not 200 <= status < 300 or not isinstance(response, dict):
           raise ValueError(f"Schwab account response unavailable/malformed: HTTP {status}")
       body = response.get("securitiesAccount", response)
       if not isinstance(body, dict):
           raise ValueError("Schwab securitiesAccount malformed")
       if "positions" in body and not isinstance(body["positions"], list):
           raise ValueError("Schwab positions malformed")
       if "positions" not in body and not (
           isinstance(body.get("currentBalances"), dict) and body.get("accountNumber")
       ):
           raise ValueError("Schwab empty-position response lacks account identity/balances")
       schwab_rows = body.get("positions", [])
       broker_holdings = []
       for row in schwab_rows:
           if not isinstance(row, dict) or not isinstance(row.get("instrument"), dict):
               raise ValueError("Schwab holding shape unreadable")
           symbol = row["instrument"].get("symbol")
           if not isinstance(symbol, str) or not symbol:
               raise ValueError("Schwab holding symbol unreadable")
           long_qty = Decimal(str(row.get("longQuantity", 0)))
           short_qty = Decimal(str(row.get("shortQuantity", 0)))
           if not long_qty.is_finite() or not short_qty.is_finite():
               raise ValueError("Schwab holding quantity nonfinite")
           quantity = long_qty - short_qty
           if quantity:
               broker_holdings.append(("live:schwab_1m_v2", symbol.upper(), quantity))
       webull_holdings, pages = await asyncio.to_thread(webull_rows, WebullBrokerAdapter(settings))
       broker_holdings.extend(webull_holdings)
       engine = build_engine(settings.database_url, connect_timeout_s=5,
                             statement_timeout_ms=5000)
       with Session(engine) as session:
           counts = {account: int(session.scalar(select(func.count()).select_from(OmsManagedPosition)
                        .where(OmsManagedPosition.broker_account_name == account,
                               OmsManagedPosition.status == "open")) or 0)
                     for account in ("live:schwab_1m_v2", "live:orb")}
           virtual_rows = session.execute(select(BrokerAccount.name, VirtualPosition.symbol,
                                                 VirtualPosition.quantity)
                   .join(BrokerAccount, VirtualPosition.broker_account_id == BrokerAccount.id)
                   .where(BrokerAccount.name.in_(("live:schwab_1m_v2", "live:orb")),
                          VirtualPosition.quantity != 0)).all()
           et_now = datetime.now(ZoneInfo("America/New_York"))
           day_start = datetime.combine(et_now.date(), time(4), et_now.tzinfo)
           if et_now < day_start:
               day_start -= timedelta(days=1)
           signed = case((func.upper(Fill.side) == "BUY", Fill.quantity),
                         (func.upper(Fill.side) == "SELL", -Fill.quantity), else_=None)
           fill_rows = session.execute(select(BrokerAccount.name, Fill.symbol,
                                              func.sum(signed), func.count(Fill.id),
                                              func.count(signed))
                   .join(BrokerAccount, Fill.broker_account_id == BrokerAccount.id)
                   .where(BrokerAccount.name.in_(("live:schwab_1m_v2", "live:orb")),
                          Fill.filled_at >= day_start.astimezone(timezone.utc))
                   .group_by(BrokerAccount.name, Fill.symbol)).all()
       if any(total != known for _, _, _, total, known in fill_rows):
           raise ValueError("UNKNOWN fill side prevents net bot-fill proof")
       net_fills = [(account, symbol, str(net)) for account, symbol, net, _, _ in fill_rows if net != 0]
       print(f"FRESH_DIRECT_READ schwab_holding_rows={len(schwab_rows)} "
             f"webull_holding_rows={len(webull_holdings)} webull_pages={pages} "
             f"broker_holdings={[(a, s, str(q)) for a, s, q in broker_holdings]} "
             f"open_managed={counts} nonzero_virtual={virtual_rows} "
             f"fill_session_start_et={day_start.isoformat()} net_bot_fills={net_fills}")
       if any(counts.values()) or virtual_rows or net_fills:
           return 1
       for account, symbol, quantity in broker_holdings:
           print(f"MANUAL_HOLDING_RECORDED_NOT_BLOCKING account={account} "
                 f"symbol={symbol} quantity={quantity}")
       return 0

   try:
       sys.exit(asyncio.run(check()))
   except Exception as exc:
       print(f"UNKNOWN direct-flat-or-managed-read {type(exc).__name__}: {exc}", file=sys.stderr)
       sys.exit(2)
   PY
   sudo chmod 0600 "$FLAT_CHECK"
   sudo sha256sum "$FLAT_CHECK"
   # Invoke as: timeout 45s sudo "$REPO/.venv/bin/python" "$FLAT_CHECK".
   # Require rc=0 before a gateway restart; rc=1/2 wait in the 16:05 loop.
   ```
3. Before touching the shared gateway, preserve its current subscription
   stream/owner-hash evidence and prove the retained stream can reconstruct
   **all four consumer owners** (including explicit empty replace
   events where appropriate). Record their event IDs and current union. If
   any owner is absent, the Redis read is unavailable, or a replay would
   remove a scanner/v2-owned symbol, **do not restart the gateway**. Obtain
   fresh owner replaces or a separately reviewed migration plan first.
   ORB and live orb-schwab are also debounced gateway consumers; record each
   latest replace. The old gateway cannot restore all these owners by itself.
   Capture the raw UTF-8 payload bytes, not a reconstructed event, using the
   read-only Redis command below. The only writes in this step are the
   reviewed local read-only helpers and new evidence files; no service state
   or trading configuration changes.
   The existing file must not be silently overwritten. A missing/trimmed
   scanner, v2, ORB, or ORB Schwab replace refuses the install, including when the last
   known symbol list was empty. Run this O_EXCL capture for every five-minute
   attempt immediately before its 120-second tick control and restart decision.
   Owner IDs may legitimately change later; content proof re-reads current
   owners rather than comparing sets to this capture. Python reading the
   root-only fleet env runs as root; owner/control/replay artifacts are
   root-owned and their later readers run as root. The trader-owned venv's
   `pip install -e` stays as trader.

   ```bash
   REPO=/home/trader/project-mai-tai
   OWNER_CAPTURE=/home/trader/after-hours/2026-10-01/option-a-capture-owners.py
   ORB_REPLAY_APPROVED=1  # Operator approved all active ORB consumers for rollback replay.
   sudo install -d -m 0750 /home/trader/after-hours/2026-10-01
   sudo bash -c 'set -C; cat > "$1"' bash "$OWNER_CAPTURE" <<'PY'
   import base64
   import json
   import os
   import sys
   from pathlib import Path
   from redis import Redis
   from project_mai_tai.events import MarketDataSubscriptionEvent, stream_name
   from project_mai_tai.settings import Settings

   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   redis = Redis.from_url(settings.redis_url, decode_responses=False)
   stream = stream_name(settings.redis_stream_prefix, "market-data-subscriptions")
   # XREVRANGE <=250 compact replace events: measured reply <1 MiB.
   entries = redis.xrevrange(stream, count=settings.redis_market_data_subscription_stream_maxlen)
   required = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab"}
   owners = {}
   seen = set()
   orb_replay_approved = sys.argv[2] == "1"
   if sys.argv[2] not in {"0", "1"}:
       raise SystemExit("invalid ORB replay approval switch")
   for event_id, fields in entries:
       raw = fields.get(b"data")
       if raw is None:
           raise SystemExit("subscription event has no data")
       event = MarketDataSubscriptionEvent.model_validate(json.loads(raw))
       consumer = event.payload.consumer_name
       if consumer in seen:
           continue
       seen.add(consumer)
       if consumer not in required:
           raise SystemExit(f"unexpected subscription consumer: {consumer}")
       if event.payload.mode != "replace":
           raise SystemExit(f"latest {consumer} event is not replace")
       owners[consumer] = {
           "source_id": event_id.decode("ascii"),
           "raw_b64": base64.b64encode(raw).decode("ascii"),
           "symbols": sorted(set(event.payload.symbols)),
       }
   if set(owners) != required:
       raise SystemExit(f"missing retained replace: {sorted(required - set(owners))}")
   if owners["orb"]["symbols"] and not orb_replay_approved:
       raise SystemExit("nonempty ORB owner needs a reviewed rollback replay before any install write")
   # HGETALL owner hash: four consumer sets plus metadata, <1 MiB reply.
   saved_hash = redis.hgetall(stream_name(settings.redis_stream_prefix, "market-data-subscription-owners"))
   if saved_hash:
       for consumer, record in owners.items():
           encoded = saved_hash.get(consumer.encode("ascii"))
           if encoded is None or set(json.loads(encoded)) != set(record["symbols"]):
               raise SystemExit(f"retained event disagrees with owner hash: {consumer}")
       for raw_consumer, encoded in saved_hash.items():
           consumer = raw_consumer.decode("ascii")
           if not consumer.startswith("_") and consumer not in required:
               raise SystemExit(f"unexpected owner hash consumer: {consumer}")
   path = Path(sys.argv[1])
   fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, "w", encoding="utf-8") as output:
       json.dump({"owners": owners, "orb_replay_approved": orb_replay_approved,
                  "stream": stream}, output, sort_keys=True)
       output.write("\n")
   print("preserved owner event IDs", {name: item["source_id"] for name, item in owners.items()})
   PY
   sudo chmod 0600 "$OWNER_CAPTURE"
   sudo sha256sum "$OWNER_CAPTURE"
   ```
   The 120-second control reads the retained old-gateway stream ending
   immediately before each restart decision. It must cover the entire window;
   a trimmed/unreadable stream is UNKNOWN, not zero ticks. This reviewed
   read-only helper writes one O_EXCL file per attempt and prints per-symbol
   trade/quote denominators:

   ```bash
   TICK_CONTROL=/home/trader/after-hours/2026-10-01/option-a-tick-control.py
   sudo bash -c 'set -C; cat > "$1"' bash "$TICK_CONTROL" <<'PY'
   import json
   import os
   import sys
   from datetime import UTC, datetime, timedelta
   from pathlib import Path
   from redis import Redis
   from project_mai_tai.events import stream_name
   from project_mai_tai.settings import Settings

   saved = json.loads(Path(sys.argv[1]).read_text())
   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   redis = Redis.from_url(settings.redis_url, decode_responses=True)
   key = stream_name(settings.redis_stream_prefix, "market-data")
   end = datetime.now(UTC)
   start = end - timedelta(seconds=120)
   start_ms = int(start.timestamp() * 1000)
   end_ms = int(end.timestamp() * 1000)
   first = redis.xrange(key, count=1)  # One tick event, <16 KiB reply.
   if not first or int(first[0][0].split("-")[0]) > start_ms:
       raise SystemExit("UNKNOWN 120s control stream missing/trimmed")
   symbols = set(settings.market_data_static_symbol_list)
   for row in saved["owners"].values():
       symbols.update(row["symbols"])
   counts = {name: {"trade_tick": 0, "quote_tick": 0} for name in sorted(symbols)}
   cursor = f"{start_ms}-0"
   scanned = 0
   while True:
       rows = redis.xrange(key, min=cursor, max=f"({end_ms + 1}-0", count=100)  # <1 MiB measured reply.
       if not rows:
           break
       for _, fields in rows:
           event = json.loads(fields["data"])
           kind = event.get("event_type")
           symbol = event.get("payload", {}).get("symbol")
           if (event.get("source_service") == "market-data-gateway"
                   and kind in {"trade_tick", "quote_tick"} and symbol in counts):
               stamp = datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
               if start < stamp <= end:
                   counts[symbol][kind] += 1
           scanned += 1
       cursor = f"({rows[-1][0]}"
   output = {"start_utc": start.isoformat(), "end_utc": end.isoformat(),
             "owner_ids": {name: row["source_id"] for name, row in saved["owners"].items()},
             "counts": counts, "scanned": scanned}
   fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, "w", encoding="utf-8") as out:
       json.dump(output, out, sort_keys=True)
       out.write("\n")
   print("CONTROL", output)
   PY
   sudo chmod 0600 "$TICK_CONTROL"
   sudo sha256sum "$TICK_CONTROL"
   ```
   Every Redis read in this plan has a bounded reply. `INFO stats` and
   `INFO memory` are under 64 KiB each; `SCAN count=100` returns key names
   only (under 1 MiB at the measured key count), and each `TYPE` is one word.
   `market-data` pages use `count=100` (under 1 MiB per measured page),
   `heartbeats count=25` under 256 KiB, `strategy-state-isolated count=1`
   under 1 MiB, subscription-event pages at the configured 250 maximum
   under 1 MiB, and the owner hash under 1 MiB. **Snapshot batches are about
   6 MiB each: read at most one per call, never a batch of batches.** Refuse
   if any measured reply exceeds 20 MiB; do not retry with a larger count.
   Create this read-only eviction/presence guard with O_EXCL. The capture is
   taken just before restart; every subsequent check compares to that exact
   baseline. New streams may appear, but no preexisting stream may disappear.
   If `evicted_keys` changes or a stream vanishes, call UNKNOWN, page, and
   stop: do not enter or repeat the rollback loop.

   ```bash
   REDIS_GUARD=/home/trader/after-hours/2026-10-01/option-a-redis-guard.py
   sudo bash -c 'set -C; cat > "$1"' bash "$REDIS_GUARD" <<'PY'
   import json
   import os
   import sys
   from pathlib import Path
   from redis import Redis
   from project_mai_tai.settings import Settings

   mode, baseline_path = sys.argv[1:3]
   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   redis = Redis.from_url(settings.redis_url, decode_responses=True)
   # INFO replies <64 KiB each; SCAN count=100 returns names only, <1 MiB
   # at the measured key count; TYPE replies are one word per key.
   evicted = int(redis.info("stats")["evicted_keys"])
   used = int(redis.info("memory")["used_memory"])
   cursor = 0
   streams = set()
   while True:
       cursor, keys = redis.scan(cursor, match="mai_tai:*", count=100)
       for key in keys:
           if redis.type(key) == "stream":
               streams.add(key)
       if cursor == 0:
           break
   required = {"mai_tai:market-data-subscriptions", "mai_tai:market-data",
               "mai_tai:snapshot-batches", "mai_tai:heartbeats",
               "mai_tai:strategy-state", "mai_tai:strategy-state-isolated"}
   missing_required = sorted(required - streams)
   print(f"REDIS_SAFETY mode={mode} evicted_keys={evicted} used_memory={used} "
         f"stream_count={len(streams)} missing_required={missing_required}")
   if missing_required:
       raise SystemExit("UNKNOWN required stream absent; stop and page")
   if mode == "capture":
       if used > 1_600_000_000:
           raise SystemExit("UNKNOWN Redis used_memory >1.6 GB; refuse restart")
       fd = os.open(baseline_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
       with os.fdopen(fd, "w", encoding="utf-8") as output:
           json.dump({"evicted_keys": evicted, "streams": sorted(streams),
                      "used_memory": used}, output, sort_keys=True)
           output.write("\n")
   elif mode == "check":
       baseline = json.loads(Path(baseline_path).read_text())
       lost = sorted(set(baseline["streams"]) - streams)
       if evicted != baseline["evicted_keys"] or lost:
           print(f"UNKNOWN REDIS_EVICTION_OR_STREAM_LOSS delta={evicted-baseline['evicted_keys']} "
                 f"lost={lost}; STOP, PAGE, NO ROLLBACK LOOP", file=sys.stderr)
           raise SystemExit(2)
   else:
       raise SystemExit("UNKNOWN invalid Redis guard mode")
   PY
   sudo chmod 0600 "$REDIS_GUARD"
   sudo sha256sum "$REDIS_GUARD"
   ```
4. Claude-1 approved the accepted-with-gaps inactive control after
   recomputing load, snapshot cadence, heartbeats, LGHL lag and OMS refusals.
   Claude-1 **did not recompute** VBIO/TGE lag rows or the OMS eligible-intent
   denominator. Preserve those caveats. #1074's automatic treatment guard
   is pinned and merged; recheck the final source and first-session thresholds
   against the reviewed baseline before GO. If a required treatment signal
   cannot be observed, stop the whole install before the first write.
5. Capture hashes of `/home/trader/preopen.sh`, the isolated files in
   `/home/trader/restart_evidence/`, the current checkout and service units;
   capture the current restart-evidence install record and fleet journal.
   No env keys may change. No database migration,
   unrelated service restart, Momentum replay, or live Momentum order route
   is in scope.
6. The operator explicitly accepted the 1-of-2 ORB partial-fill gap for this
   run and assigned daily 15:55 ET position review to Claude-1 (board B row
   46). That is a known unproven exit lifecycle, **not** a proven protection
   path. Fresh flatness, native-bracket verification and the current risk
   limits remain mandatory; this acceptance does not authorize a larger size.
   Also not covered tonight: ORB INC1 sources missing from the installed
   watch, and an attended live Schwab place/replace/cancel test before the
   first order. Record these as open risks in the deploy journal, not PASS.
7. Capture OMS, strategy, v2, ORB and orb-schwab PID, start and `NRestarts`
   as a new root-owned O_EXCL file. Use the same command substitution below
   after the new gateway proof and again before and after any rollback checkout
   switch. A byte mismatch is a hard stop, not a silent re-pin.

   ```bash
   LIVE_IDS_FILE=/home/trader/after-hours/2026-10-01/option-a-live-identities-1605-go.txt
   LIVE_UNITS=(project-mai-tai-oms project-mai-tai-strategy project-mai-tai-schwab-1m-v2 project-mai-tai-orb project-mai-tai-orb-schwab)
   for unit in "${LIVE_UNITS[@]}"; do
       printf '%s\n' "$unit"
       systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"
   done | sudo bash -c 'set -C; cat > "$1"' bash "$LIVE_IDS_FILE"
   sudo cat "$LIVE_IDS_FILE"
   ```

## 2. Gateway phase at or after 16:05 ET on 2026-10-01

1. Execute these literal commands only after independent review of this
   refresh and section 1's unchanged service identities. The loop below
   enforces a fresh direct broker read and zero bot-owned exposure immediately before
   restart; armed segments are recorded without blocking. Each attempt gets
   a new owner and tick-control artifact. The owner artifact must contain
   the latest raw `replace`
   event and symbol set for strategy-engine, schwab-1m-v2, orb and orb-schwab;
   absent/truncated history or a fifth consumer is a hard stop. An ID change
   during the proof is expected and is judged against the current owner read.
   Record the initial OMS, strategy, v2, orb and orb-schwab PIDs/start times;
   all five must remain unchanged. Paper is already stopped and stays stopped.

   ```bash
   set -euo pipefail
   REPO=/home/trader/project-mai-tai
   TARGET_SHA=01a64e9b7552b673e7db6f6e6c787b77b16f6f22
   OWNER_CAPTURE=/home/trader/after-hours/2026-10-01/option-a-capture-owners.py
   TICK_CONTROL=/home/trader/after-hours/2026-10-01/option-a-tick-control.py
   FLAT_CHECK=/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py
   REDIS_GUARD=/home/trader/after-hours/2026-10-01/option-a-redis-guard.py
   ORB_REPLAY_APPROVED=1
   test "$(TZ=America/New_York date +%F)" = 2026-10-01
   test "$(TZ=America/New_York date +%H%M%S)" -ge 160500
   test "$(TZ=America/New_York date +%H%M%S)" -lt 191500
   sudo test -s "$OWNER_CAPTURE" && sudo test -s "$TICK_CONTROL" && sudo test -s "$FLAT_CHECK" && sudo test -s "$REDIS_GUARD"
   test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$TARGET_SHA"
   test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
   test "$(systemctl show -p ActiveState --value project-mai-tai-momentum-paper.service)" = inactive
   sudo -u trader git -C "$REPO" fetch origin main:refs/remotes/origin/main
   sudo -u trader git -C "$REPO" cat-file -e "$TARGET_SHA^{commit}"
   sudo -u trader git -C "$REPO" merge-base --is-ancestor "$TARGET_SHA" origin/main
   sudo -u trader git -C "$REPO" diff --name-only "$TARGET_SHA" origin/main | while IFS= read -r path; do
       case "$path" in docs/*) ;; *) printf 'REFUSE non-doc main diff: %s\n' "$path" >&2; exit 1 ;; esac
   done
   test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
   sudo -u trader "$REPO/.venv/bin/python" -c 'import project_mai_tai, pathlib; print(pathlib.Path(project_mai_tai.__file__).resolve())'
   test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$TARGET_SHA"
   sudo install -m 0644 "$REPO/ops/systemd/project-mai-tai-option-a-guard@.service" /etc/systemd/system/project-mai-tai-option-a-guard@.service
   sudo install -m 0644 "$REPO/ops/systemd/project-mai-tai-option-a-guard-failure@.service" /etc/systemd/system/project-mai-tai-option-a-guard-failure@.service
   test "$(sha256sum "$REPO/ops/systemd/project-mai-tai-option-a-guard@.service" | awk '{print $1}')" = "$(sha256sum /etc/systemd/system/project-mai-tai-option-a-guard@.service | awk '{print $1}')"
   test "$(sha256sum "$REPO/ops/systemd/project-mai-tai-option-a-guard-failure@.service" | awk '{print $1}')" = "$(sha256sum /etc/systemd/system/project-mai-tai-option-a-guard-failure@.service | awk '{print $1}')"
   sudo systemd-analyze verify /etc/systemd/system/project-mai-tai-option-a-guard@.service /etc/systemd/system/project-mai-tai-option-a-guard-failure@.service
   sudo systemctl daemon-reload
   LIVE_IDS_FILE=/home/trader/after-hours/2026-10-01/option-a-live-identities-1605-go.txt
   LIVE_UNITS=(project-mai-tai-oms project-mai-tai-strategy project-mai-tai-schwab-1m-v2 project-mai-tai-orb project-mai-tai-orb-schwab)
   ATTEMPT_JOURNAL=/home/trader/after-hours/2026-10-01/option-a-attempts-1605-go.log
   sudo bash -c 'set -C; : > "$1"' bash "$ATTEMPT_JOURNAL"
   V2_PID="$(systemctl show -p MainPID --value project-mai-tai-schwab-1m-v2.service)"
   sudo "$REPO/.venv/bin/python" - "$V2_PID" <<'PY'
   import sys
   from pathlib import Path
   raw = Path(f"/proc/{sys.argv[1]}/environ").read_bytes().split(b"\0")
   env = dict(pair.decode("utf-8").split("=", 1) for pair in raw if b"=" in pair)
   hour = env.get("MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_WINDOW_END_HOUR_ET")
   minute = env.get("MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_WINDOW_END_MINUTE_ET")
   print("v2 running entry-window end", hour, minute, "PID", sys.argv[1])
   if (hour, minute) != ("15", "45"):
       raise SystemExit("UNKNOWN/WRONG running v2 entry-window cutoff; do not treat arms as inert")
   PY
   while :; do
       test "$(TZ=America/New_York date +%F)" = 2026-10-01
       if test "$(TZ=America/New_York date +%H%M%S)" -ge 191500; then
           printf 'REFUSE: last gateway restart cutoff 19:15 ET reached\n' >&2
           exit 1
       fi
       test "$(for unit in "${LIVE_UNITS[@]}"; do printf '%s\n' "$unit"; systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"; done)" = "$(sudo cat "$LIVE_IDS_FILE")"
       ATTEMPT="$(date -u +%Y%m%dT%H%M%S%N)"
       OWNER_FILE="/home/trader/after-hours/2026-10-01/option-a-owners-${ATTEMPT}.json"
       CONTROL_FILE="/home/trader/after-hours/2026-10-01/option-a-control-${ATTEMPT}.json"
       printf 'PREFLIGHT_ATTEMPT utc=%s owner=%s control=%s\n' "$(date -u +%FT%TZ)" "$OWNER_FILE" "$CONTROL_FILE" | sudo tee -a "$ATTEMPT_JOURNAL"
       sudo "$REPO/.venv/bin/python" "$OWNER_CAPTURE" "$OWNER_FILE" "$ORB_REPLAY_APPROVED"
       sudo "$REPO/.venv/bin/python" "$TICK_CONTROL" "$OWNER_FILE" "$CONTROL_FILE"
       if sudo "$REPO/.venv/bin/python" - "$OWNER_FILE" <<'PY'; then
   import json, sys
   from pathlib import Path
   from redis import Redis
   from project_mai_tai.events import MarketDataSubscriptionEvent
   from project_mai_tai.settings import Settings
   saved = json.loads(Path(sys.argv[1]).read_text())
   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   redis = Redis.from_url(settings.redis_url, decode_responses=True)
   current = {}
   # XREVRANGE <=250 compact replace events, <1 MiB measured reply.
   for event_id, fields in redis.xrevrange(saved["stream"], count=settings.redis_market_data_subscription_stream_maxlen):
       event = MarketDataSubscriptionEvent.model_validate(json.loads(fields["data"]))
       current.setdefault(event.payload.consumer_name, event_id)
   captured = {name: row["source_id"] for name, row in saved["owners"].items()}
   if set(current) != set(captured):
       print(f"UNKNOWN owner set changed/unexpected during control: {current}", file=sys.stderr)
       raise SystemExit(2)
   if current != captured:
       print(f"OWNER_MOVED during control; recapture: {captured} -> {current}", file=sys.stderr)
       raise SystemExit(3)
   print("control owner source IDs stable until restart decision", current)
   PY
           :
       else
           OWNER_RC=$?
           printf 'OWNER_CHECK rc=%s owner=%s control=%s\n' "$OWNER_RC" "$OWNER_FILE" "$CONTROL_FILE" | sudo tee -a "$ATTEMPT_JOURNAL"
           if test "$OWNER_RC" -eq 3; then continue; fi
           exit 2
       fi
       if sudo "$REPO/.venv/bin/python" - <<'PY'; then
   import json
   from redis import Redis
   from project_mai_tai.events import stream_name
   from project_mai_tai.settings import Settings
   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   # One strategy-state event, <1 MiB reply.
   rows = Redis.from_url(settings.redis_url, decode_responses=True).xrevrange(
       stream_name(settings.redis_stream_prefix, "strategy-state-isolated"), count=1)
   if rows:
       event = json.loads(rows[0][1]["data"])
       print("ARMED_RECORDED_NOT_BLOCKING", event.get("produced_at"),
             event.get("payload", {}).get("cw_armed_segments"))
   else:
       print("ARMED_UNKNOWN_NOT_BLOCKING no published state")
   PY
           :
       else
           echo 'ARMED_UNKNOWN_NOT_BLOCKING published state unreadable'
       fi
       if FLAT_RESULT="$(timeout 45s sudo "$REPO/.venv/bin/python" "$FLAT_CHECK" 2>&1)"; then
           printf 'PREFLIGHT_ATTEMPT rc=0 %s\n' "$FLAT_RESULT" | sudo tee -a "$ATTEMPT_JOURNAL"
           break
       else
           FLAT_RC=$?
           printf 'PREFLIGHT_ATTEMPT rc=%s at=%s %s\n' "$FLAT_RC" "$(date -u +%FT%TZ)" "$FLAT_RESULT" | sudo tee -a "$ATTEMPT_JOURNAL"
           printf 'NO_CLEAR_READ rc=%s wait 300s, then new capture/control/flat read\n' "$FLAT_RC"
           sleep 300
       fi
   done
   test "$(TZ=America/New_York date +%H%M%S)" -lt 191500
   REDIS_BASELINE="/home/trader/after-hours/2026-10-01/option-a-redis-before-${ATTEMPT}.json"
   sudo "$REPO/.venv/bin/python" "$REDIS_GUARD" capture "$REDIS_BASELINE"
   printf '%s\n' "$REDIS_BASELINE" | sudo bash -c 'set -C; cat > "$1"' bash /home/trader/after-hours/2026-10-01/option-a-redis-baseline-path-1605-go.txt
   RESTART_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   sudo systemctl restart project-mai-tai-market-data.service
   sudo systemctl is-active --quiet project-mai-tai-market-data.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-market-data.service
   POST_START_UTC="$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)"  # After restart returned; content lower bound.
   sudo "$REPO/.venv/bin/python" "$REDIS_GUARD" check "$REDIS_BASELINE" || { echo 'UNKNOWN Redis eviction after restart: STOP, PAGE, NO ROLLBACK'; exit 2; }
   set +e
   sudo "$REPO/.venv/bin/python" - "$OWNER_FILE" "$CONTROL_FILE" "$RESTART_UTC" "$POST_START_UTC" "$REDIS_BASELINE" <<'PY'
   import json
   import math
   import sys
   import time
   from collections import defaultdict
   from datetime import UTC, datetime, timedelta
   from pathlib import Path
   from redis import Redis
   from project_mai_tai.events import MarketDataSubscriptionEvent, stream_name
   from project_mai_tai.settings import Settings

   captured = json.loads(Path(sys.argv[1]).read_text())
   control = json.loads(Path(sys.argv[2]).read_text())
   restarted = datetime.fromisoformat(sys.argv[3].replace("Z", "+00:00"))
   post_start = datetime.fromisoformat(sys.argv[4].replace("Z", "+00:00"))
   if post_start <= restarted:
       raise SystemExit("UNKNOWN post-restart lower bound is not after restart anchor")
   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   redis = Redis.from_url(settings.redis_url, decode_responses=True)
   redis_baseline = json.loads(Path(sys.argv[5]).read_text())
   baseline_streams = redis_baseline["streams"]
   heartbeat_key = stream_name(settings.redis_stream_prefix, "heartbeats")
   snapshot_key = stream_name(settings.redis_stream_prefix, "snapshot-batches")
   market_key = stream_name(settings.redis_stream_prefix, "market-data")
   owner_key = stream_name(settings.redis_stream_prefix, "market-data-subscription-owners")
   required_owners = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab"}
   assert set(captured["owners"]) == required_owners
   assert datetime.fromisoformat(control["end_utc"]) <= restarted
   captured_ids = {name: row["source_id"] for name, row in captured["owners"].items()}
   control_positive = {name for name, kinds in control["counts"].items() if sum(kinds.values()) > 0}
   tick_end = post_start + timedelta(seconds=120)
   proof_end = restarted + timedelta(seconds=180)

   def current_owners():
       newest = {}
       # <=250 compact subscription events, <1 MiB measured reply.
       for event_id, fields in redis.xrevrange(captured["stream"], count=settings.redis_market_data_subscription_stream_maxlen):
           event = MarketDataSubscriptionEvent.model_validate(json.loads(fields["data"]))
           name = event.payload.consumer_name
           if name in newest:
               continue
           if name not in required_owners or event.payload.mode != "replace":
               raise ValueError(f"unexpected/non-replace consumer {name}")
           newest[name] = {"source_id": event_id, "symbols": set(event.payload.symbols)}
       if set(newest) != required_owners:
           raise ValueError(f"missing retained consumers {sorted(required_owners - set(newest))}")
       return newest

   if datetime.now(UTC) >= proof_end:
       raise SystemExit("UNKNOWN forward proof started after 180s; single rollback only")
   start_ms = int(post_start.timestamp() * 1000)
   end_ms = int(tick_end.timestamp() * 1000)
   post_counts = defaultdict(lambda: {"trade_tick": 0, "quote_tick": 0})
   scanned = 0
   trimmed = None
   tick_error = None
   tick_scanned = False
   # One snapshot batch (about 6 MiB) per XRANGE reply; use stream-ID ms,
   # never fetch 180 large payloads or trust a pre-restart produced_at.
   snapshot_cursor = f"{start_ms}-0"
   snapshot_ms = []
   cadence_error = None
   while True:
       now = datetime.now(UTC)
       # INFO stats <64 KiB and EXISTS returns one integer; any eviction/key
       # loss ends this proof immediately, before another snapshot read.
       if (int(redis.info("stats")["evicted_keys"]) != redis_baseline["evicted_keys"]  # <64 KiB.
               or redis.exists(*baseline_streams) != len(baseline_streams)):  # One integer, <1 KiB.
           raise SystemExit("UNKNOWN Redis eviction/stream loss during proof; STOP, PAGE, NO ROLLBACK")
       if not cadence_error:
           try:
               rows = redis.xrange(snapshot_key, min=f"({snapshot_cursor}", count=1)  # One batch, ~5.5 MiB.
               if rows:
                   snapshot_cursor = rows[0][0]
                   batch_ms = int(snapshot_cursor.split("-")[0])
                   if batch_ms > start_ms:
                       snapshot_ms.append(batch_ms)
           except Exception as exc:
               cadence_error = f"{type(exc).__name__}:{exc}"
       if not tick_scanned and now >= tick_end:
           tick_scanned = True
           try:
               # market-data pages: count=100, measured <1 MiB each.
               first = redis.xrange(market_key, count=1)  # One tick, <16 KiB reply.
               trimmed = not first or int(first[0][0].split("-")[0]) > start_ms
               cursor = f"{start_ms}-0"
               while True:
                   rows = redis.xrange(market_key, min=cursor,
                                       max=f"({end_ms + 1}-0", count=100)  # ~32 KiB measured.
                   if not rows:
                       break
                   for _, fields in rows:
                       event = json.loads(fields["data"])
                       kind = event.get("event_type")
                       symbol = event.get("payload", {}).get("symbol")
                       if (event.get("source_service") == "market-data-gateway"
                               and kind in {"trade_tick", "quote_tick"} and symbol):
                           stamp = datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
                           if post_start < stamp <= tick_end:
                               post_counts[symbol][kind] += 1
                       scanned += 1
                   cursor = f"({rows[-1][0]}"
           except Exception as exc:
               tick_error = f"{type(exc).__name__}:{exc}"
       owner_error = heartbeat_error = None
       current = {}
       expected = set()
       owners = actual = {}
       extra = []
       paper = None
       ids_stable = False
       try:
           current = current_owners()
           expected = set(settings.market_data_static_symbol_list)
           for row in current.values():
               expected.update(row["symbols"])
           owners = redis.hgetall(owner_key)  # Four owner sets + metadata, <1 MiB reply.
           actual = {name: set(json.loads(owners[name])) if name in owners else None for name in required_owners}
           extra = sorted(set(owners) - required_owners - {"_migration_complete", "_last_applied_id", "momentum-paper", "static"})
           paper = json.loads(owners.get("momentum-paper", "[]"))
           ids_stable = {name: row["source_id"] for name, row in current_owners().items()} == {name: row["source_id"] for name, row in current.items()}
       except Exception as exc:
           owner_error = f"{type(exc).__name__}:{exc}"
       produced = age = active = status = None
       try:
           # Last 25 heartbeat events, <256 KiB reply.
           gateway = next((event for _, fields in redis.xrevrange(heartbeat_key, count=25)
                           if (event := json.loads(fields["data"])).get("source_service") == "market-data-gateway"), None)
           produced = datetime.fromisoformat(gateway["produced_at"].replace("Z", "+00:00")) if gateway else None
           age = (now - produced).total_seconds() if produced else None
           active = gateway.get("payload", {}).get("details", {}).get("active_symbols") if gateway else None
           status = gateway.get("payload", {}).get("status") if gateway else None
       except Exception as exc:
           heartbeat_error = f"{type(exc).__name__}:{exc}"
       heartbeat_ok = (not owner_error and not heartbeat_error and produced is not None
                       and post_start < produced <= now and 0 <= age <= 30.819
                       and status == "healthy" and str(active) == str(len(expected)))
       intervals = [(b - a) / 1000 for a, b in zip(snapshot_ms, snapshot_ms[1:])]
       p95 = sorted(intervals)[math.ceil(.95 * len(intervals)) - 1] if intervals else None
       cadence_ok = cadence_error is None and len(intervals) >= 10 and p95 <= 10
       required_ticks = control_positive & expected
       excused = {name: ("no_pre_restart_tick" if name in expected else "no_longer_owned")
                  for name in (set(control["counts"]) | expected) - required_ticks}
       missing_ticks = sorted(name for name in required_ticks if not sum(post_counts[name].values()))
       ticks_ok = tick_scanned and tick_error is None and not trimmed and not missing_ticks
       owners_ok = (owner_error is None and ids_stable and owners.get("_migration_complete") == "1"
                    and not extra and not paper and all(actual[name] == current[name]["symbols"] for name in required_owners))
       if (heartbeat_ok and ticks_ok and cadence_ok and owners_ok) or now >= proof_end:
           break
       time.sleep(1)
   in_time = datetime.now(UTC) <= proof_end
   current_ids = {name: row["source_id"] for name, row in current.items()}
   expected_owner_sets = {name: sorted(row["symbols"]) for name, row in current.items()}
   print("owner source IDs captured", captured_ids, "current", current_ids)
   print(f"content(1) {'PASS' if heartbeat_ok and in_time else 'NOT_PROVEN'} heartbeat={produced} status={status} age_s={age} active={active} expected={len(expected)} error={heartbeat_error or owner_error}")
   print(f"content(2) {'PASS' if ticks_ok and in_time else 'NOT_PROVEN'} control={control['start_utc']}..{control['end_utc']} post={post_start.isoformat()}..{tick_end.isoformat()} scanned={scanned} trimmed={trimmed} required={sorted(required_ticks)} counts={dict(post_counts)} excused={excused} missing={missing_ticks} error={tick_error}")
   print(f"content(3) {'PASS' if cadence_ok and in_time else 'NOT_PROVEN'} intervals={len(intervals)} p95_s={p95} max_s={max(intervals) if intervals else None} error={cadence_error}")
   print(f"content(4) {'PASS' if owners_ok and in_time else 'NOT_PROVEN'} migration={owners.get('_migration_complete')} actual={actual} expected={expected_owner_sets} ids_stable={ids_stable} extra={extra} paper={paper} error={owner_error}")
   if not (heartbeat_ok and ticks_ok and cadence_ok and owners_ok and in_time):
       raise SystemExit("new gateway content proof UNKNOWN; single rollback only")
   PY
   PROOF_RC=$?
   set -e
   if ! sudo "$REPO/.venv/bin/python" "$REDIS_GUARD" check "$REDIS_BASELINE"; then
       echo 'UNKNOWN Redis eviction/stream loss; STOP, PAGE, NO ROLLBACK' >&2
       exit 2
   fi
   if test "$PROOF_RC" -ne 0; then
       echo 'UNKNOWN content proof; execute section 4 single rollback immediately after fresh Redis safety check' >&2
       exit 3
   fi
   test "$(for unit in "${LIVE_UNITS[@]}"; do printf '%s\n' "$unit"; systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"; done)" = "$(sudo cat "$LIVE_IDS_FILE")"
   test "$(systemctl show -p ActiveState --value project-mai-tai-momentum-paper.service)" = inactive
   GATEWAY_ID_FILE=/home/trader/after-hours/2026-10-01/option-a-gateway-identity-1605-go.txt
   systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-market-data.service | sudo bash -c 'set -C; cat > "$1"' bash "$GATEWAY_ID_FILE"
   ```

   The content proof prints (1) the latest
   heartbeat/status/current-union count, (2) control-paired per-symbol
   trade/quote counts within 120 seconds with explicit excusals, (3) at least
   10 snapshot intervals and p95, and (4) the migrated owner hash against
   current retained sets (printing captured and current source IDs). An
   unproven condition is UNKNOWN and invokes
   only the newly authorized single rollback. INFO log lines are not proof.

   There is no checkout switch or runtime reinstall in the forward phase:
   Mode B already left the clean box at this exact service SHA. The import
   path and clean-tree checks remain mandatory. Do **not** use `deploy_main.sh`,
   `deploy_service.sh market-data`, or `08_install_runtime.sh`: they touch
   companion/unrelated units. A fresh healthy heartbeat and 5-second nominal
   snapshot cadence must also be observed; the block above's heartbeat is
   not a substitute for the sustained cadence check. No ORB/OMS/v2/strategy
   process may change identity **during this gateway phase**. If the
   180-second content check fails, use only section 4's one-time rollback;
   do not continue to a guard or paper start.
2. The ORB live, OMS/strategy companion, FLAGGATE and restart-gate phases
   were completed under the separate 2026-09-30 Mode B GO. Do not repeat
   their env edits, restarts, isolated-file installs, install-record writes,
   or preopen re-pin during this Mode A gateway phase. Preserve all five
   running process identities; a changed PID/start or `NRestarts` is a hard
   stop. The 2026-10-01 preopen result's historical traceback limitation
   remains an explicitly known UNKNOWN until a separately reviewed fix.

## 3. Overnight guard and conditional paper start

1. After the four gateway content checks pass, record its new PID/start,
   `NRestarts`, exact box SHA, stream evidence and preserved owner sets.
   Recheck OMS, strategy, v2, orb and orb-schwab PID/start/`NRestarts`
   against preflight. These services must not restart. Paper remains STOPPED
   tonight; no treatment claim, paper intent, or live fill follows merely
   from successful gateway restoration. Record the installed unit hashes.
   If the gateway is not healthy or the five identities drift, refuse the
   next step and report. Keep the 2026-10-01 restart-gate UNKNOWN from the
   historical untimestamped traceback distinct from gateway content proof.
2. At 19:59 ET, before the expected 20:00:06 ET `copytruncate`, capture
   the gateway log device/inode and byte size. After 20:00:06, require the
   **same** inode with a smaller size. If no drop is observed by 20:03 ET,
   or the log is unreadable/replaced, the rotation proof is UNKNOWN and
   paper remains stopped. Do not start the sampler at an unproven byte
   offset. Use a new O_EXCL evidence file under the 2026-10-01 directory.
   Start only `project-mai-tai-option-a-guard@2026-10-02.service` after
   rotation proof, with its reviewed OnFailure unit installed. The guard
   itself starts the 1008 sampler and begins the 1 Hz load audit at 07:00 ET.
   Before 07:00, only the sampler JSONL advances; the guard audit JSONL has
   a startup record, while systemd watchdog pulses prove the live guard loop.
   Verify the unit active, the sampler JSONL advancing and the guard startup
   record present; record PID, offset, sample
   timestamps, device/inode, hashes and journal path. The guard's treatment
   date is **2026-10-02**, not the date of this gateway restart.

   ```bash
   set -euo pipefail
   REPO=/home/trader/project-mai-tai
   GATEWAY_LOG=/var/log/project-mai-tai/market-data.log
   ROTATION_EVIDENCE=/home/trader/after-hours/2026-10-01/option-a-gateway-rotation-1605-go.txt
   test "$(TZ=America/New_York date +%H%M%S)" -lt 200000
   ROT_BEFORE_ID="$(stat -c '%d:%i' "$GATEWAY_LOG")"
   ROT_BEFORE_SIZE="$(stat -c '%s' "$GATEWAY_LOG")"
   sudo "$REPO/.venv/bin/python" - "$ROTATION_EVIDENCE" "$ROT_BEFORE_ID" "$ROT_BEFORE_SIZE" <<'PY'
   import os, sys
   from datetime import UTC, datetime
   fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, "w") as out:
       out.write(f"before_utc={datetime.now(UTC).isoformat()} id={sys.argv[2]} size={sys.argv[3]}\n")
   PY
   ROTATION_PROVED=0
   for attempt in $(seq 1 180); do
       ET_NOW="$(TZ=America/New_York date +%H%M%S)"
       ROT_AFTER_ID="$(stat -c '%d:%i' "$GATEWAY_LOG")"
       ROT_AFTER_SIZE="$(stat -c '%s' "$GATEWAY_LOG")"
       if test "$ROT_AFTER_ID" != "$ROT_BEFORE_ID"; then break; fi
       if test "$ET_NOW" -ge 200006 && test "$ROT_AFTER_SIZE" -lt "$ROT_BEFORE_SIZE"; then
           ROTATION_PROVED=1
           break
       fi
       sleep 1
   done
   printf 'after_utc=%s id=%s size=%s proved=%s\n' "$(date -u +%FT%TZ)" "$ROT_AFTER_ID" "$ROT_AFTER_SIZE" "$ROTATION_PROVED" | sudo tee -a "$ROTATION_EVIDENCE"
   test "$ROTATION_PROVED" = 1
   sudo systemctl is-active --quiet project-mai-tai-market-data.service
   test "$(systemctl show -p ActiveState --value project-mai-tai-momentum-paper.service)" = inactive
   sudo systemctl start project-mai-tai-option-a-guard@2026-10-02.service
   sudo systemctl is-active --quiet project-mai-tai-option-a-guard@2026-10-02.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p WatchdogUSec -p NRestarts project-mai-tai-option-a-guard@2026-10-02.service
   TREATMENT=/home/trader/after-hours/2026-10-02/option-a-treatment
   for attempt in $(seq 1 10); do
       if sudo test -s "$TREATMENT/option-a-guard.jsonl" && sudo test -s "$TREATMENT/option-a-1008.jsonl"; then break; fi
       sleep 1
   done
   sudo test -s "$TREATMENT/option-a-guard.jsonl"
   sudo test -s "$TREATMENT/option-a-1008.jsonl"
   ```

3. The reviewed guard permits paper to be inactive from 20:00 ET until
   05:15 ET: before 07:00 it continuously checks the sampler and watchdog,
   but does not require paper active or evaluate the minute-by-minute
   trading slowdown. A new gateway 1008, collector error, blind sample,
   crash or watchdog miss can stop the already-stopped paper and page;
   it must **not** be reset or ignored to permit a morning start. At
   05:15 ET on 2026-10-02, an attended one-shot check must verify the
   gateway PID/start is unchanged since section 2, its latest heartbeat
   healthy/fresh with the preserved union, the guard active with no stop
   trigger, and the 1008 JSONL fresh with continuous 1 Hz rows. The guard
   audit JSONL is not refreshed before 07:00 by the reviewed code. The
   operator's 2026-10-01 ruling instead requires the guard unit active,
   `NRestarts=0`, its `action=start` record for treatment date 2026-10-02,
   and the 1008 JSONL modified within five seconds with continuous 1 Hz
   rows. Do not require fresh guard-audit mtime. Recheck
   the direct two-broker and bot-book check as RECORDED_NOT_BLOCKING; record armed segments
   without blocking
   immediately before starting paper. If any proof is missing, leave
   paper STOPPED and page; do not restart the gateway or a live service.
   Only then start the new Momentum-paper and verify its PID, condition
   provenance, at most 16 own subscriptions, no second Massive connection,
   and paper-only/no broker route. Confirm guard/files at 06:30 ET. Re-pin
   preopen for 2026-10-02 to the actual gateway PID using the separately
   reviewed preopen procedure, preserving OMS/strategy/v2 pins; the
   06:20 gate is read-only and may still call restart continuity UNKNOWN
   until the separate traceback checker fix is installed. The first
   treatment window is 07:00–09:40 ET on 2026-10-02. The guard's stop rule
   stops ONLY paper, verifies owner release and sends a low-priority page.
   Load >3.5 remains WARNING only. Do not change thresholds or use a
   green gate label to hide an UNKNOWN.

   ```bash
   set -euo pipefail
   REPO=/home/trader/project-mai-tai
   GATEWAY_ID_FILE=/home/trader/after-hours/2026-10-01/option-a-gateway-identity-1605-go.txt
   TREATMENT=/home/trader/after-hours/2026-10-02/option-a-treatment
   test "$(TZ=America/New_York date +%F)" = 2026-10-02
   test "$(TZ=America/New_York date +%H%M%S)" -ge 051500
   test "$(TZ=America/New_York date +%H%M%S)" -lt 070000
   test "$(systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-market-data.service)" = "$(sudo cat "$GATEWAY_ID_FILE")"
   sudo systemctl is-active --quiet project-mai-tai-market-data.service
   sudo systemctl is-active --quiet project-mai-tai-option-a-guard@2026-10-02.service
   test "$(systemctl show -p NRestarts --value project-mai-tai-option-a-guard@2026-10-02.service)" = 0
   test "$(systemctl show -p ActiveState --value project-mai-tai-momentum-paper.service)" = inactive
   sudo "$REPO/.venv/bin/python" - "$TREATMENT" <<'PY'
   import json, sys, time
   from pathlib import Path
   from redis import Redis
   from project_mai_tai.events import MarketDataSubscriptionEvent, stream_name
   from project_mai_tai.settings import Settings

   root = Path(sys.argv[1])
   guard = root / "option-a-guard.jsonl"
   if not guard.is_file() or not any(
       (row := json.loads(line)).get("action") == "start"
       and row.get("treatment_date") == "2026-10-02"
       for line in guard.read_text().splitlines()
   ):
       raise SystemExit("UNKNOWN guard startup record missing")
   sampler = root / "option-a-1008.jsonl"
   if not sampler.is_file() or time.time() - sampler.stat().st_mtime > 5:
       raise SystemExit("UNKNOWN 1008 sampler stale")
   rows = [json.loads(line) for line in sampler.read_text().splitlines()[-10:]]
   if len(rows) < 5:
       raise SystemExit("UNKNOWN 1008 sampler has <5 recent rows")
   from datetime import datetime
   stamps = [datetime.fromisoformat(row["sampled_at_utc"].replace("Z", "+00:00")) for row in rows]
   if any(not .5 <= (b - a).total_seconds() <= 1.5 for a, b in zip(stamps, stamps[1:])):
       raise SystemExit("UNKNOWN 1008 sampler not continuous at 1 Hz")
   print("GUARD_ALIVE startup_date=2026-10-02 sampler_rows", len(rows), "last", stamps[-1])
   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   redis = Redis.from_url(settings.redis_url, decode_responses=True)
   owners = {}
   # <=250 compact replace events, <1 MiB measured reply.
   for event_id, fields in redis.xrevrange(
       stream_name(settings.redis_stream_prefix, "market-data-subscriptions"),
       count=settings.redis_market_data_subscription_stream_maxlen,
   ):
       event = MarketDataSubscriptionEvent.model_validate(json.loads(fields["data"]))
       owners.setdefault(event.payload.consumer_name, set(event.payload.symbols))
   required = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab"}
   if set(owners) != required:
       raise SystemExit(f"UNKNOWN 05:15 owner set missing/unexpected: {set(owners)}")
   union = set(settings.market_data_static_symbol_list)
   for symbols in owners.values():
       union.update(symbols)
   key = stream_name(settings.redis_stream_prefix, "heartbeats")
   for _, fields in redis.xrevrange(key, count=25):  # <256 KiB heartbeat reply.
       event = json.loads(fields["data"])
       if event.get("source_service") != "market-data-gateway":
           continue
       from datetime import UTC, datetime
       stamp = datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
       age = (datetime.now(UTC) - stamp).total_seconds()
       if event.get("payload", {}).get("status") != "healthy" or not 0 <= age <= 30.819:
           raise SystemExit(f"gateway heartbeat unhealthy or stale: age_s={age}")
       active = event.get("payload", {}).get("details", {}).get("active_symbols")
       if str(active) != str(len(union)):
           raise SystemExit(f"UNKNOWN gateway union at 05:15 active={active} expected={len(union)}")
       print("gateway heartbeat", event["produced_at"], "age_s", age,
             "active_symbols", active, "expected_union", sorted(union))
       break
   else:
       raise SystemExit("gateway heartbeat missing")
   PY
   FLAT_CHECK=/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py
   if timeout 45s sudo "$REPO/.venv/bin/python" "$FLAT_CHECK"; then
       echo '05:15 flat-check rc=0 RECORDED_NOT_BLOCKING; paper starts no trading service'
   else
       FLAT_RC=$?
       printf '05:15 flat-check rc=%s RECORDED_NOT_BLOCKING; paper starts no trading service\n' "$FLAT_RC"
   fi
   if sudo "$REPO/.venv/bin/python" - <<'PY'; then
   import json
   from redis import Redis
   from project_mai_tai.events import stream_name
   from project_mai_tai.settings import Settings
   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   # One strategy-state event, <1 MiB reply.
   rows = Redis.from_url(settings.redis_url, decode_responses=True).xrevrange(
       stream_name(settings.redis_stream_prefix, "strategy-state-isolated"), count=1)
   event = json.loads(rows[0][1]["data"]) if rows else {}
   print("05:15 ARMED_RECORDED_NOT_BLOCKING", event.get("produced_at"),
         event.get("payload", {}).get("cw_armed_segments", "UNKNOWN"))
   PY
       :
   else
       echo '05:15 ARMED_UNKNOWN_NOT_BLOCKING published state unreadable'
   fi
   sudo systemctl start project-mai-tai-momentum-paper.service
   sudo systemctl is-active --quiet project-mai-tai-momentum-paper.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-momentum-paper.service
   ```

   Pre-ruled 10-02 fallback: if the guard stops paper solely for
   Blind/evidence loss, and the raw gateway log proves **zero new 1008**
   plus treatment evidence proves **no slowdown trigger**, Claude-1 may
   attend an unguarded paper-only restart and record the evidence and time.
   A real 1008 or slowdown keeps paper stopped. This does not change guard
   code or authorize a gateway/live-service restart; unreadable cause is
   UNKNOWN and stays stopped.

## 4. One authorized gateway rollback

The operator's 2026-10-01 GO authorizes one new rollback for this attempt;
the 2026-09-30 attempt's rollback is already spent. Trigger this one if any
of section 2's four content conditions is not proven within **180 seconds**
of restart. Switch temporarily to old gateway SHA
`3389090a7d30bdc88736a53211d968f4c82f0288`, then replay **all four**
**newest retained** raw consumer replaces at rollback time (strategy-engine,
schwab-1m-v2, orb, orb-schwab) byte-for-byte in source-ID order, newest last.
The 16:05 capture is audit evidence, not the replay source. The old
gateway restores only the final event on its own, so a healthy heartbeat
alone is not enough. `ORB_REPLAY_APPROVED=1` is fixed by this GO. A fifth
consumer refuses preflight rather than being silently dropped. After old
gateway proof, return the checkout and editable runtime to `01a64e9b`;
the old Momentum-paper stays STOPPED. Record the five live-unit PIDs/starts
before and after the temporary old checkout; any drift stops further work.

After a renewed proven-clear bot-book check and Redis eviction guard, the
one-time rollback command sequence is:

```bash
set -euo pipefail
REPO=/home/trader/project-mai-tai
OWNER_CAPTURE=/home/trader/after-hours/2026-10-01/option-a-capture-owners.py
TICK_CONTROL=/home/trader/after-hours/2026-10-01/option-a-tick-control.py
FLAT_CHECK=/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py
REDIS_GUARD=/home/trader/after-hours/2026-10-01/option-a-redis-guard.py
REDIS_BASELINE="$(sudo cat /home/trader/after-hours/2026-10-01/option-a-redis-baseline-path-1605-go.txt)"
OLD_SHA=3389090a7d30bdc88736a53211d968f4c82f0288
TARGET_SHA=01a64e9b7552b673e7db6f6e6c787b77b16f6f22
LIVE_IDS_FILE=/home/trader/after-hours/2026-10-01/option-a-live-identities-1605-go.txt
LIVE_UNITS=(project-mai-tai-oms project-mai-tai-strategy project-mai-tai-schwab-1m-v2 project-mai-tai-orb project-mai-tai-orb-schwab)
sudo test -s "$OWNER_CAPTURE" && sudo test -s "$TICK_CONTROL" && sudo test -s "$FLAT_CHECK" && sudo test -s "$REDIS_GUARD" && sudo test -s "$REDIS_BASELINE"
sudo "$REPO/.venv/bin/python" "$REDIS_GUARD" check "$REDIS_BASELINE" || { echo 'UNKNOWN Redis eviction: STOP, PAGE, NO ROLLBACK LOOP'; exit 2; }
test "$(systemctl show -p ActiveState --value project-mai-tai-momentum-paper.service)" = inactive
if timeout 45s sudo "$REPO/.venv/bin/python" "$FLAT_CHECK"; then
    echo 'ROLLBACK_PRECHECK rc=0 bot_books_clear_manual_holdings_recorded'
else
    FLAT_RC=$?
    printf 'ROLLBACK_REFUSED direct-flat rc=%s\n' "$FLAT_RC" >&2
    if test "$FLAT_RC" -eq 1; then exit 1; fi
    exit 2
fi
if sudo "$REPO/.venv/bin/python" - <<'PY'; then
import json
from redis import Redis
from project_mai_tai.events import stream_name
from project_mai_tai.settings import Settings
settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
rows = Redis.from_url(settings.redis_url, decode_responses=True).xrevrange(  # One event, <1 MiB.
    stream_name(settings.redis_stream_prefix, "strategy-state-isolated"), count=1)
event = json.loads(rows[0][1]["data"]) if rows else {}
print("ROLLBACK ARMED_RECORDED_NOT_BLOCKING", event.get("produced_at"),
      event.get("payload", {}).get("cw_armed_segments", "UNKNOWN"))
PY
    :
else
    echo 'ROLLBACK ARMED_UNKNOWN_NOT_BLOCKING published state unreadable'
fi
test "$(for unit in "${LIVE_UNITS[@]}"; do printf '%s\n' "$unit"; systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"; done)" = "$(sudo cat "$LIVE_IDS_FILE")"
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$TARGET_SHA"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
test "$(TZ=America/New_York date +%F)" = 2026-10-01
test "$(TZ=America/New_York date +%H%M%S)" -lt 191500
restore_checkout() {
    sudo -u trader git -C "$REPO" switch --detach "$TARGET_SHA"
    sudo -u trader "$REPO/.venv/bin/python" -m pip install --no-deps --disable-pip-version-check -e "$REPO"
    test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$TARGET_SHA"
    test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
}
sudo -u trader git -C "$REPO" switch --detach "$OLD_SHA"
trap 'restore_checkout || printf "CRITICAL: checkout restoration unproven; page operator\n" >&2' EXIT
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$OLD_SHA"
sudo -u trader "$REPO/.venv/bin/python" -m pip install --no-deps --disable-pip-version-check -e "$REPO"
ROLLBACK_ATTEMPT="$(date -u +%Y%m%dT%H%M%S%N)"
ROLLBACK_OWNER="/home/trader/after-hours/2026-10-01/option-a-rollback-owners-${ROLLBACK_ATTEMPT}.json"
ROLLBACK_CONTROL="/home/trader/after-hours/2026-10-01/option-a-rollback-control-${ROLLBACK_ATTEMPT}.json"
sudo "$REPO/.venv/bin/python" "$OWNER_CAPTURE" "$ROLLBACK_OWNER" 1
sudo "$REPO/.venv/bin/python" "$TICK_CONTROL" "$ROLLBACK_OWNER" "$ROLLBACK_CONTROL"
if timeout 45s sudo "$REPO/.venv/bin/python" "$FLAT_CHECK"; then
    echo 'ROLLBACK_RESTART_CHECK rc=0 bot_books_clear_manual_holdings_recorded'
else
    FLAT_RC=$?
    printf 'ROLLBACK_REFUSED before restart direct-flat rc=%s\n' "$FLAT_RC" >&2
    if test "$FLAT_RC" -eq 1; then exit 1; fi
    exit 2
fi
test "$(for unit in "${LIVE_UNITS[@]}"; do printf '%s\n' "$unit"; systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"; done)" = "$(sudo cat "$LIVE_IDS_FILE")"
test "$(TZ=America/New_York date +%H%M%S)" -lt 191500
ROLLBACK_RESTART_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sudo systemctl restart project-mai-tai-market-data.service
sudo systemctl is-active --quiet project-mai-tai-market-data.service
sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-market-data.service
POST_ROLLBACK_UTC="$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)"
sudo "$REPO/.venv/bin/python" "$REDIS_GUARD" check "$REDIS_BASELINE" || { echo 'UNKNOWN Redis eviction after rollback restart: STOP, PAGE'; exit 2; }
sudo "$REPO/.venv/bin/python" - "$ROLLBACK_RESTART_UTC" "$POST_ROLLBACK_UTC" "$REDIS_BASELINE" <<'PY'
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from redis import Redis
from project_mai_tai.events import stream_name
from project_mai_tai.settings import Settings

settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
redis = Redis.from_url(settings.redis_url, decode_responses=True)
restarted = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
since = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
baseline = json.loads(Path(sys.argv[3]).read_text())
deadline = restarted + timedelta(seconds=180)
while datetime.now(UTC) < deadline:
    # INFO stats <64 KiB; EXISTS returns one integer (<1 KiB).
    if (int(redis.info("stats")["evicted_keys"]) != baseline["evicted_keys"]
            or redis.exists(*baseline["streams"]) != len(baseline["streams"])):
        raise SystemExit("UNKNOWN Redis eviction/stream loss during rollback health; STOP, PAGE")
    for _, fields in redis.xrevrange(stream_name(settings.redis_stream_prefix, "heartbeats"), count=25):  # <256 KiB.
        event = json.loads(fields["data"])
        if event.get("source_service") != "market-data-gateway":
            continue
        stamped = datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
        if stamped > since and event["payload"]["status"] == "healthy" and (datetime.now(UTC) - stamped).total_seconds() <= 30.819:
            print("old gateway healthy before owner replay", event["produced_at"])
            raise SystemExit(0)
    time.sleep(1)
raise SystemExit("old gateway health unproven within 180s; keep paper stopped and page")
PY
ROLLBACK_EVENTS=/home/trader/after-hours/2026-10-01/option-a-rollback-events-1605-go.jsonl
sudo "$REPO/.venv/bin/python" - "$ROLLBACK_OWNER" "$ROLLBACK_EVENTS" <<'PY'
import base64
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from redis import Redis
from project_mai_tai.events import MarketDataSubscriptionEvent
from project_mai_tai.settings import Settings

captured = json.loads(Path(sys.argv[1]).read_text())
required = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab"}
if set(captured["owners"]) != required or captured["orb_replay_approved"] is not True:
    raise SystemExit("UNKNOWN rollback capture incomplete")
settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
redis = Redis.from_url(settings.redis_url, decode_responses=False)
newest = {}
for event_id, fields in redis.xrevrange(captured["stream"], count=settings.redis_market_data_subscription_stream_maxlen):  # <=250, <1 MiB.
    raw = fields.get(b"data")
    if raw is None:
        raise SystemExit("UNKNOWN retained event without payload")
    event = MarketDataSubscriptionEvent.model_validate(json.loads(raw))
    consumer = event.payload.consumer_name
    if consumer in newest:
        continue
    if consumer not in required or event.payload.mode != "replace":
        raise SystemExit(f"UNKNOWN unexpected/non-replace consumer {consumer}")
    newest[consumer] = {"source_id": event_id.decode("ascii"), "raw_b64": base64.b64encode(raw).decode("ascii"),
                        "symbols": sorted(set(event.payload.symbols))}
if set(newest) != required:
    raise SystemExit(f"UNKNOWN missing retained consumers {sorted(required - set(newest))}")
ordered = sorted((tuple(map(int, row["source_id"].split("-"))), name, row)
                 for name, row in newest.items())
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as output:
    for _, consumer, row in ordered:
        raw = base64.b64decode(row["raw_b64"], validate=True)
        new_id = redis.xadd(captured["stream"], {b"data": raw},
                            maxlen=settings.redis_market_data_subscription_stream_maxlen,
                            approximate=True)
        item = {"published_at": datetime.now(UTC).isoformat(), "consumer": consumer,
                "source_id": row["source_id"], "raw_b64": row["raw_b64"],
                "symbols": row["symbols"], "new_id": new_id.decode("ascii")}
        output.write(json.dumps(item, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
        print("rollback newest replace", item["consumer"], "source", item["source_id"], "new", item["new_id"])
PY
sudo "$REPO/.venv/bin/python" "$REDIS_GUARD" check "$REDIS_BASELINE" || { echo 'UNKNOWN Redis eviction after owner replay: STOP, PAGE'; exit 2; }
set +e
sudo "$REPO/.venv/bin/python" - "$ROLLBACK_OWNER" "$ROLLBACK_CONTROL" "$ROLLBACK_EVENTS" "$ROLLBACK_RESTART_UTC" "$POST_ROLLBACK_UTC" "$REDIS_BASELINE" <<'PY'
import base64
import json
import math
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from redis import Redis
from project_mai_tai.events import MarketDataSubscriptionEvent, stream_name
from project_mai_tai.settings import Settings

captured = json.loads(Path(sys.argv[1]).read_text())
control = json.loads(Path(sys.argv[2]).read_text())
published = [json.loads(line) for line in Path(sys.argv[3]).read_text().splitlines()]
restarted = datetime.fromisoformat(sys.argv[4].replace("Z", "+00:00"))
post_start = datetime.fromisoformat(sys.argv[5].replace("Z", "+00:00"))
redis_baseline = json.loads(Path(sys.argv[6]).read_text())
baseline_streams = redis_baseline["streams"]
if post_start <= restarted:
    raise SystemExit("UNKNOWN rollback post-start bound invalid")
required_owners = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab"}
if set(captured["owners"]) != required_owners or len(published) != 4:
    raise SystemExit("UNKNOWN rollback owner capture or replay incomplete")
if {row["consumer"] for row in published} != required_owners:
    raise SystemExit("UNKNOWN rollback replay omitted/duplicated consumer")
settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
redis = Redis.from_url(settings.redis_url, decode_responses=True)
market_key = stream_name(settings.redis_stream_prefix, "market-data")
heartbeat_key = stream_name(settings.redis_stream_prefix, "heartbeats")
snapshot_key = stream_name(settings.redis_stream_prefix, "snapshot-batches")
after_publish = datetime.fromisoformat(published[-1]["published_at"])
captured_ids = {name: row["source_id"] for name, row in captured["owners"].items()}
replayed_ids = {row["consumer"]: row["source_id"] for row in published}
control_positive = {name for name, kinds in control["counts"].items() if sum(kinds.values()) > 0}
byte_error = None
try:
    for row in published:
        exact = redis.xrange(captured["stream"], min=row["new_id"], max=row["new_id"], count=1)  # <16 KiB.
        if len(exact) != 1 or exact[0][1]["data"].encode("utf-8") != base64.b64decode(row["raw_b64"], validate=True):
            raise ValueError(f"replayed bytes missing/different: {row['consumer']}")
except Exception as exc:
    byte_error = f"{type(exc).__name__}:{exc}"

def current_owners():
    newest = {}
    for event_id, fields in redis.xrevrange(captured["stream"], count=settings.redis_market_data_subscription_stream_maxlen):  # <=250, <1 MiB.
        event = MarketDataSubscriptionEvent.model_validate(json.loads(fields["data"]))
        name = event.payload.consumer_name
        if name in newest:
            continue
        if name not in required_owners or event.payload.mode != "replace":
            raise ValueError(f"unexpected/non-replace consumer {name}")
        newest[name] = {"source_id": event_id, "symbols": set(event.payload.symbols)}
    if set(newest) != required_owners:
        raise ValueError(f"missing retained consumers {sorted(required_owners - set(newest))}")
    return newest

tick_end = post_start + timedelta(seconds=120)
proof_end = restarted + timedelta(seconds=180)
if datetime.now(UTC) >= proof_end:
    raise SystemExit("UNKNOWN rollback proof started after 180s; paper remains stopped")
start_ms = int(post_start.timestamp() * 1000)
end_ms = int(tick_end.timestamp() * 1000)
post_counts = defaultdict(lambda: {"trade_tick": 0, "quote_tick": 0})
scanned = 0
trimmed = None
tick_error = None
tick_scanned = False
# Snapshot payload is about 6 MiB: strictly one new XRANGE entry per call.
snapshot_cursor = f"{start_ms}-0"
snapshot_ms = []
cadence_error = None
while True:
    now = datetime.now(UTC)
    if (int(redis.info("stats")["evicted_keys"]) != redis_baseline["evicted_keys"]  # <64 KiB.
            or redis.exists(*baseline_streams) != len(baseline_streams)):  # One integer, <1 KiB.
        raise SystemExit("UNKNOWN Redis eviction/stream loss during rollback proof; STOP, PAGE")
    if not cadence_error:
        try:
            rows = redis.xrange(snapshot_key, min=f"({snapshot_cursor}", count=1)  # One batch, ~5.5 MiB.
            if rows:
                snapshot_cursor = rows[0][0]
                batch_ms = int(snapshot_cursor.split("-")[0])
                if batch_ms > start_ms:
                    snapshot_ms.append(batch_ms)
        except Exception as exc:
            cadence_error = f"{type(exc).__name__}:{exc}"
    if not tick_scanned and now >= tick_end:
        tick_scanned = True
        try:
            first = redis.xrange(market_key, count=1)  # One tick, <16 KiB.
            trimmed = not first or int(first[0][0].split("-")[0]) > start_ms
            cursor = f"{start_ms}-0"
            while True:
                rows = redis.xrange(market_key, min=cursor,
                                    max=f"({end_ms + 1}-0", count=100)  # ~32 KiB measured.
                if not rows:
                    break
                for _, fields in rows:
                    event = json.loads(fields["data"])
                    kind = event.get("event_type")
                    symbol = event.get("payload", {}).get("symbol")
                    if (event.get("source_service") == "market-data-gateway"
                            and kind in {"trade_tick", "quote_tick"} and symbol):
                        stamp = datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
                        if max(post_start, after_publish) < stamp <= tick_end:
                            post_counts[symbol][kind] += 1
                    scanned += 1
                cursor = f"({rows[-1][0]}"
        except Exception as exc:
            tick_error = f"{type(exc).__name__}:{exc}"
    current = {}
    expected = set()
    owner_error = heartbeat_error = None
    ids_stable = False
    try:
        current = current_owners()
        expected = set(settings.market_data_static_symbol_list)
        for row in current.values():
            expected.update(row["symbols"])
        ids_stable = {name: row["source_id"] for name, row in current_owners().items()} == {name: row["source_id"] for name, row in current.items()}
    except Exception as exc:
        owner_error = f"{type(exc).__name__}:{exc}"
    produced = age = active = status = None
    try:
        gateway = next((event for _, fields in redis.xrevrange(heartbeat_key, count=25)  # <256 KiB.
                        if (event := json.loads(fields["data"])).get("source_service") == "market-data-gateway"), None)
        produced = datetime.fromisoformat(gateway["produced_at"].replace("Z", "+00:00")) if gateway else None
        age = (now - produced).total_seconds() if produced else None
        active = gateway.get("payload", {}).get("details", {}).get("active_symbols") if gateway else None
        status = gateway.get("payload", {}).get("status") if gateway else None
    except Exception as exc:
        heartbeat_error = f"{type(exc).__name__}:{exc}"
    heartbeat_ok = (not owner_error and not heartbeat_error and produced is not None
                    and max(post_start, after_publish) < produced <= now and 0 <= age <= 30.819
                    and status == "healthy" and str(active) == str(len(expected)))
    intervals = [(b - a) / 1000 for a, b in zip(snapshot_ms, snapshot_ms[1:])]
    p95 = sorted(intervals)[math.ceil(.95 * len(intervals)) - 1] if intervals else None
    cadence_ok = cadence_error is None and len(intervals) >= 10 and p95 <= 10
    required_ticks = control_positive & expected
    excused = {name: ("no_pre_restart_tick" if name in expected else "no_longer_owned")
               for name in (set(control["counts"]) | expected) - required_ticks}
    missing_ticks = sorted(name for name in required_ticks if not sum(post_counts[name].values()))
    ticks_ok = tick_scanned and tick_error is None and not trimmed and not missing_ticks
    replay_ok = byte_error is None and owner_error is None and ids_stable
    if (heartbeat_ok and ticks_ok and cadence_ok and replay_ok) or now >= proof_end:
        break
    time.sleep(1)
in_time = datetime.now(UTC) <= proof_end
current_ids = {name: row["source_id"] for name, row in current.items()}
print("owner source IDs captured", captured_ids, "replay_sources", replayed_ids, "current", current_ids)
print(f"content(1) {'PASS' if heartbeat_ok and in_time else 'NOT_PROVEN'} heartbeat={produced} status={status} age_s={age} active={active} expected={len(expected)} error={heartbeat_error or owner_error}")
print(f"content(2) {'PASS' if ticks_ok and in_time else 'NOT_PROVEN'} control={control['start_utc']}..{control['end_utc']} post={post_start.isoformat()}..{tick_end.isoformat()} scanned={scanned} trimmed={trimmed} required={sorted(required_ticks)} counts={dict(post_counts)} excused={excused} missing={missing_ticks} error={tick_error}")
print(f"content(3) {'PASS' if cadence_ok and in_time else 'NOT_PROVEN'} intervals={len(intervals)} p95_s={p95} max_s={max(intervals) if intervals else None} error={cadence_error}")
print(f"replay_bytes {'PASS' if replay_ok and in_time else 'NOT_PROVEN'} ids_stable={ids_stable} source_ids={replayed_ids} new_ids={[row['new_id'] for row in published]} error={byte_error or owner_error}")
if not (heartbeat_ok and ticks_ok and cadence_ok and replay_ok and in_time):
    raise SystemExit("rollback content proof UNKNOWN; paper stays stopped; page; no second rollback")
PY
ROLLBACK_PROOF_RC=$?
set -e
sudo "$REPO/.venv/bin/python" "$REDIS_GUARD" check "$REDIS_BASELINE" || { echo 'UNKNOWN Redis eviction/stream loss after rollback proof: STOP, PAGE'; exit 2; }
if test "$ROLLBACK_PROOF_RC" -ne 0; then
    echo 'UNKNOWN rollback proof; STOP, PAGE, NO SECOND ROLLBACK' >&2
    exit 2
fi
# Restore the service checkout after the old gateway proof; do NOT restart
# OMS, strategy, v2, ORB or ORB Schwab while old code is checked out.
restore_checkout
trap - EXIT
sudo -u trader "$REPO/.venv/bin/python" -c 'import project_mai_tai, pathlib; print(pathlib.Path(project_mai_tai.__file__).resolve())'
test "$(for unit in "${LIVE_UNITS[@]}"; do printf '%s\n' "$unit"; systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"; done)" = "$(sudo cat "$LIVE_IDS_FILE")"
for unit in "${LIVE_UNITS[@]}"; do systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"; done
```

The four printed **new stream IDs** and the post-restart gateway PID go into
the fleet journal. The old gateway code processes each `replace` into its
`_desired_symbols_by_consumer` map, but INFO log lines are not a required
proof: the gateway entrypoint does not configure INFO logging. Instead require
the exact newest replayed payload bytes/IDs and content conditions (1)–(3),
with each measured value printed. The count alone is not enough. If any
payload or ID is unreadable, a symbol that ticked in the pre-restart control
and remains owned has no trade/quote in the post-restart 120-second window,
the heartbeat or cadence is not proven, classify rollback **UNKNOWN**, page
the operator, stop further changes and keep old paper stopped. Do not retry
the rollback or restart another service. This GO fixes
`ORB_REPLAY_APPROVED=1`. The ORB live flag was already enabled in Mode B;
the rollback therefore replays ORB and ORB Schwab owners before it is called
restored. Independent command review of this refresh remains required before
the 16:05 ET restart. No paper start follows a rollback.

## 5. Journal and decision

Journal the operator GO, independent plan review, preflight denominators and
raw paths, all four retained owner source IDs/sets, service identities and
unchanged five live-unit tuples, exact checkout SHA, gateway content proof
values, rollback event IDs (if used), guard unit/hash, rotation device/inode
and sampler offsets/coverage, next-day paper start proof or refusal, and
preopen diff/hash if separately re-pinned. Keep evidence under new O_EXCL
paths in `/home/trader/after-hours/2026-10-01/` or the treatment-date
directory; never overwrite the earlier morning-go owner artifact. Report
the result
as REAL FAILURE, EXPECTED BY DESIGN, or UNKNOWN after checking the relevant
code/design, not by repeating a tool's red line. The first-session result is
`STOPPED`, `OBSERVED`, or `UNKNOWN`, not a Momentum P&L or live-trading verdict.
