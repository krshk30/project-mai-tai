# Combined 2026-09-30 install plan: FLAGGATE, restart evidence, Option A

Status: **BLOCKED PLAN; no production write or restart authorized.** The
operator chose an automatic first-session stop guard on 2026-09-30. The
reviewed main SHA `1d70172609fe0f397667101792d4ed6a2e94f876` contains
#1070, #1073 and #1072, but its 1008 sampler only exits with rc 3; it does
not stop the paper service or page. The guard and its two systemd units are
under build/review on this branch, not installed or independently pinned.
They must merge first, so this is **not yet the final exact-SHA GO candidate**.
After that merge, refresh this SHA, re-review the whole command list, then
request one operator GO. If main moves, the source differs, or the reviewed
baseline/stop procedure is not approved, stop. Do not include
the unconfirmed ORB Schwab MACD wait-card, WBREAD1, or a watch reinstall.

## 1. Read-only preflight and hard stops

1. Confirm the exact SHA and reviewed PR heads/pins; verify the box checkout is
   clean, no other install is in progress, all current service identities and
   `NRestarts` are recorded, and Redis, gateway, v2, scanner/strategy, OMS and
   broker connections are healthy. A Git merge is not an install.
2. Obtain fresh broker reads proving **both live accounts flat**, zero open
   managed rows on `live:schwab_1m_v2` and `live:orb`, and zero armed segments.
   Repeat immediately before each service restart. A failed, stale, ambiguous,
   or unavailable read blocks the restart; a database zero alone is not broker
   flatness. Do not override a gate or restart while a position is held.
3. Before touching the shared gateway, preserve its current subscription
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

   ```bash
   REPO=/home/trader/project-mai-tai
   OWNER_FILE=/home/trader/after-hours/2026-09-30/option-a-preflight-owners.json
   ORB_REPLAY_APPROVED=0  # Change only if the exact-SHA operator GO explicitly approves ORB replay.
   sudo -u trader "$REPO/.venv/bin/python" - "$OWNER_FILE" "$ORB_REPLAY_APPROVED" <<'PY'
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
4. Claude-1 approved the accepted-with-gaps inactive control after
   recomputing load, snapshot cadence, heartbeats, LGHL lag and OMS refusals.
   Claude-1 **did not recompute** VBIO/TGE lag rows or the OMS eligible-intent
   denominator. Preserve those caveats. Independently review the automatic
   treatment guard and its thresholds before a GO; if a required treatment
   signal cannot be observed, stop the whole install before the first write.
5. Capture hashes/backups of `/home/trader/preopen.sh`, the isolated files in
   `/home/trader/restart_evidence/`, the current checkout and service units;
   capture the existing Sunday pre-restart snapshot and the 09-29 fleet
   journal. No flag/env change, database migration, unrelated service
   restart, Momentum replay, or live Momentum order route is in scope.

## 2. One scoped install under that GO

1. Command review for the scoped service change follows. These are **not yet
   executable**: the first `test` rejects the placeholder until #1074 is
   independently pinned/merged and the final exact SHA is placed in the
   operator GO. Run only after section 1's fresh flatness, zero-row/arm,
   retained-owner and identity proofs, and recheck flatness immediately
   before the two service operations. The preflight owner artifact must name
   the latest retained raw `replace` event and symbol set for scanner, v2 and
   any other active consumer; absent/truncated history is a hard stop. The
   second Python block below is the post-restart gate, not a reason to skip
   the separate subscription-stream replay proof.

   ```bash
   set -euo pipefail
   REPO=/home/trader/project-mai-tai
   TARGET_SHA=PENDING_GUARD_MERGE_SHA
   OWNER_FILE=/home/trader/after-hours/2026-09-30/option-a-preflight-owners.json
   test "$TARGET_SHA" != PENDING_GUARD_MERGE_SHA
   test -s "$OWNER_FILE"
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
   sudo -u trader "$REPO/.venv/bin/python" - "$OWNER_FILE" <<'PY'
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
   sudo -u trader "$REPO/.venv/bin/python" - "$OWNER_FILE" "$RESTART_UTC" <<'PY'
   import json
   import math
   import sys
   import time
   from datetime import UTC, datetime
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
   deadline = time.monotonic() + 180
   while time.monotonic() < deadline:
       owners = redis.hgetall(stream_name(settings.redis_stream_prefix, "market-data-subscription-owners"))
       if owners.get("_migration_complete") != "1":
           time.sleep(1)
           continue
       expected_consumers = tuple(saved["owners"])
       if set(expected_consumers) != {"strategy-engine", "schwab-1m-v2", "orb"}:
           raise SystemExit(f"unexpected preserved consumer set: {expected_consumers}")
       for consumer in expected_consumers:
           if consumer not in owners or set(json.loads(owners[consumer])) != set(saved["owners"][consumer]["symbols"]):
               break
       else:
           allowed = {"static", "momentum-paper", *expected_consumers}
           unexpected = [name for name in owners
                         if not name.startswith("_") and name not in allowed]
           if unexpected or json.loads(owners.get("momentum-paper", "[]")):
               time.sleep(1)
               continue
           union = set(settings.market_data_static_symbol_list)
           for consumer, payload in owners.items():
               if not consumer.startswith("_"):
                   union.update(json.loads(payload))
           gateway = next((json.loads(fields["data"]) for _, fields in redis.xrevrange(heartbeat_key, count=25)
                           if json.loads(fields["data"]).get("source_service") == "market-data-gateway"), None)
           batches = sorted({datetime.fromisoformat(json.loads(fields["data"])["produced_at"].replace("Z", "+00:00"))
                             for _, fields in redis.xrevrange(snapshot_key, count=180)
                             if json.loads(fields["data"]).get("event_type") == "snapshot_batch"})
           recent = [stamp for stamp in batches if stamp > restarted]
           intervals = [(b - a).total_seconds() for a, b in zip(recent, recent[1:])]
           if gateway is not None:
               stamp = datetime.fromisoformat(gateway["produced_at"].replace("Z", "+00:00"))
               healthy = (stamp > restarted and gateway["payload"]["status"] == "healthy"
                          and (datetime.now(UTC) - stamp).total_seconds() <= 30.819
                          and int(gateway["payload"]["details"]["active_symbols"]) == len(union))
               if healthy and len(intervals) >= 20 and max(intervals) <= 15 and sorted(intervals)[math.ceil(.95 * len(intervals)) - 1] <= 10:
                   print("gateway owners and cadence restored", sorted(union), gateway["produced_at"])
                   break
       time.sleep(1)
   else:
       raise SystemExit("gateway owner/heartbeat/snapshot restoration not proven within 180s; use the one rollback")
   PY
   sudo systemctl start project-mai-tai-momentum-paper.service
   sudo systemctl is-active --quiet project-mai-tai-momentum-paper.service
   sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-momentum-paper.service
   ```

   The editable refresh may update the project distribution metadata and
   console entrypoints in this existing `.venv`, and may use a temporary
   isolated build environment. `--no-deps` does **not** update runtime
   dependencies; the unchanged `pyproject.toml` is checked against the old
   SHA. It does not install units/env, upgrade pip, run migrations or restart
   another service. If pip changes the tracked tree or fails, stop before
   restarting the gateway. Do **not** use `deploy_main.sh`,
   `deploy_service.sh market-data`, or `08_install_runtime.sh`: they touch
   companion/unrelated units. A fresh healthy heartbeat and 5-second nominal
   snapshot cadence must also be observed; the block above's heartbeat is
   not a substitute for the sustained cadence check. No ORB/OMS/v2/strategy
   process may change identity.
2. Stage isolated `/home/trader/restart_evidence/` copies from this exact SHA:
   `ops/health/expected_flags.json`, `expected_flags_check.py`,
   `preopen_restart_evidence.sh` (#1070), and
   `v2_restart_evidence.py` (#1073). Compare each installed SHA-256 against
   its Git blob. Use these isolated copies in the wrapper rather than
   assuming the updated checkout's ops files are installed automatically.
3. Write a structured install record bound to the existing snapshot's exact
   `captured_at_utc`, with a source fleet journal and **every** unit monitored
   by the restart gate classified as `restarted`, `newly_installed`, or
   `deliberately_untouched`. Verify evidence for `control`, `market-capture`,
   `reconciler`, and `tv-alerts`; never infer untouched from silence. Account
   for the 09-29 OMS/strategy/v2/ORB restarts and new `orb-schwab`, plus this
   gateway restart, against the snapshot. The paper service is journaled
   separately if it is not in the gate's monitored service list. A missing or
   conflicting classification is UNKNOWN and blocks readiness. The reviewed
   restart reporter requires the prior v2 restart declaration; do not create
   a fictitious v2 restart tonight merely to make the report pass.
4. Back up `/home/trader/preopen.sh`; update it **once** for 2026-10-01, the
   final checkout SHA and freshly verified OMS/strategy/v2 PIDs/start times.
   Point its restart-evidence call at the isolated checker with the structured
   `--install-record`, the verified `--restarted`/`--new-service` declarations
   (including `orb-schwab`), and the existing required flags. Source the
   isolated router and add the reviewed
   `preopen_check_expected_flags "$REPO/.venv/bin/python"` call with the
   isolated checker/catalog before the final verdict. Preserve other checks.
   Log the exact before/after diff, backup path, `bash -n`, and new SHA-256.
5. Run the revised gate read-only as `trader` (or root if permissions require)
   against actual running processes. Require exactly one consistent final call
   from each checker: FLAGGATE `PASS` with the full catalog checked, and
   restart evidence `PASS` or a genuine `EXPECTED BY DESIGN` N/A. A mismatch
   is REAL FAILURE; unreadable evidence or a return-code/final-call mismatch
   is UNKNOWN. Neither is an install success. Arrange a one-shot read-only
   06:20 ET 2026-10-01 gate with the verified script hash; no service restart
   may be used to force green.

## 3. Post-restart proof and first-session stop coverage

1. Record old/new PIDs, exact start times, `NRestarts`, box checkout SHA,
   service logs, `/proc` flags and the unchanged PIDs of all other units.
   Attribute the final SHA only to the two newly started processes; an
   untouched process does not acquire new code merely because checkout moved.
   Require the gateway healthy, normal snapshot/heartbeat cadence, restored
   scanner and v2 owner sets, and a union containing every symbol still owned
   by either. Require Momentum owns at most 16 candidates, opens **no** second
   Massive socket, has no broker route, and receives condition-provenance trade
   ticks. No observed paper intent may be described as a live fill.
2. The gateway log rotates by `copytruncate` at about **20:00:06 ET**. A
   truncation makes the 1008 byte-offset sampler UNKNOWN. Start the reviewed
   one-second 1008 sampler and treatment samplers **after both 20:00:06 ET and
   the gateway restart**, and verify this day's rotation has actually
   completed before fixing the initial offset. Record the sampler PID, start
   time, treatment date, log device/inode, initial byte offset, raw JSONL path
   and expected sample count. Confirm them still running at **06:30 ET** before
   the first full 07:00-09:40 ET treatment window. Late start, unreadable log, rotation,
   truncation, or missing samples is UNKNOWN and blocks a healthy treatment
   verdict; it is never reported as zero 1008s.
3. The proposed automatic owner is
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
   ROTATION_EVIDENCE=/home/trader/after-hours/2026-09-30/option-a-gateway-rotation.txt
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
4. Apply the independently reviewed
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

## 4. Rollback is not yet preauthorized

The proposed, still GO-dependent rollback targets only
`3389090a7d30bdc88736a53211d968f4c82f0288` and only once. Trigger it
if the gateway is not healthy within **180 seconds** of its restart, either
scanner/v2 owner set is not restored, or 20 new snapshot intervals plus a
fresh healthy heartbeat do not meet section 2.1's cadence proof within that
bound. Stop paper first and leave **old Momentum paper STOPPED**: its old
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
OWNER_FILE=/home/trader/after-hours/2026-09-30/option-a-preflight-owners.json
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
sudo -u trader "$REPO/.venv/bin/python" - "$ROLLBACK_RESTART_UTC" <<'PY'
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
ROLLBACK_LOG_OFFSET="$(stat -c%s /var/log/project-mai-tai/market-data.log)"
ROLLBACK_LOG_ID="$(stat -c '%d:%i' /var/log/project-mai-tai/market-data.log)"
ROLLBACK_EVENTS=/home/trader/after-hours/2026-09-30/option-a-rollback-events.jsonl
sudo -u trader "$REPO/.venv/bin/python" - "$OWNER_FILE" "$ROLLBACK_EVENTS" <<'PY'
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
sudo -u trader "$REPO/.venv/bin/python" - "$OWNER_FILE" "$ROLLBACK_EVENTS" "$ROLLBACK_LOG_OFFSET" "$ROLLBACK_LOG_ID" <<'PY'
import base64
import json
import math
import re
import sys
import time
from datetime import UTC, datetime
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
log_path = Path("/var/log/project-mai-tai/market-data.log")
offset, identity = int(sys.argv[3]), sys.argv[4]
settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
redis = Redis.from_url(settings.redis_url, decode_responses=False)
retained = dict(redis.xrevrange(saved["stream"], count=settings.redis_market_data_subscription_stream_maxlen))
for row in published:
    actual = retained[row["new_id"].encode("ascii")][b"data"]
    original = base64.b64decode(saved["owners"][row["consumer"]]["raw_b64"], validate=True)
    assert actual == original, (row["consumer"], "replayed bytes differ")
expected = set(settings.market_data_static_symbol_list)
for consumer in replay_names:
    expected.update(saved["owners"][consumer]["symbols"])
after_publish = datetime.fromisoformat(published[-1]["published_at"])
deadline = time.monotonic() + 180
pattern = re.compile(r"market-data subscriptions updated by (\S+) -> (\d+) symbols")
while time.monotonic() < deadline:
    stat = log_path.stat()
    assert f"{stat.st_dev}:{stat.st_ino}" == identity and stat.st_size >= offset, "gateway log rotated/truncated"
    with log_path.open("rb") as log:
        log.seek(offset)
        text = log.read().decode("utf-8", errors="replace")
    assert "failed to apply market-data subscription event" not in text
    updates = [pattern.search(line).groups() for line in text.splitlines() if pattern.search(line)]
    assert len(updates) <= len(published), ("unexpected consumer update", updates)
    assert [name for name, _ in updates] == [row["consumer"] for row in published[:len(updates)]], updates
    if len(updates) == len(published) and int(updates[-1][1]) == len(expected):
        key = stream_name(settings.redis_stream_prefix, "heartbeats")
        heartbeat = next((json.loads(fields[b"data"]) for _, fields in redis.xrevrange(key, count=25)
                          if json.loads(fields[b"data"]).get("source_service") == "market-data-gateway"), None)
        if heartbeat:
            stamped = datetime.fromisoformat(heartbeat["produced_at"].replace("Z", "+00:00"))
            if (stamped > after_publish and heartbeat["payload"]["status"] == "healthy"
                    and (datetime.now(UTC) - stamped).total_seconds() <= 30.819
                    and int(heartbeat["payload"]["details"]["active_symbols"]) == len(expected)):
                batch_key = stream_name(settings.redis_stream_prefix, "snapshot-batches")
                stamps = sorted({datetime.fromisoformat(json.loads(fields[b"data"])["produced_at"].replace("Z", "+00:00"))
                                 for _, fields in redis.xrevrange(batch_key, count=180)
                                 if json.loads(fields[b"data"]).get("event_type") == "snapshot_batch"})
                recent = [stamp for stamp in stamps if stamp > after_publish]
                intervals = [(b - a).total_seconds() for a, b in zip(recent, recent[1:])]
                if len(intervals) >= 20 and max(intervals) <= 15 and sorted(intervals)[math.ceil(.95 * len(intervals)) - 1] <= 10:
                    print("old gateway restored union by code/log proof", sorted(expected),
                          [row["new_id"] for row in published], heartbeat["produced_at"])
                    raise SystemExit(0)
    time.sleep(1)
raise SystemExit("rollback union/cadence unproven; keep paper stopped and page; no second rollback")
PY
```

The two or three printed **new stream IDs** and the post-restart gateway PID go into
the fleet journal. Do not claim the union from the count alone. The old
gateway code deterministically processes each `replace` into its
`_desired_symbols_by_consumer` map; require two post-offset
`market-data subscriptions updated by <consumer> -> <count> symbols` log
lines for every published consumer in order, no intervening failed-apply line or unexpected
consumer update, and a newer healthy heartbeat whose `active_symbols` is
the count of the preserved static/scanner/v2 union. This is a code-path and
log proof, not direct inspection of old gateway memory. If either event is
not visibly applied, any raw payload or new ID is unreadable, the log
rotates, or the heartbeat count differs, classify rollback **UNKNOWN**, page
the operator, stop further changes and keep old paper stopped. Do not retry
the rollback or restart another service. The third-owner decision and
independent command review remain required before this is GO-ready.

## 5. Journal and decision

Journal the operator GO, preflight denominators and raw paths, retained-owner
proof, every file blob/installed hash and backup, the structured install record,
service identities, preopen diff/hash, gate return codes **with their actual
Final calls**, sampler offsets/coverage, and any hard stop. Report the result
as REAL FAILURE, EXPECTED BY DESIGN, or UNKNOWN after checking the relevant
code/design, not by repeating a tool's red line. No automatic rollback or
additional restart is authorized by this blocked plan. The first-session result is
`STOPPED`, `OBSERVED`, or `UNKNOWN`, not a Momentum P&L or live-trading verdict.
