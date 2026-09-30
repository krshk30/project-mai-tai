# Combined 2026-09-30 install plan: FLAGGATE, restart evidence, Option A, ORB live

Status: **FINAL PLAN FOR INDEPENDENT REVIEW; NOT OPERATOR GO.** The exact
candidate is `01a64e9b7552b673e7db6f6e6c787b77b16f6f22`, the rebase-merge
of pinned #1076 after pinned #1075. It contains #1070 FLAGGATE, #1073 restart
evidence, #1072 Momentum Option A, #1074 automatic treatment guard, #1075
ORB Schwab late-bar handling, and #1076's paired ORB flag expectations.
No further main merge, substitute SHA, service restart, flag edit, or box
checkout advance is authorized by this document. Independent review of this
plan precedes one operator GO naming this full SHA and every listed restart.
If main moves or any required evidence differs, refresh the plan and ask for
a new GO. WBREAD1, the watch reinstall, and all unrelated live rules stay out.

The next GO must select `INSTALL_MODE=A` (the full Option A gateway, paper,
and ORB install) or `INSTALL_MODE=B` (the ORB phase and isolated gate files
only). The 2026-09-30 18:41 ET attempt already consumed its one gateway
rollback. Mode A therefore needs a newly authorized single rollback and new
attempt-specific evidence paths; mode B never restarts the gateway, starts
paper, or runs the Option A samplers. The old momentum-paper service remains
STOPPED unless the operator separately rules otherwise. Mode B can stage the
FLAGGATE files but cannot produce a green full FLAGGATE result at this SHA:
its catalog expects `momentum_paper_enabled=true` from a running paper PID.
That row is UNKNOWN while paper is stopped; do not waive it, claim readiness,
or schedule a green morning gate without a separately reviewed resolution.

## 1. Read-only preflight and hard stops

1. Confirm the exact SHA and reviewed PR heads/pins; verify the box checkout is
   clean, no other install is in progress, all current service identities and
   `NRestarts` are recorded, and Redis, gateway, v2, scanner/strategy, OMS and
   broker connections are healthy. Confirm the installed watch and both cron
   SHA guards remain untouched. A Git merge is not an install.
2. Obtain fresh broker reads proving **both live accounts flat**, zero open
   managed rows on `live:schwab_1m_v2` and `live:orb`, and zero armed segments.
   Repeat immediately before **each** gateway, OMS, strategy, orb-schwab and
   momentum-paper restart/start. Record the time, source, account and zero
   denominator for every read. A failed, stale, ambiguous, or unavailable read
   blocks that restart; a database zero alone is not broker flatness. Run the
   OMS restart fence and live preflight before OMS, and re-run them if the
   state changes. Do not override a gate or restart while a position is held.
3. **Mode A only:** Before touching the shared gateway, preserve its current subscription
   stream/owner-hash evidence and prove the retained stream can reconstruct
   **both scanner and v2 consumer owners** (including explicit empty replace
   events where appropriate). Record their event IDs and current union. If
   either owner is absent, the Redis read is unavailable, or a replay would
   remove a scanner/v2-owned symbol, **do not restart the gateway**. Obtain
   fresh owner replaces or a separately reviewed migration plan first.
   The live ORB paper observer is also a debounced gateway consumer; record
   its latest replace, even though `orb-schwab` in `OBSERVE_ONLY` does not
   publish one. The old gateway cannot restore all these owners by itself.
   Capture the raw UTF-8 payload bytes, not a reconstructed event, using the
   read-only Redis command below. The only write is a new local evidence file.
   The existing file must not be silently overwritten. A missing/trimmed
   scanner, v2, or ORB replace refuses the install, including when the last
   known symbol list was empty. Re-run the capture immediately before the
   gateway restart and refuse if any of the three newest source IDs changed.
   The six Python blocks that read the root-only fleet env run as root;
   `OWNER_FILE` and `ROLLBACK_EVENTS` are therefore root-owned, and their later
   readers also run as root. The trader-owned venv's `pip install -e` stays as trader.

   ```bash
   REPO=/home/trader/project-mai-tai
   OWNER_FILE=/home/trader/after-hours/2026-09-30/option-a-preflight-owners-attempt2.json
   ORB_REPLAY_APPROVED=0  # Change only if the renewed exact-SHA GO explicitly approves ORB replay.
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
   required = {"strategy-engine", "schwab-1m-v2", "orb"}
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
           raise SystemExit(f"unexpected fourth subscription consumer: {consumer}")
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
               raise SystemExit(f"unexpected fourth owner hash: {consumer}")
   path = Path(sys.argv[1])
   fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, "w", encoding="utf-8") as output:
       json.dump({"owners": owners, "orb_replay_approved": orb_replay_approved,
                  "stream": stream}, output, sort_keys=True)
       output.write("\n")
   print("preserved owner event IDs", {name: item["source_id"] for name, item in owners.items()})
   PY
   ```
4. **Mode A only:** Claude-1 approved the accepted-with-gaps inactive control after
   recomputing load, snapshot cadence, heartbeats, LGHL lag and OMS refusals.
   Claude-1 **did not recompute** VBIO/TGE lag rows or the OMS eligible-intent
   denominator. Preserve those caveats. #1074's automatic treatment guard
   is pinned and merged; recheck the final source and first-session thresholds
   against the reviewed baseline before GO. If a required treatment signal
   cannot be observed, stop the whole install before the first write.
5. Capture hashes/backups of `/home/trader/preopen.sh`, the isolated files in
   `/home/trader/restart_evidence/`, the current checkout and service units;
   capture the snapshot bound to the current restart-evidence install record
   and the 09-29/09-30 fleet journals. Only the two ORB env keys below may
   change. No database migration,
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

## 2. One scoped install under that GO

1. Command review for the scoped service change follows. These are **not
   executable until the operator's exact-SHA GO**. Run only after section 1's
   fresh flatness, zero-row/arm and identity proofs (plus retained-owner proof
   in mode A), and recheck flatness immediately
   before every stop/start/restart listed here. The preflight owner artifact must name
   the latest retained raw `replace` event and symbol set for scanner, v2 and
   any other active consumer in mode A; absent/truncated history is a hard
   stop. The checkout/runtime refresh is common to A and B; the owner check,
   gateway unit/restart/proof and new paper start are A-only. Mode B keeps
   market-data at its preflight PID and the old paper service stopped.

   ```bash
   set -euo pipefail
   REPO=/home/trader/project-mai-tai
   TARGET_SHA=01a64e9b7552b673e7db6f6e6c787b77b16f6f22
   INSTALL_MODE=A  # Set to B only when the renewed exact-SHA GO chooses B.
   case "$INSTALL_MODE" in A|B) ;; *) exit 2 ;; esac
   OWNER_FILE=/home/trader/after-hours/2026-09-30/option-a-preflight-owners-attempt2.json
   test "$(git -C "$REPO" ls-remote origin refs/heads/main | awk '{print $1}')" = "$TARGET_SHA"
   if test "$INSTALL_MODE" = A; then test -s "$OWNER_FILE"; fi
   test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = 3389090a7d30bdc88736a53211d968f4c82f0288
   test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
   sudo systemctl stop project-mai-tai-momentum-paper.service
   test "$(systemctl show -p ActiveState --value project-mai-tai-momentum-paper.service)" = inactive
   sudo -u trader git -C "$REPO" fetch origin main
   sudo -u trader git -C "$REPO" cat-file -e "$TARGET_SHA^{commit}"
   sudo -u trader git -C "$REPO" switch --detach "$TARGET_SHA"
   test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$TARGET_SHA"
   sudo -u trader "$REPO/.venv/bin/python" -m pip install --no-deps --disable-pip-version-check -e "$REPO"
   sudo -u trader "$REPO/.venv/bin/python" -c 'import project_mai_tai, pathlib; print(pathlib.Path(project_mai_tai.__file__).resolve())'
   test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
   if test "$INSTALL_MODE" = A; then
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
   sudo systemctl start project-mai-tai-momentum-paper.service
   sudo systemctl is-active --quiet project-mai-tai-momentum-paper.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-momentum-paper.service
   fi
   ```

   In mode B, stop after the common checkout/runtime refresh above and go to
   section 2.2; do not install/restart gateway units, run the gateway content
   proof, or start paper. Verify the gateway's original PID/start time again
   after the refresh. In mode A, the content proof prints (1) the latest
   heartbeat/status/union count, (2) per-symbol trade/quote counts within
   120 seconds, (3) snapshot interval count/p95, and (4) the migrated owner
   hash and preserved sets. An unproven condition is UNKNOWN and invokes
   only the newly authorized single rollback. INFO log lines are not proof.

   The editable refresh may update the project distribution metadata and
   console entrypoints in this existing `.venv`, and may use a temporary
   isolated build environment. `--no-deps` does **not** update runtime
   dependencies; the unchanged `pyproject.toml` is checked against the old
   SHA. It does not install units/env, upgrade pip, run migrations or restart
   another service. If pip changes the tracked tree or fails, stop before
   any service restart. Do **not** use `deploy_main.sh`,
   `deploy_service.sh market-data`, or `08_install_runtime.sh`: they touch
   companion/unrelated units. A fresh healthy heartbeat and 5-second nominal
   snapshot cadence must also be observed; the block above's heartbeat is
   not a substitute for the sustained cadence check. No ORB/OMS/v2/strategy
   process may change identity **during this gateway phase**. In mode A, if the
   180-second owner/cadence check fails, use only section 4's one-time
   rollback; do not continue into the ORB flag change.
2. **ORB live phase:** In mode A, begin only after gateway restoration is
   proven. In mode B, begin only after the common checkout/runtime refresh
   and proof that market-data retained its preflight PID/start time; it is
   deliberately still running old gateway code, and paper stays stopped.
   Capture a second set of fresh direct broker-flat reads for both live accounts, zero
   open managed rows and zero armed segments. Run the OMS live preflight and
   `preflight_oms_restart.sh --require-all-account-positions-flat`; an
   unreadable or positive result stops here. Back up the fleet env and log
   its SHA-256. Make exactly two edits in
   `/etc/project-mai-tai/project-mai-tai.env`:
   `MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED=true` and
   `MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED=false`. Reject absent or duplicate
   keys; prove the before/after diff has no other lines. This is the only
   runtime flag change. Both values must be loaded by **new processes**, not
   inferred from the env file. The service's constructor refuses both true.

   Restart order is: stop strategy companion, restart OMS, start strategy,
   then restart orb-schwab. Immediately **before each** restart/start, repeat
   the fresh two-broker flat, zero-managed-row and zero-armed-segment reads;
   the OMS fence is also repeated before OMS itself. If any read becomes
   UNKNOWN or a position appears, stop without proceeding. A failed OMS or
   orb-schwab restart is a partial install; do not force the next step or
   call it complete. The already-running v2, ORB paper, control,
   market-capture and reconciler are **not** restarted.

   ```bash
   set -euo pipefail
   REPO=/home/trader/project-mai-tai
   TARGET_SHA=01a64e9b7552b673e7db6f6e6c787b77b16f6f22
   ENV_FILE=/etc/project-mai-tai/project-mai-tai.env
   test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$TARGET_SHA"
   test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
   # Stop here until fresh direct broker snapshots for BOTH accounts, zero
   # managed rows and zero armed segments are attached to this step's journal.
   sudo -u trader "$REPO/.venv/bin/python" "$REPO/src/project_mai_tai/deploy_preflight.py" --service oms
   sudo "$REPO/ops/preflight/preflight_oms_restart.sh" --require-all-account-positions-flat
   ENV_BACKUP="${ENV_FILE}.before-orb-live-$(date -u +%Y%m%dT%H%M%SZ)"
   sudo cp -p "$ENV_FILE" "$ENV_BACKUP"
   sudo sha256sum "$ENV_FILE" "$ENV_BACKUP"
   sudo python3 - "$ENV_FILE" <<'PY'
   import os
   import stat
   import sys
   import tempfile
   from pathlib import Path

   path = Path(sys.argv[1])
   original = path.read_text()
   replacements = {
       "MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED": "true",
       "MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED": "false",
   }
   lines = original.splitlines(keepends=True)
   for key, desired in replacements.items():
       matches = [i for i, line in enumerate(lines) if line.startswith(f"{key}=")]
       if len(matches) != 1:
           raise SystemExit(f"refuse missing or duplicate {key}: {len(matches)}")
       i = matches[0]
       ending = "\n" if lines[i].endswith("\n") else ""
       lines[i] = f"{key}={desired}{ending}"
   updated = "".join(lines)
   if updated == original:
       raise SystemExit("refuse no-op flag edit; recheck running values")
   st = path.stat()
   fd, temp = tempfile.mkstemp(prefix=".orb-live-", dir=path.parent)
   try:
       with os.fdopen(fd, "w") as handle:
           handle.write(updated)
           handle.flush()
           os.fsync(handle.fileno())
       os.chmod(temp, stat.S_IMODE(st.st_mode))
       os.chown(temp, st.st_uid, st.st_gid)
       os.replace(temp, path)
   finally:
       if os.path.exists(temp):
           os.unlink(temp)
   PY
   sudo diff -u "$ENV_BACKUP" "$ENV_FILE" || test "$?" = 1
   sudo sha256sum "$ENV_BACKUP" "$ENV_FILE"
   # Before EACH operation below, reattach fresh direct broker-flat,
   # zero-row and zero-arm proof; never rely on the earlier snapshot.
   sudo systemctl stop project-mai-tai-strategy.service
   # The all-service preflight was run above; it cannot be rerun while
   # strategy is deliberately stopped. The broker reads and OMS fence can.
   sudo "$REPO/ops/preflight/preflight_oms_restart.sh" --require-all-account-positions-flat
   sudo systemctl restart project-mai-tai-oms.service
   sudo systemctl is-active --quiet project-mai-tai-oms.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-oms.service
   # Fresh direct broker-flat/row/arm proof again before strategy starts.
   sudo "$REPO/ops/preflight/preflight_oms_restart.sh" --require-all-account-positions-flat
   sudo systemctl start project-mai-tai-strategy.service
   sudo systemctl is-active --quiet project-mai-tai-strategy.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-strategy.service
   # Fresh direct broker-flat/row/arm proof again before the ORB producer.
   sudo -u trader "$REPO/.venv/bin/python" "$REPO/src/project_mai_tai/deploy_preflight.py" --service oms
   sudo "$REPO/ops/preflight/preflight_oms_restart.sh" --require-all-account-positions-flat
   sudo systemctl restart project-mai-tai-orb-schwab.service
   sudo systemctl is-active --quiet project-mai-tai-orb-schwab.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-orb-schwab.service
   ```

   The shell preflight is not a substitute for fresh direct broker reads:
   `account_positions` is OMS-maintained. Do not execute this block if the
   two direct snapshots cannot be captured independently at each stop point.
   The old env backup is evidence and a recovery input, not an automatic
   rollback instruction. Check `/proc/<new OMS PID>/environ` and
   `/proc/<new orb-schwab PID>/environ` for live=true; check the orb-schwab
   process for observe=false and boot `LIVE`. Require OMS/strategy/orb-schwab
   zero tracebacks and normal heartbeats. The old v2 PID and RETRY_ONE flag
   stay unchanged.
3. Stage isolated `/home/trader/restart_evidence/` copies from this exact SHA:
   `ops/health/expected_flags.json`, `expected_flags_check.py`,
   `preopen_restart_evidence.sh` (#1070), and
   `v2_restart_evidence.py` (#1073). Compare each installed SHA-256 against
   its Git blob. Use these isolated copies in the wrapper rather than
   assuming the updated checkout's ops files are installed automatically.
4. Write a structured install record bound to the existing snapshot's exact
   `captured_at_utc`, with a source fleet journal and **every** unit monitored
   by the restart gate classified as `restarted`, `newly_installed`, or
   `deliberately_untouched`. Verify evidence for `control`, `market-capture`,
   `reconciler`, and `tv-alerts`; never infer untouched from silence. Account
   for the 09-29 v2/ORB restarts, tonight's OMS/strategy/orb-schwab restarts,
   the gateway restart in mode A or its proven untouched identity in mode B,
   and orb-schwab's original installation, against the
   snapshot. Classify tonight's orb-schwab action as `restarted`, not a
   fictitious new service. The paper service is journaled
   separately if it is not in the gate's monitored service list. A missing or
   conflicting classification is UNKNOWN and blocks readiness. The reviewed
   restart reporter requires the prior v2 restart declaration; do not create
   a fictitious v2 restart tonight merely to make the report pass.
5. Back up `/home/trader/preopen.sh`; update it **once** for 2026-10-01, the
   final checkout SHA and freshly verified OMS/strategy/v2 PIDs/start times.
   Point its restart-evidence call at the isolated checker with the structured
   `--install-record`, the verified `--restarted`/`--new-service` declarations
   (including tonight's `oms`, `strategy`, and `orb-schwab`), and the existing
   required flags. Source the
   isolated router and add the reviewed
   `preopen_check_expected_flags "$REPO/.venv/bin/python"` call with the
   isolated checker/catalog before the final verdict. Preserve other checks.
   Log the exact before/after diff, backup path, `bash -n`, and new SHA-256.
6. Run the revised gate read-only as `trader` (or root if permissions require)
   against actual running processes. Require exactly one consistent final call
   from each checker: FLAGGATE `PASS` with the full catalog checked, and
   restart evidence `PASS` or a genuine `EXPECTED BY DESIGN` N/A. A mismatch
   is REAL FAILURE; unreadable evidence or a return-code/final-call mismatch
   is UNKNOWN. Neither is an install success. In mode B the paper service is
   intentionally stopped, while the unchanged catalog still requires its
   running `momentum_paper_enabled=true` value. FLAGGATE must therefore call
   that row UNKNOWN, not PASS. Mode B can stage the isolated files but cannot
   complete this wrapper/gate step or claim morning readiness without a
   separately reviewed resolution. Arrange a one-shot read-only 06:20 ET
   2026-10-01 gate only after the final wrapper and all checks are verified;
   no service restart may be used to force green.

## 3. Post-restart proof and first-session stop coverage

1. Record old/new PIDs, exact start times, `NRestarts`, box checkout SHA,
   service logs, `/proc` flags and the unchanged PIDs of all other units.
   Attribute the final SHA only to gateway and momentum-paper in mode A, and
   to OMS, strategy and orb-schwab in either mode, after each is confirmed
   restarted; an
   untouched process does not acquire new code merely because checkout moved.
   In mode B record the unchanged old gateway PID/code and stopped paper
   PID=0 explicitly. In mode A require the gateway healthy, normal snapshot/heartbeat cadence, restored
   scanner and v2 owner sets, and a union containing every symbol still owned
   by either. Require Momentum owns at most 16 candidates, opens **no** second
   Massive socket, has no broker route, and receives condition-provenance trade
   ticks. No observed paper intent may be described as a live fill.
   Confirm from `/proc` that the new OMS and orb-schwab processes have
   `MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED=true`, and orb-schwab has
   `MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED=false`. The installed FLAGGATE catalog
   from this SHA must check that same pair (`live=true`, `observe=false`),
   including the OMS secondary owner; no separate hand-edited catalog is
   permitted. Preserve all other ruled flags, including v2 RETRY_ONE=true,
   OMS floor=false, target=5%, hard-stop=8%, EOD transition and 19:55
   flatten=true, polygon_30s=false, and parked massive seed=false. No
   unexercised order path is called PASS on the strength of these flags.
2. **Mode A only:** The gateway log rotates by `copytruncate` at about **20:00:06 ET**. A
   truncation makes the 1008 byte-offset sampler UNKNOWN. Start the reviewed
   one-second 1008 sampler and treatment samplers **after both 20:00:06 ET and
   the gateway restart**, and verify this day's rotation has actually
   completed before fixing the initial offset. Record the sampler PID, start
   time, treatment date, log device/inode, initial byte offset, raw JSONL path
   and expected sample count. Confirm them still running at **06:30 ET** before
   the first full 07:00-09:40 ET treatment window. Late start, unreadable log, rotation,
   truncation, or missing samples is UNKNOWN and blocks a healthy treatment
   verdict; it is never reported as zero 1008s.
3. **Mode A only:** The reviewed automatic stop owner is
   `project-mai-tai-option-a-guard@2026-10-01.service`, running as root under
   systemd's 15-second watchdog with `RefuseManualStop=yes`. The treatment-date
   instance is fixed to `2026-10-01`; manually stopping the guard is refused,
   not a way to bypass its paper stop and release checks. It starts after the
   evening log rotation and
   before 07:00 ET, supervises the separate 1008 collector, records a 1 Hz
   load/coverage audit, and evaluates direct slowdown evidence once per
   minute. Collector rc 3, a direct slowdown trigger, or unreadable/stalled
   required evidence calls only `systemctl stop
   project-mai-tai-momentum-paper.service`; the guard then verifies the
   `momentum-paper` owner hash, union-removal log and post-stop heartbeat,
   publishes one empty replace if needed, and pages low-priority. The
   `project-mai-tai-option-a-guard-failure@2026-10-01.service` OnFailure unit
   stops paper and pages if the guard crashes or misses its watchdog. Its
   exact installed unit text and a stop/release test must be reviewed before
   enabling it. A missing or failed guard is UNKNOWN, never an observed
   treatment session. No paper-only stop may restart the gateway or a live
   trading service.
   Its reviewed unit is started once, only after the **actual**
   `copytruncate` around 20:00:06 ET has been observed and the gateway is
   healthy. Capture the first tuple before 20:00 ET; a same-inode size drop
   after 20:00 is the required rotation proof. The commands journal both
   tuples. If the log was too small to prove a drop, the file was replaced,
   or the comparison is unavailable or inconclusive, leave paper stopped and
   call the treatment UNKNOWN rather than starting a sampler with an unsafe
   offset. The named systemd unit is the stop owner; no unattended human
   response is assumed.

   ```bash
   set -euo pipefail
   GATEWAY_LOG=/var/log/project-mai-tai/market-data.log
   ROTATION_EVIDENCE=/home/trader/after-hours/2026-09-30/option-a-gateway-rotation-attempt2.txt
   test "$(TZ=America/New_York date +%H%M%S)" -lt 200000
   ROT_BEFORE_ID="$(stat -c '%d:%i' "$GATEWAY_LOG")"
   ROT_BEFORE_SIZE="$(stat -c '%s' "$GATEWAY_LOG")"
   printf 'before_utc=%s id=%s size=%s\n' "$(date -u +%FT%TZ)" "$ROT_BEFORE_ID" "$ROT_BEFORE_SIZE" | sudo -u trader tee "$ROTATION_EVIDENCE"
   ROTATION_PROVED=0
   for attempt in $(seq 1 180); do
       ET_NOW="$(TZ=America/New_York date +%H%M%S)"
       ROT_AFTER_ID="$(stat -c '%d:%i' "$GATEWAY_LOG")"
       ROT_AFTER_SIZE="$(stat -c '%s' "$GATEWAY_LOG")"
       if test "$ROT_AFTER_ID" != "$ROT_BEFORE_ID"; then
           break
       fi
       if test "$ET_NOW" -ge 200006 && test "$ROT_AFTER_SIZE" -lt "$ROT_BEFORE_SIZE"; then
           ROTATION_PROVED=1
           break
       fi
       sleep 1
   done
   printf 'after_utc=%s id=%s size=%s proved=%s\n' "$(date -u +%FT%TZ)" "$ROT_AFTER_ID" "$ROT_AFTER_SIZE" "$ROTATION_PROVED" | sudo -u trader tee -a "$ROTATION_EVIDENCE"
   test "$ROTATION_PROVED" = 1
   sudo systemctl is-active --quiet project-mai-tai-market-data.service
   sudo systemctl start project-mai-tai-option-a-guard@2026-10-01.service
   sudo systemctl is-active --quiet project-mai-tai-option-a-guard@2026-10-01.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p WatchdogUSec -p NRestarts project-mai-tai-option-a-guard@2026-10-01.service
   sudo journalctl -u project-mai-tai-option-a-guard@2026-10-01.service --since '2026-10-01 00:00:06 UTC' --no-pager -n 30
   sudo test -s /home/trader/after-hours/2026-10-01/option-a-treatment/option-a-guard.jsonl
   sudo test -s /home/trader/after-hours/2026-10-01/option-a-treatment/option-a-1008.jsonl
   ```

   At 06:30 ET, rerun `systemctl is-active` and inspect the newest timestamps
   in both JSONL files. Any gap greater than 1.5 seconds in the 1008 rows,
   greater than 1.5 seconds in the guard's 1 Hz load rows, a stopped unit,
   or an unverified owner release after a stop is UNKNOWN and blocks an
   `OBSERVED` verdict. The guard unit's `OnFailure` stops only paper and sends
   a low-priority page if the guard crashes or misses its watchdog; even if
   its Python import is broken, its failure unit's first command still stops
   paper. This is a paper safety stop, not a live-service kill switch.
4. **Mode A only:** Apply the independently reviewed
   `FIRST_SESSION_PROTOCOL.md` and
   `INACTIVE_CONTROL_2026-09-30.md` without tuning thresholds after treatment
   begins. Any new **gateway** 1008 stops only the paper service; measured
   trading slowdown uses the pre-registered v2, OMS, heartbeat and snapshot
   rules. One-minute load over 3.5 is a warning, not a stop. Record Momentum
   union additions and historical 30/60-second REST attempts per session.
   If a stop fires, stop only paper, page low-priority, and verify its owner
   hash is `[]`, the post-stop union removes Momentum-only symbols, and a
   fresh gateway heartbeat matches the remaining owners. If release fails,
   publish only the approved empty `momentum-paper` replace and reverify;
   blind evidence remains UNKNOWN and pages. A service-stop page drill (board
   row 45) is **excluded** unless the exact-SHA GO explicitly approves it.

## 4. One gateway rollback for a renewed mode-A GO

Mode B does not restart the gateway and does not use this rollback. The
18:41 ET attempt already used its one rollback; this section requires a NEW
one-time authorization in the next mode-A GO. It targets only
`3389090a7d30bdc88736a53211d968f4c82f0288` and only once. Trigger it
if any of section 2.1's four content conditions is not proven within **180
seconds** of the restart. Stop paper first and leave **old Momentum paper STOPPED**: its old
`T.*` socket can load the gateway. The old gateway restores only the last
subscription event. Therefore its restart must be followed by byte-for-byte
republication of the preserved scanner and v2 `replace` events, in their
original source-ID order, newest last. The live ORB service also has a
debounced consumer. The preflight's single `ORB_REPLAY_APPROVED=0` switch
defaults to refusing a nonempty ORB set. Only an exact-SHA operator GO that
explicitly includes ORB replay may change that line to `1`; that choice is
saved in the owner artifact. With `1`, the rollback republishes **all three**
preserved raw replaces in source-ID order and verifies all three. Any fourth
consumer refuses the install in preflight; none is silently dropped.

After a renewed fresh-flat check, the one-time rollback command sequence is:

```bash
set -euo pipefail
REPO=/home/trader/project-mai-tai
OWNER_FILE=/home/trader/after-hours/2026-09-30/option-a-preflight-owners-attempt2.json
OLD_SHA=3389090a7d30bdc88736a53211d968f4c82f0288
test -s "$OWNER_FILE"
sudo systemctl stop project-mai-tai-momentum-paper.service
test "$(systemctl show -p ActiveState --value project-mai-tai-momentum-paper.service)" = inactive
sudo -u trader git -C "$REPO" switch --detach "$OLD_SHA"
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
ROLLBACK_EVENTS=/home/trader/after-hours/2026-09-30/option-a-rollback-events-attempt2.jsonl
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
assert set(saved["owners"]) == {"strategy-engine", "schwab-1m-v2", "orb"}
replay_names = {"strategy-engine", "schwab-1m-v2"}
if saved["orb_replay_approved"]:
    replay_names.add("orb")
else:
    assert not saved["owners"]["orb"]["symbols"], "nonempty ORB owner needs separate rollback approval"
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
replay_names = {"strategy-engine", "schwab-1m-v2"}
if saved["orb_replay_approved"]:
    replay_names.add("orb")
else:
    assert not saved["owners"]["orb"]["symbols"]
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
```

The two or three printed **new stream IDs** and the post-restart gateway PID go into
the fleet journal. The old gateway code processes each `replace` into its
`_desired_symbols_by_consumer` map, but INFO log lines are not a required
proof: the gateway entrypoint does not configure INFO logging. Instead require
the exact replayed payload bytes/IDs and content conditions (1)–(3), with each
measured value printed. The count alone is not enough. If any payload or ID
is unreadable, a required symbol has no trade/quote in the 120-second window,
the heartbeat or cadence is not proven, classify rollback **UNKNOWN**, page
the operator, stop further changes and keep old paper stopped. Do not retry
the rollback or restart another service. The GO must explicitly set
`ORB_REPLAY_APPROVED=1` if the preserved ORB owner is nonempty, or this plan
refuses before a production write. Independent command review of this final
revision remains required before GO. In mode A the ORB live flag edit comes
**after** gateway restoration proof, so this rollback cannot race a new live
ORB producer. Mode B has no gateway rollback or treatment sampler.

## 5. Journal and decision

Journal the operator GO, preflight denominators and raw paths, retained-owner
proof, every file blob/installed hash and backup, the structured install record,
service identities, preopen diff/hash, gate return codes **with their actual
Final calls**, mode-A sampler offsets/coverage (or mode-B skipped status),
and any hard stop. Report the result
as REAL FAILURE, EXPECTED BY DESIGN, or UNKNOWN after checking the relevant
code/design, not by repeating a tool's red line. Until the operator signs the
exact-SHA GO, no rollback or additional restart is authorized by this
review-only plan. The first-session result is
`STOPPED`, `OBSERVED`, or `UNKNOWN`, not a Momentum P&L or live-trading verdict.
