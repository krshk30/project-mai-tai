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
rollback if any mandatory content proof is unproven, a guard start after
the 20:00:06 ET log rotation, and a conditional paper start at 05:15 ET on
2026-10-02. Independent review of this refreshed plan must finish first.
No restart before 16:05 ET, OMS/strategy/v2/ORB/orb-schwab restart, env/flag
change, WBREAD1, watch install, gate edit, Momentum replay, or protocol
threshold change is authorized here. The old gateway's final restart must
finish before the 20:00 ET rotation. The separate untimestamped-traceback
checker defect is not a reason to alter this live gateway phase.

## 1. Read-only preflight and hard stops

1. Confirm the exact SHA and reviewed PR heads/pins; verify the box checkout is
   clean, no other install is in progress, all current service identities and
   `NRestarts` are recorded, and Redis, gateway, v2, scanner/strategy, OMS and
   broker connections are healthy. Confirm the installed watch and both cron
   SHA guards remain untouched. A Git merge is not an install.
2. Obtain fresh direct broker reads proving **both live accounts flat**, zero open
   managed rows on `live:schwab_1m_v2` and `live:orb`, and zero armed segments.
   Repeat immediately before the gateway restart and any rollback restart.
   Record the time, source, account and zero
   denominator for every read. A failed, stale, ambiguous, or unavailable read
   blocks that restart; a database zero alone is not broker flatness. Run the
   live preflight before the gateway restart, and re-run it if the
   state changes. Do not override a gate or restart while a position is held.
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
   read-only Redis command below. The only write is a new local evidence file.
   The existing file must not be silently overwritten. A missing/trimmed
   scanner, v2, ORB, or ORB Schwab replace refuses the install, including when the last
   known symbol list was empty. Perform this O_EXCL capture at the 16:05
   install preflight, immediately before the section 2 command block;
   refuse if any of the four newest source IDs changes between capture and
   restart. The Python blocks that read the root-only fleet env run as root;
   `OWNER_FILE` and `ROLLBACK_EVENTS` are therefore root-owned, and their later
   readers also run as root. The trader-owned venv's `pip install -e` stays as trader.

   ```bash
   REPO=/home/trader/project-mai-tai
   OWNER_FILE=/home/trader/after-hours/2026-10-01/option-a-preflight-owners-1605-go.json
   ORB_REPLAY_APPROVED=1  # Operator approved all active ORB consumers for rollback replay.
   sudo install -d -m 0750 /home/trader/after-hours/2026-10-01
   sudo "$REPO/.venv/bin/python" - "$OWNER_FILE" "$ORB_REPLAY_APPROVED" <<'PY'
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
   refresh and section 1's fresh flatness, zero-row/arm, unchanged service
   identities and four-owner proof. Recheck flatness immediately before the
   gateway restart. The owner artifact must contain the latest raw `replace`
   event and symbol set for strategy-engine, schwab-1m-v2, orb and orb-schwab;
   absent/truncated history, a fifth consumer, or an ID change is a hard stop.
   Record the initial OMS, strategy, v2, orb and orb-schwab PIDs/start times;
   all five must remain unchanged. Paper is already stopped and stays stopped.

   ```bash
   set -euo pipefail
   REPO=/home/trader/project-mai-tai
   TARGET_SHA=01a64e9b7552b673e7db6f6e6c787b77b16f6f22
   OWNER_FILE=/home/trader/after-hours/2026-10-01/option-a-preflight-owners-1605-go.json
   test "$(TZ=America/New_York date +%F)" = 2026-10-01
   test "$(TZ=America/New_York date +%H%M%S)" -ge 160500
   test "$(TZ=America/New_York date +%H%M%S)" -lt 191500
   test -s "$OWNER_FILE"
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
   sudo "$REPO/.venv/bin/python" - "$OWNER_FILE" <<'PY'
   import json
   import sys
   from pathlib import Path
   from redis import Redis
   from project_mai_tai.events import MarketDataSubscriptionEvent
   from project_mai_tai.settings import Settings

   saved = json.loads(Path(sys.argv[1]).read_text())
   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   redis = Redis.from_url(settings.redis_url, decode_responses=False)
   newest = {}
   for event_id, fields in redis.xrevrange(saved["stream"], count=settings.redis_market_data_subscription_stream_maxlen):
       event = MarketDataSubscriptionEvent.model_validate(json.loads(fields[b"data"]))
       newest.setdefault(event.payload.consumer_name, event_id.decode("ascii"))
   required = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab"}
   assert set(saved["owners"]) == required
   assert saved["orb_replay_approved"] is True
   assert set(newest) == required, ("unexpected subscription consumer", set(newest) - required)
   for consumer, record in saved["owners"].items():
       assert newest.get(consumer) == record["source_id"], (consumer, "source changed after capture")
   print("pre-restart owner source IDs unchanged", newest)
   PY
   sudo install -m 0644 "$REPO/ops/systemd/project-mai-tai-option-a-guard@.service" /etc/systemd/system/project-mai-tai-option-a-guard@.service
   sudo install -m 0644 "$REPO/ops/systemd/project-mai-tai-option-a-guard-failure@.service" /etc/systemd/system/project-mai-tai-option-a-guard-failure@.service
   test "$(sha256sum "$REPO/ops/systemd/project-mai-tai-option-a-guard@.service" | awk '{print $1}')" = "$(sha256sum /etc/systemd/system/project-mai-tai-option-a-guard@.service | awk '{print $1}')"
   test "$(sha256sum "$REPO/ops/systemd/project-mai-tai-option-a-guard-failure@.service" | awk '{print $1}')" = "$(sha256sum /etc/systemd/system/project-mai-tai-option-a-guard-failure@.service | awk '{print $1}')"
   sudo systemd-analyze verify /etc/systemd/system/project-mai-tai-option-a-guard@.service /etc/systemd/system/project-mai-tai-option-a-guard-failure@.service
   sudo systemctl daemon-reload
   LIVE_IDS_FILE=/home/trader/after-hours/2026-10-01/option-a-live-identities-1605-go.txt
   LIVE_UNITS=(project-mai-tai-oms project-mai-tai-strategy project-mai-tai-schwab-1m-v2 project-mai-tai-orb project-mai-tai-orb-schwab)
   test "$(for unit in "${LIVE_UNITS[@]}"; do printf '%s\n' "$unit"; systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"; done)" = "$(sudo cat "$LIVE_IDS_FILE")"
   # Immediately before this restart, repeat fresh direct broker flatness,
   # zero open managed rows and zero armed segments for BOTH accounts.
   test "$(TZ=America/New_York date +%H%M%S)" -lt 191500
   RESTART_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   sudo systemctl restart project-mai-tai-market-data.service
   sudo systemctl is-active --quiet project-mai-tai-market-data.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-market-data.service
   sudo "$REPO/.venv/bin/python" - "$OWNER_FILE" "$RESTART_UTC" <<'PY'
   import json
   import math
   import sys
   import time
   from datetime import UTC, datetime, timedelta
   from pathlib import Path
   from redis import Redis
   from project_mai_tai.events import stream_name
   from project_mai_tai.settings import Settings

   saved = json.loads(Path(sys.argv[1]).read_text())
   restarted = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   redis = Redis.from_url(settings.redis_url, decode_responses=True)
   heartbeat_key = stream_name(settings.redis_stream_prefix, "heartbeats")
   snapshot_key = stream_name(settings.redis_stream_prefix, "snapshot-batches")
   market_key = stream_name(settings.redis_stream_prefix, "market-data")
   owner_key = stream_name(settings.redis_stream_prefix, "market-data-subscription-owners")
   expected_consumers = set(saved["owners"])
   expected = set(settings.market_data_static_symbol_list)
   for record in saved["owners"].values():
       expected.update(record["symbols"])
   tick_end = restarted + timedelta(seconds=120)
   proof_end = restarted + timedelta(seconds=180)
   if datetime.now(UTC) >= proof_end:
       for number in range(1, 5):
           print(f"content({number}) NOT_PROVEN reason=proof_started_after_180s")
       raise SystemExit("new gateway content proof UNKNOWN; use only the renewed one-time rollback")

   while datetime.now(UTC) < tick_end:
       time.sleep(1)
   start_ms = int(restarted.timestamp() * 1000)
   end_ms = int(tick_end.timestamp() * 1000)
   counts = {symbol: {"trade_tick": 0, "quote_tick": 0} for symbol in expected}
   trimmed = None
   scanned = 0
   tick_error = None
   try:
       first = redis.xrange(market_key, count=1)
       trimmed = bool(first and int(first[0][0].split("-")[0]) > start_ms)
       cursor = f"{start_ms}-0"
       while True:
           rows = redis.xrange(market_key, min=cursor, max=f"({end_ms + 1}-0", count=1000)
           if not rows:
               break
           for _, fields in rows:
               event = json.loads(fields["data"])
               kind = event.get("event_type")
               symbol = event.get("payload", {}).get("symbol")
               if (event.get("source_service") == "market-data-gateway"
                       and kind in {"trade_tick", "quote_tick"} and symbol in counts):
                   stamp = datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
                   if restarted < stamp <= tick_end:
                       counts[symbol][kind] += 1
               scanned += 1
           cursor = f"({rows[-1][0]}"
   except Exception as exc:
       tick_error = f"{type(exc).__name__}:{exc}"
   ticks_ok = tick_error is None and all(sum(by_kind.values()) > 0 for by_kind in counts.values())

   while True:
       now = datetime.now(UTC)
       produced = age = active = status = None
       heartbeat_error = None
       try:
           gateway = next((event for _, fields in redis.xrevrange(heartbeat_key, count=25)
                           if (event := json.loads(fields["data"])).get("source_service") == "market-data-gateway"), None)
           produced = datetime.fromisoformat(gateway["produced_at"].replace("Z", "+00:00")) if gateway else None
           age = (now - produced).total_seconds() if produced else None
           active = gateway.get("payload", {}).get("details", {}).get("active_symbols") if gateway else None
           status = gateway.get("payload", {}).get("status") if gateway else None
       except Exception as exc:
           heartbeat_error = f"{type(exc).__name__}:{exc}"
       heartbeat_ok = (heartbeat_error is None and produced is not None and restarted < produced <= now
                       and 0 <= age <= 30.819 and status == "healthy" and str(active) == str(len(expected)))
       intervals = []
       p95 = None
       cadence_error = None
       try:
           batches = sorted({datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
                             for _, fields in redis.xrevrange(snapshot_key, count=180)
                             if (event := json.loads(fields["data"])).get("event_type") == "snapshot_batch"})
           recent = [stamp for stamp in batches if stamp > restarted]
           intervals = [(b - a).total_seconds() for a, b in zip(recent, recent[1:])]
           p95 = sorted(intervals)[math.ceil(.95 * len(intervals)) - 1] if intervals else None
       except Exception as exc:
           cadence_error = f"{type(exc).__name__}:{exc}"
       cadence_ok = cadence_error is None and len(intervals) >= 20 and p95 <= 10
       owners = {}
       actual = {}
       extra = []
       paper = None
       owner_error = None
       try:
           owners = redis.hgetall(owner_key)
           actual = {name: set(json.loads(owners[name])) if name in owners else None
                     for name in expected_consumers}
           extra = sorted(set(owners) - expected_consumers - {"_migration_complete", "_last_applied_id", "momentum-paper", "static"})
           paper = json.loads(owners.get("momentum-paper", "[]"))
       except Exception as exc:
           owner_error = f"{type(exc).__name__}:{exc}"
       owners_ok = (owner_error is None and owners.get("_migration_complete") == "1" and not extra and not paper
                    and all(actual[name] == set(saved["owners"][name]["symbols"])
                            for name in expected_consumers))
       if (heartbeat_ok and ticks_ok and cadence_ok and owners_ok) or now >= proof_end:
           break
       time.sleep(1)
   in_time = datetime.now(UTC) <= proof_end
   missing_ticks = sorted(symbol for symbol, by_kind in counts.items() if not sum(by_kind.values()))
   heartbeat_reasons = ([heartbeat_error] if heartbeat_error else []) + (["no_post_restart_healthy_fresh_heartbeat_or_union_mismatch"] if not heartbeat_ok else [])
   tick_reasons = ([tick_error] if tick_error else []) + ([f"missing_symbols={missing_ticks}"] if missing_ticks else [])
   cadence_reasons = ([cadence_error] if cadence_error else []) + (["fewer_than_20_intervals_or_p95_over_10s"] if not cadence_ok else [])
   if not in_time:
       heartbeat_reasons.append("proof_deadline_exceeded")
       tick_reasons.append("proof_deadline_exceeded")
       cadence_reasons.append("proof_deadline_exceeded")
   print(f"content(1) {'PASS' if heartbeat_ok and in_time else 'NOT_PROVEN'} heartbeat={produced} status={status} age_s={age} active={active} expected={len(expected)} reasons={heartbeat_reasons}")
   print(f"content(2) {'PASS' if ticks_ok and in_time else 'NOT_PROVEN'} window={restarted.isoformat()}..{tick_end.isoformat()} scanned={scanned} trimmed={trimmed} counts={counts} reasons={tick_reasons}")
   print(f"content(3) {'PASS' if cadence_ok and in_time else 'NOT_PROVEN'} intervals={len(intervals)} p95_s={p95} max_s={max(intervals) if intervals else None} reasons={cadence_reasons}")
   expected_owner_sets = {name: saved["owners"][name]["symbols"] for name in expected_consumers}
   owner_mismatch = sorted(name for name in expected_consumers if actual.get(name) != set(expected_owner_sets[name]))
   owner_reasons = ([owner_error] if owner_error else []) + ([f"mismatched_consumers={owner_mismatch}"] if owner_mismatch else [])
   if owners.get("_migration_complete") != "1":
       owner_reasons.append("migration_incomplete")
   if extra or paper:
       owner_reasons.append("unexpected_or_stale_owner")
   if not in_time:
       owner_reasons.append("proof_deadline_exceeded")
   print(f"content(4) {'PASS' if owners_ok and in_time else 'NOT_PROVEN'} migration={owners.get('_migration_complete')} actual={actual} expected={expected_owner_sets} extra={extra} paper={paper} reasons={owner_reasons}")
   if not (heartbeat_ok and ticks_ok and cadence_ok and owners_ok and in_time):
       raise SystemExit("new gateway content proof UNKNOWN; use only the renewed one-time rollback")
   PY
   test "$(for unit in "${LIVE_UNITS[@]}"; do printf '%s\n' "$unit"; systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"; done)" = "$(sudo cat "$LIVE_IDS_FILE")"
   test "$(systemctl show -p ActiveState --value project-mai-tai-momentum-paper.service)" = inactive
   GATEWAY_ID_FILE=/home/trader/after-hours/2026-10-01/option-a-gateway-identity-1605-go.txt
   systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-market-data.service | sudo bash -c 'set -C; cat > "$1"' bash "$GATEWAY_ID_FILE"
   ```

   The content proof prints (1) the latest
   heartbeat/status/union count, (2) per-symbol trade/quote counts within
   120 seconds, (3) snapshot interval count/p95, and (4) the migrated owner
   hash and preserved sets. An unproven condition is UNKNOWN and invokes
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
   sudo test -s /home/trader/after-hours/2026-10-02/option-a-treatment/option-a-guard.jsonl
   sudo test -s /home/trader/after-hours/2026-10-02/option-a-treatment/option-a-1008.jsonl
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
   audit JSONL is not refreshed before 07:00 by the reviewed code; its
   startup record plus current systemd watchdog/journal health is the only
   available pre-07:00 guard-loop proof. The operator's requested "both
   JSONL files fresh" criterion is therefore **not satisfiable as written**.
   Unless the operator explicitly accepts that watchdog substitution, the
   05:15 paper start is a hard stop; do not alter guard code in this plan.
   Recheck
   direct two-broker flatness, zero managed rows and zero armed segments
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
   test "$(systemctl show -p ActiveState --value project-mai-tai-momentum-paper.service)" = inactive
   sudo "$REPO/.venv/bin/python" - "$TREATMENT" <<'PY'
   import json, sys, time
   from pathlib import Path
   from redis import Redis
   from project_mai_tai.events import stream_name
   from project_mai_tai.settings import Settings

   root = Path(sys.argv[1])
   for name in ("option-a-guard.jsonl", "option-a-1008.jsonl"):
       path = root / name
       if not path.is_file() or time.time() - path.stat().st_mtime > 5:
           raise SystemExit(f"stale treatment evidence: {path}")
       last = json.loads(path.read_text().splitlines()[-1])
       print("fresh treatment evidence", path, last)
   settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
   redis = Redis.from_url(settings.redis_url, decode_responses=True)
   key = stream_name(settings.redis_stream_prefix, "heartbeats")
   for _, fields in redis.xrevrange(key, count=25):
       event = json.loads(fields["data"])
       if event.get("source_service") != "market-data-gateway":
           continue
       from datetime import UTC, datetime
       stamp = datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
       age = (datetime.now(UTC) - stamp).total_seconds()
       if event.get("payload", {}).get("status") != "healthy" or not 0 <= age <= 30.819:
           raise SystemExit(f"gateway heartbeat unhealthy or stale: age_s={age}")
       print("gateway heartbeat", event["produced_at"], "age_s", age,
             "active_symbols", event.get("payload", {}).get("details", {}).get("active_symbols"))
       break
   else:
       raise SystemExit("gateway heartbeat missing")
   PY
   # Attach fresh direct two-broker flat, zero-open-row and zero-armed-segment
   # evidence here; if absent, STOP. Then and only then:
   sudo systemctl start project-mai-tai-momentum-paper.service
   sudo systemctl is-active --quiet project-mai-tai-momentum-paper.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-momentum-paper.service
   ```

## 4. One authorized gateway rollback

The operator's 2026-10-01 GO authorizes one new rollback for this attempt;
the 2026-09-30 attempt's rollback is already spent. Trigger this one if any
of section 2's four content conditions is not proven within **180 seconds**
of restart. Switch temporarily to old gateway SHA
`3389090a7d30bdc88736a53211d968f4c82f0288`, then replay **all four**
preserved raw consumer replaces (strategy-engine, schwab-1m-v2, orb,
orb-schwab) byte-for-byte in original source-ID order, newest last. The old
gateway restores only the final event on its own, so a healthy heartbeat
alone is not enough. `ORB_REPLAY_APPROVED=1` is fixed by this GO. A fifth
consumer refuses preflight rather than being silently dropped. After old
gateway proof, return the checkout and editable runtime to `01a64e9b`;
the old Momentum-paper stays STOPPED. Record the five live-unit PIDs/starts
before and after the temporary old checkout; any drift stops further work.

After a renewed fresh-flat check, the one-time rollback command sequence is:

```bash
set -euo pipefail
REPO=/home/trader/project-mai-tai
OWNER_FILE=/home/trader/after-hours/2026-10-01/option-a-preflight-owners-1605-go.json
OLD_SHA=3389090a7d30bdc88736a53211d968f4c82f0288
TARGET_SHA=01a64e9b7552b673e7db6f6e6c787b77b16f6f22
LIVE_IDS_FILE=/home/trader/after-hours/2026-10-01/option-a-live-identities-1605-go.txt
LIVE_UNITS=(project-mai-tai-oms project-mai-tai-strategy project-mai-tai-schwab-1m-v2 project-mai-tai-orb project-mai-tai-orb-schwab)
test -s "$OWNER_FILE"
test "$(systemctl show -p ActiveState --value project-mai-tai-momentum-paper.service)" = inactive
# First attach fresh direct flat reads for both brokers, zero managed rows,
# zero armed segments, and PID/start/NRestarts of OMS, strategy, v2, ORB and ORB Schwab.
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
ROLLBACK_RESTART_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sudo systemctl restart project-mai-tai-market-data.service
sudo systemctl is-active --quiet project-mai-tai-market-data.service
sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-market-data.service
sudo "$REPO/.venv/bin/python" - "$ROLLBACK_RESTART_UTC" <<'PY'
import json
import sys
import time
from datetime import UTC, datetime
from redis import Redis
from project_mai_tai.events import stream_name
from project_mai_tai.settings import Settings

settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
redis = Redis.from_url(settings.redis_url, decode_responses=True)
since = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    for _, fields in redis.xrevrange(stream_name(settings.redis_stream_prefix, "heartbeats"), count=25):
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
sudo "$REPO/.venv/bin/python" - "$OWNER_FILE" "$ROLLBACK_EVENTS" <<'PY'
import base64
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from redis import Redis
from project_mai_tai.settings import Settings

saved = json.loads(Path(sys.argv[1]).read_text())
replay_names = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab"}
assert set(saved["owners"]) == replay_names
assert saved["orb_replay_approved"] is True
settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
redis = Redis.from_url(settings.redis_url, decode_responses=False)
ordered = sorted((tuple(map(int, record["source_id"].split("-"))), consumer, record)
                 for consumer, record in saved["owners"].items()
                 if consumer in replay_names)
assert len(ordered) == len(replay_names)
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as output:
    for _, consumer, record in ordered:
        raw = base64.b64decode(record["raw_b64"], validate=True)
        new_id = redis.xadd(saved["stream"], {b"data": raw},
                            maxlen=settings.redis_market_data_subscription_stream_maxlen,
                            approximate=True)
        item = {"published_at": datetime.now(UTC).isoformat(), "consumer": consumer,
                "original_id": record["source_id"], "new_id": new_id.decode("ascii")}
        output.write(json.dumps(item, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
        print("rollback replace", item)
PY
sudo "$REPO/.venv/bin/python" - "$OWNER_FILE" "$ROLLBACK_EVENTS" "$ROLLBACK_RESTART_UTC" <<'PY'
import base64
import json
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from redis import Redis
from project_mai_tai.events import stream_name
from project_mai_tai.settings import Settings

saved = json.loads(Path(sys.argv[1]).read_text())
published = [json.loads(line) for line in Path(sys.argv[2]).read_text().splitlines()]
replay_names = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab"}
assert set(saved["owners"]) == replay_names
assert saved["orb_replay_approved"] is True
assert len(published) == len(replay_names) and {row["consumer"] for row in published} == replay_names
settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
redis = Redis.from_url(settings.redis_url, decode_responses=True)
restarted = datetime.fromisoformat(sys.argv[3].replace("Z", "+00:00"))
retained = dict(redis.xrevrange(saved["stream"], count=settings.redis_market_data_subscription_stream_maxlen))
for row in published:
    actual = retained[row["new_id"]]["data"].encode("utf-8")
    original = base64.b64decode(saved["owners"][row["consumer"]]["raw_b64"], validate=True)
    assert actual == original, (row["consumer"], "replayed bytes differ")
expected = set(settings.market_data_static_symbol_list)
for consumer in replay_names:
    expected.update(saved["owners"][consumer]["symbols"])
after_publish = datetime.fromisoformat(published[-1]["published_at"])
tick_end = restarted + timedelta(seconds=120)
proof_end = restarted + timedelta(seconds=180)
if datetime.now(UTC) >= proof_end:
    for number in range(1, 4):
        print(f"content({number}) NOT_PROVEN reason=proof_started_after_180s")
    raise SystemExit("rollback content proof UNKNOWN; paper stays stopped; page; no second rollback")
while datetime.now(UTC) < tick_end:
    time.sleep(1)
market_key = stream_name(settings.redis_stream_prefix, "market-data")
start_ms = int(restarted.timestamp() * 1000)
end_ms = int(tick_end.timestamp() * 1000)
counts = {symbol: {"trade_tick": 0, "quote_tick": 0} for symbol in expected}
trimmed = None
scanned = 0
tick_error = None
try:
    first = redis.xrange(market_key, count=1)
    trimmed = bool(first and int(first[0][0].split("-")[0]) > start_ms)
    cursor = f"{start_ms}-0"
    while True:
        rows = redis.xrange(market_key, min=cursor, max=f"({end_ms + 1}-0", count=1000)
        if not rows:
            break
        for _, fields in rows:
            event = json.loads(fields["data"])
            kind = event.get("event_type")
            symbol = event.get("payload", {}).get("symbol")
            if (event.get("source_service") == "market-data-gateway"
                    and kind in {"trade_tick", "quote_tick"} and symbol in counts):
                stamp = datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
                if restarted < stamp <= tick_end:
                    counts[symbol][kind] += 1
            scanned += 1
        cursor = f"({rows[-1][0]}"
except Exception as exc:
    tick_error = f"{type(exc).__name__}:{exc}"
ticks_ok = tick_error is None and all(sum(by_kind.values()) > 0 for by_kind in counts.values())
heartbeat_key = stream_name(settings.redis_stream_prefix, "heartbeats")
snapshot_key = stream_name(settings.redis_stream_prefix, "snapshot-batches")
while True:
    now = datetime.now(UTC)
    produced = age = active = status = None
    heartbeat_error = None
    try:
        gateway = next((event for _, fields in redis.xrevrange(heartbeat_key, count=25)
                        if (event := json.loads(fields["data"])).get("source_service") == "market-data-gateway"), None)
        produced = datetime.fromisoformat(gateway["produced_at"].replace("Z", "+00:00")) if gateway else None
        age = (now - produced).total_seconds() if produced else None
        active = gateway.get("payload", {}).get("details", {}).get("active_symbols") if gateway else None
        status = gateway.get("payload", {}).get("status") if gateway else None
    except Exception as exc:
        heartbeat_error = f"{type(exc).__name__}:{exc}"
    heartbeat_ok = (heartbeat_error is None and produced is not None
                    and max(restarted, after_publish) < produced <= now
                    and 0 <= age <= 30.819 and status == "healthy"
                    and str(active) == str(len(expected)))
    intervals = []
    p95 = None
    cadence_error = None
    try:
        batches = sorted({datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
                          for _, fields in redis.xrevrange(snapshot_key, count=180)
                          if (event := json.loads(fields["data"])).get("event_type") == "snapshot_batch"})
        recent = [stamp for stamp in batches if stamp > restarted]
        intervals = [(b - a).total_seconds() for a, b in zip(recent, recent[1:])]
        p95 = sorted(intervals)[math.ceil(.95 * len(intervals)) - 1] if intervals else None
    except Exception as exc:
        cadence_error = f"{type(exc).__name__}:{exc}"
    cadence_ok = cadence_error is None and len(intervals) >= 20 and p95 <= 10
    if (heartbeat_ok and ticks_ok and cadence_ok) or now >= proof_end:
        break
    time.sleep(1)
in_time = datetime.now(UTC) <= proof_end
missing_ticks = sorted(symbol for symbol, by_kind in counts.items() if not sum(by_kind.values()))
heartbeat_reasons = ([heartbeat_error] if heartbeat_error else []) + (["no_post_replay_healthy_fresh_heartbeat_or_union_mismatch"] if not heartbeat_ok else [])
tick_reasons = ([tick_error] if tick_error else []) + ([f"missing_symbols={missing_ticks}"] if missing_ticks else [])
cadence_reasons = ([cadence_error] if cadence_error else []) + (["fewer_than_20_intervals_or_p95_over_10s"] if not cadence_ok else [])
if not in_time:
    heartbeat_reasons.append("proof_deadline_exceeded")
    tick_reasons.append("proof_deadline_exceeded")
    cadence_reasons.append("proof_deadline_exceeded")
print(f"content(1) {'PASS' if heartbeat_ok and in_time else 'NOT_PROVEN'} heartbeat={produced} status={status} age_s={age} active={active} expected={len(expected)} after_replay={after_publish} reasons={heartbeat_reasons}")
print(f"content(2) {'PASS' if ticks_ok and in_time else 'NOT_PROVEN'} window={restarted.isoformat()}..{tick_end.isoformat()} scanned={scanned} trimmed={trimmed} counts={counts} reasons={tick_reasons}")
print(f"content(3) {'PASS' if cadence_ok and in_time else 'NOT_PROVEN'} intervals={len(intervals)} p95_s={p95} max_s={max(intervals) if intervals else None} reasons={cadence_reasons}")
print(f"replayed bytes and IDs {[row['new_id'] for row in published]}")
if not (heartbeat_ok and ticks_ok and cadence_ok and in_time):
    raise SystemExit("rollback content proof UNKNOWN; paper stays stopped; page; no second rollback")
PY
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
the exact replayed payload bytes/IDs and content conditions (1)–(3), with each
measured value printed. The count alone is not enough. If any payload or ID
is unreadable, a required symbol has no trade/quote in the 120-second window,
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
