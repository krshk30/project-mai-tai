# 2026-10-01 conditional OMS and ORB Schwab install

Status: **plan for review, not an install authorization**. The operator must name
the final full `origin/main` SHA in a new GO. Do not infer that a pinned PR, a
merge, or this plan authorizes a production restart.

## Scope and hard gates

- Eligible changes are #1078, and #1079 only if each has an independent pin on
  its exact head, both Validate checks pass, and it merges by 15:30 ET. Record
  both PR heads, pin evidence, merge commits and the resulting exact main SHA.
  Never include an unpinned PR by advancing to a later main SHA.
- The separately approved Option A gateway phase must first finish with all
  four content proofs established, no Redis eviction and no rollback or
  unresolved UNKNOWN. Record its journal, final gateway PID and owner sets.
  Do not overlap an active gateway operation, the 20:00:06 log rotation, or a
  Redis recovery. If the gateway phase refuses or rolls back, stop this plan.
- Confirm no other deploy; `origin/main` equals the operator-approved full SHA;
  the box checkout can advance **only** to that SHA and is clean afterward.
  Save the old SHA, source-file hashes and process identities before advancing.
  Do not run a general deploy script or change a production flag or env value.
- Use the reviewed, hash-verified
  `/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py` helper
  from the gateway journal for fresh direct reads immediately before **each**
  service restart/start. Its rc 0 means bot-flat: zero open managed rows and
  virtual positions on both `live:schwab_1m_v2` and `live:orb`, zero net bot
  fills today by account/symbol, and readable broker positions on both legs.
  A manual broker holding is recorded by symbol/quantity but is not called an
  account-flat position. rc 1 (bot exposure) or rc 2 (unknown), or unreadable
  armed segments/working bot entries, stops the install. Require zero armed
  segments and no working bot entry orders as well. No restart on ambiguity.

## Execution after the exact-SHA GO

1. Capture PID, start time, `NRestarts`, and code identity for **OMS, strategy,
   v2, orb-schwab, and market-data**; also record ORB and momentum-paper.
   Back up the host-specific `/home/trader/preopen.sh`, isolated FLAGGATE
   catalog and any checkout-sensitive runtime files; record SHA-256 values.
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
5. Back up and re-pin `/home/trader/preopen.sh` **once** for 2026-10-02 with
   the approved checkout SHA, new OMS/strategy/orb-schwab identities, and the
   verified unchanged v2/ORB/gateway identities. Preserve the installed
   restart checker/router and FLAGGATE routing. Log the diff, `bash -n`, and
   before/after hashes. Run the gate read-only as `trader`; record seven PIN
   lines, both Final calls, exact rc and raw evidence path. UNKNOWN remains
   UNKNOWN, never a green gate. The historical timestamp-less traceback may
   still require a separately reviewed checker fix; do not mask it here.

Journal the exact SHA, all five primary PIDs/start times before and after,
flat-read evidence, backups/hashes, restart timestamps, `/proc` flag readings,
gate calls and accepted gaps. A failed/unknown preflight or health check stops
further steps and pages the operator; this plan does not pre-authorize a
rollback or an extra restart. No trading-service install occurs without the
operator's final exact-SHA GO.
