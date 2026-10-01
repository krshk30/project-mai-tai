# 2026-10-01 conditional OMS and ORB Schwab install

Status: **plan for review, not an install authorization**. The operator must name
the final full `origin/main` SHA in a new GO. Do not infer that a pinned PR, a
merge, or this plan authorizes a production restart.

## Scope and hard gates

- Eligible changes are #1078, and #1079 only if each has an independent pin on
  its exact head, both Validate checks pass, and it merges by 15:30 ET. Record
  both PR heads, pin evidence, merge commits and the resulting exact main SHA.
  Never include an unpinned PR by advancing to a later main SHA.
- The separately approved Option A gateway phase must be journaled before the
  first OMS-phase restart. Proceed if its new gateway passed all content proofs,
  if it refused **before any restart**, or if its one rollback proved the old
  gateway. Stop on an UNKNOWN outcome (including eviction, unproven rollback,
  or process-identity drift). Do not overlap an active gateway operation or a
  Redis recovery. Record its final gateway PID/start and owner sets.
- Confirm no other deploy. The box advances **only** to the operator-approved
  full SHA and is clean afterward. `origin/main` may be ahead only by paths
  under `docs/`; a non-docs difference refuses the install.
  Save the old SHA, source-file hashes and process identities before advancing.
  Do not run a general deploy script or change a production flag or env value.
- Use the reviewed, hash-verified
  `/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py` helper
  from the gateway journal for fresh direct reads immediately before **each**
  service restart/start. Its rc 0 means bot-flat: zero open managed rows and
  virtual positions on both `live:schwab_1m_v2` and `live:orb`, zero net bot
  fills today by account/symbol, and readable broker positions on both legs.
  A manual broker holding is recorded by symbol/quantity but is not called an
  account-flat position. Record armed segments, but do **not** block on them:
  v2 is not restarted, its 15:45 ET entry-window end must be read from its
  running `/proc`, and this phase does not modify arms. Working bot **entry**
  orders block. Before the first restart, rc 1 or 2 means wait 300 seconds and
  retry inside the authorized window; only a proven rc 0 permits a restart.
  A later unreadable/positive read stops further service operations.

The first restart may not begin until the gateway phase is journaled. Finish
the **last** OMS-phase restart by 19:20 ET; if that is not feasible, do not
start until after 20:10 ET. Never cross the 20:00:06 log rotation with an
in-progress restart. A fresh exact-SHA GO is still required for either window.

## Scoped command sequence

Run as the box operator, **only after** the gateway outcome and exact-SHA GO
are reviewed. Set `APPROVED_SHA` from that GO, not from the current main head.
The five-unit snapshot and every flat-check output belong in the secured fleet
journal. This is adapted from the reviewed 2026-09-30 Mode B scoped path; it
deliberately omits that path's env edits and its all-accounts-flat fence, which
would reject an operator-owned manual holding under today's ruling.

```bash
set -euo pipefail
REPO=/home/trader/project-mai-tai
APPROVED_SHA='<exact-40-hex-SHA-from-new-operator-GO>'
FLAT_CHECK=/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py
JOURNAL=/home/trader/fleet_health/oms-install-20261001.log
UNITS=(project-mai-tai-oms project-mai-tai-strategy project-mai-tai-schwab-1m-v2 project-mai-tai-orb-schwab project-mai-tai-market-data)
[[ "$APPROVED_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$(TZ=America/New_York date +%F)" = 2026-10-01
sudo test -s "$FLAT_CHECK"
sudo sha256sum "$FLAT_CHECK"  # Match the reviewed gateway journal hash.
for unit in "${UNITS[@]}"; do
  sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"
done | sudo tee -a "$JOURNAL"
sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-orb.service
sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-momentum-paper.service
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
sudo -u trader git -C "$REPO" fetch origin main:refs/remotes/origin/main
sudo -u trader git -C "$REPO" cat-file -e "$APPROVED_SHA^{commit}"
sudo -u trader git -C "$REPO" merge-base --is-ancestor "$APPROVED_SHA" origin/main
sudo -u trader git -C "$REPO" diff --name-only "$APPROVED_SHA" origin/main | while IFS= read -r path; do
  case "$path" in docs/*) ;; *) printf 'REFUSE non-doc main diff: %s\n' "$path" >&2; exit 1 ;; esac
done
sudo -u trader git -C "$REPO" switch --detach "$APPROVED_SHA"
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$APPROVED_SHA"
sudo -u trader "$REPO/.venv/bin/python" -m pip install --no-deps --disable-pip-version-check -e "$REPO"
IMPORT_PATH="$(sudo -u trader "$REPO/.venv/bin/python" -c 'import pathlib, project_mai_tai; print(pathlib.Path(project_mai_tai.__file__).resolve())')"
printf 'import_path=%s\n' "$IMPORT_PATH" | sudo tee -a "$JOURNAL"
test "$IMPORT_PATH" = "$REPO/src/project_mai_tai/__init__.py"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
```

Before each command in the next block that stops, starts or restarts a unit,
attach a **new** strict-flat helper output and working-entry-order read to
the journal; do not reuse a prior snapshot. Record armed segments without
making their count an admission test. Verify v2's running 15:45 entry-window
end from its `/proc` before entering this block. Only the **first** flat
check may wait and retry on rc 1 or 2, every 300 seconds while inside the
window. Do not restart until one read is proven rc 0. A later rc 1/2 halts.

```bash
flat_once() {
  local rc=0 output
  output="$(timeout 45s sudo "$REPO/.venv/bin/python" "$FLAT_CHECK" 2>&1)" || rc=$?
  printf '%s\n' "$output" | sudo tee -a "$JOURNAL"
  printf 'strict_flat_rc=%s\n' "$rc" | sudo tee -a "$JOURNAL"
  return "$rc"
}
entry_orders_clear() {
  sudo "$REPO/.venv/bin/python" - <<'PY'
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from project_mai_tai.db.models import BrokerAccount, BrokerOrder
from project_mai_tai.db.session import build_engine
from project_mai_tai.settings import Settings

settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
with Session(build_engine(settings.database_url)) as session:
    rows = session.execute(
        select(BrokerAccount.name, BrokerOrder.id, BrokerOrder.symbol, BrokerOrder.status)
        .join(BrokerAccount, BrokerOrder.broker_account_id == BrokerAccount.id)
        .where(BrokerAccount.name.in_(("live:schwab_1m_v2", "live:orb")))
        .where(func.lower(BrokerOrder.side) == "buy")
        .where(func.lower(BrokerOrder.status).in_(("pending", "submitted", "accepted", "partially_filled")))
    ).all()
print(f"working_bot_entry_orders={len(rows)} rows={rows}")
if rows:
    raise SystemExit(1)
PY
}
until flat_once; do
  rc=$?
  case "$rc" in 1|2) sleep 300 ;; *) printf 'REFUSE flat helper rc=%s\n' "$rc" >&2; exit 1 ;; esac
  now_et=$(TZ=America/New_York date +%H%M%S)
  if (( now_et >= 191000 && now_et < 201000 )); then
    printf 'REFUSE too late to finish by 19:20; wait until after 20:10\n' >&2
    exit 1
  fi
done
now_et=$(TZ=America/New_York date +%H%M%S)
if (( now_et >= 191000 && now_et < 201000 )); then exit 1; fi
entry_orders_clear | sudo tee -a "$JOURNAL"
V2_PID=$(sudo systemctl show -p MainPID --value project-mai-tai-schwab-1m-v2.service)
V2_WINDOW=$(sudo sh -c 'tr "\0" "\n" < "$1" | grep -E "^MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_WINDOW_END_(HOUR|MINUTE)_ET="' sh "/proc/$V2_PID/environ")
printf '%s\n' "$V2_WINDOW" | sudo tee -a "$JOURNAL"
test "$(printf '%s\n' "$V2_WINDOW" | grep -Fxc 'MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_WINDOW_END_HOUR_ET=15')" = 1
test "$(printf '%s\n' "$V2_WINDOW" | grep -Fxc 'MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_WINDOW_END_MINUTE_ET=45')" = 1
# Record arms before proceeding.
sudo systemctl stop project-mai-tai-strategy.service
flat_once
entry_orders_clear | sudo tee -a "$JOURNAL"
sudo systemctl restart project-mai-tai-oms.service
sudo systemctl is-active --quiet project-mai-tai-oms.service
sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-oms.service
flat_once
entry_orders_clear | sudo tee -a "$JOURNAL"
sudo systemctl start project-mai-tai-strategy.service
sudo systemctl is-active --quiet project-mai-tai-strategy.service
sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-strategy.service
flat_once
entry_orders_clear | sudo tee -a "$JOURNAL"
sudo systemctl restart project-mai-tai-orb-schwab.service
sudo systemctl is-active --quiet project-mai-tai-orb-schwab.service
sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts project-mai-tai-orb-schwab.service
for unit in "${UNITS[@]}"; do
  sudo systemctl show -p MainPID -p ActiveEnterTimestamp -p NRestarts "$unit.service"
done | sudo tee -a "$JOURNAL"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
```

The `# Record` comment is a **manual stop point**: the operator must read v2's
actual 15:45 setting and record armed segments. The database entry-order
query is a necessary check, not proof of broker-side absence; reconcile any
pending broker-side entry before proceeding. Verify the five-unit PID snapshot
at each stop point. Do not run the block as one unattended script.
If the pre-19:20 deadline becomes infeasible after strategy is stopped, halt
and report the partial state rather than crossing rotation or starting an
unapproved recovery. The operator's GO must explicitly accept this scope.

## Execution after the exact-SHA GO

1. Capture PID, start time, `NRestarts`, and code identity for **OMS, strategy,
   v2, orb-schwab, and market-data** before and after the scoped operations;
   also record ORB and momentum-paper. Back up the host-specific
   `/home/trader/preopen.sh` and isolated FLAGGATE catalog, with SHA-256.
   Use the literal command block below only after substituting the GO SHA.
2. After a fresh clear bot-flat read, stop strategy. Re-read bot books and
   broker positions. Restart OMS at the approved SHA, then read the **new
   OMS PID's** `/proc/<pid>/environ` and its loaded settings: CW floor false,
   target 5.0%, hard stop 8.0%, RETRY_ONE true, EOD OCO transition true,
   OVERNIGHT_FLATTEN true and ORB live orders true. If #1079 is included,
   require `oms_v2_cw_target_stay_enabled=true`; otherwise do not claim that
   rule is installed. Check health, `NRestarts`, tracebacks and reconciler.
3. On another fresh clear bot-flat read, start strategy. Check its new PID,
   settings, health and gateway subscription ownership. On another fresh clear
   read, restart orb-schwab **only if #1078 is included**, because that PR
   changes `orb_schwab_app.py`; verify new PID, live orders true, observe false,
   boot mode, health and no traceback. Never restart v2, ORB or market-data in
   this phase. Recheck their PID/start/`NRestarts` against step 1. Keep the
   momentum-paper state decided by the separate gateway plan.
4. If #1079 is in the final SHA, install the reviewed `expected_flags.json`
   into `/home/trader/restart_evidence/` after backing up the old copy and
   comparing its SHA-256 to the exact Git blob. Require the target-stay entry
   to be expected true. Do not install the INC1 watch or alter its cron guards:
   `oms_v2_cw_target_cancel_unconfirmed` will **not page** until the watch
   copy is separately re-pinned after WBREAD1. Surface that gap in the GO and
   journal; do not report the new incident as a working pager.
5. Back up and re-pin `/home/trader/preopen.sh` **once** for 2026-10-02,
   **after both the gateway and OMS phases**, with the approved checkout SHA,
   new OMS/strategy/orb-schwab identities, and the verified unchanged v2/ORB
   identities **plus the final gateway PID/start**. Preserve the installed
   restart checker/router and FLAGGATE routing. Log the diff, `bash -n`, and
   before/after hashes. Run the gate read-only as `trader`; record seven PIN
   lines, both Final calls, exact rc and raw evidence path. UNKNOWN remains
   UNKNOWN, never a green gate. The historical timestamp-less traceback may
   still require a separately reviewed checker fix; do not mask it here.

Journal the exact SHA, all five primary PIDs/start times before and after,
flat-read evidence, backups/hashes, restart timestamps, `/proc` flag readings,
gate calls and accepted gaps. After the first-check retry allowance, a
failed/unknown preflight or health check stops further steps and pages the
operator; this plan does not pre-authorize a
rollback or an extra restart. No trading-service install occurs without the
operator's final exact-SHA GO.
