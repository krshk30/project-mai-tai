# Parent ORB completion: separate bookkeeping only

Parent owns all production actions. Application stays
06b5e388affb5f33e4edf80c6e635cf72dbc3f78. No app/gateway/env/catalog/service/
schema/DB/Redis/timer action belongs to this sidecar. Hotfix r3 ABORT and every
old helper/manifest/proof/seal remain immutable. Global install FAIL is retained.

Actual parent evidence: ORB-Schwab 2121782, start 2026-10-09 00:10:30 UTC,
InvocationID 9957f01234194abaa77597aa13263904, active/running/NRestarts0.
Systemd Started marker 00:10:30.477388 UTC, live Result success/ExecMainStatus0.
Parent restart receipt records actual command rc0 and explicitly marks at_utc
as the systemd start second; precise command-begin timestamp was not captured.
Old InvocationID is positively bound to the hashed prior upgrade-ack.json,
not reconstructed or fabricated.
Old process SIGTERM exit1 remains an observation; no reset/recovery by sidecar.

Retirement receipt:
/home/trader/restart_evidence/orblive-closeout-20261008/orb-retirement.json
SHA256 b670c1ff8ea62ef71506a3bcdae5957031b759c031d45d56388162c60c1473ab.
Request 1791504678676-0 applied, old paper ORB inactive/disabled/PID0. All five
owners and migration marker preserved except orb/cursor. No heartbeat purge.
Catalog remains b1f09e034de2a38af846b20887fa6b97a1075ed1b4f22126388657208db49bb3.

Local amended repin requires both restart and retirement receipts. It validates
the old restart identity against the historical upgrade ACK, new identity against
live systemd, and retirement against the applied request cursor. The cumulative
record adds only orb-schwab to the existing four-service restart group. Original
snapshot stays unchanged; cumulative record and source journal use exclusive new
paths and preserve prior FAIL/ABORT/hash bindings. Only the retired paper ORB
identity arguments/check disappear. New ORB-Schwab gets ordinary NRestarts0
identity checks; upgrade ACK remains historical with active_for_current_install
false. Its wrapper no longer admits the old acknowledgement as current proof.
Dynamic date/paper admission, adapter/root mode and all other identities stay.

Stage only repin_preopen.py, orb_receipts.py, removed_wait_readonly.py in a new
separate directory; publish hashes and verify before invocation. No old job rerun.
Literal repin contract (paths are populated from actual parent evidence first):

```sh
sudo -n env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/trader/project-mai-tai/src \
  /home/trader/project-mai-tai/.venv/bin/python \
  /home/trader/after-hours/2026-10-08/orb-completion-bookkeeping/repin_preopen.py \
  --approved-sha 06b5e388affb5f33e4edf80c6e635cf72dbc3f78 \
  --snapshot /home/trader/restart_evidence/hotfix-bookkeeping-06b5e388/original-snapshot.json \
  --install-record /home/trader/restart_evidence/orb-completion-bookkeeping-06b5e388/install-record.json \
  --orb-restart /home/trader/restart_evidence/orb-completion-bookkeeping-06b5e388/orb-restart.json \
  --retirement /home/trader/restart_evidence/orblive-closeout-20261008/orb-retirement.json \
  --line-enabled true \
  --receipt /home/trader/restart_evidence/orb-completion-bookkeeping-06b5e388/preopen-repin.json
```

Atomic backups/runtime-last; receipt REPINNED_CHECKS_NOT_RUN. Run actual daily
verify_runtime and official checks-only afterwards and retain raw rc/report.
No 9/9 claim if warmup evidence remains UNKNOWN; inactive momentum-paper remains
UNKNOWN by design in the flag audit. Four historical v2 errors are not waived.

Local validation: 343 mechanics PASS, seven platform skips, twenty assertion
mutations RED, Ruff clean. Real daily.verify_runtime is exercised on the isolated
five-service cumulative binding/retired-ACK fixture. No global unit-suite parity
claim. This package neither executes a restart nor repeats the parent retirement.

## Actual execution receipt

Source plan e064dc48438a5f0a26aa45c231174a0f8a7e1fb4 executed only the
authorized bookkeeping at 2026-10-09 00:15:05.703646 UTC (Oct 8 20:15 ET).
Separate package /home/trader/after-hours/2026-10-08/orb-completion-bookkeeping,
manifest SHA256
931840c3e1e43f842351314bf81a27acbdd1289c7ca78e0cfd8c7ad09a636576.
No application, service, gateway, environment, catalog, schema, DB, Redis or
timer action was performed by this sidecar.

Evidence directory:
/home/trader/restart_evidence/orb-completion-bookkeeping-06b5e388.

- preopen-repin.json SHA256
  2b5ace7555c0c53905a38cd31aaa82b464c6b0fb0aa07b9947912e643f129407;
  verdict REPINNED_CHECKS_NOT_RUN, atomic six-file backup/runtime-last.
- Current /home/trader/preopen.sh SHA256
  885cbe8b714ee72cab7c8726e77fe74a641235f61ea9a6ca3e69167b07e3b83c.
- install-record.json SHA256
  aada6da8c2bb9d223e69f3ea5430dd4868adcb7e5f2b41ffee0fb4ea39e3f7bc.
- sealed-actions.json SHA256
  1e1f57bc41b4a4864af0a28c38de5ed5025c755872fbf20fad7e50fedc496d1c.
- checks-only.json SHA256
  7285fd10df30d215fc71088d89cc4dc14549aa14198067c5e7b8bc23681802cb.

Real daily.verify_runtime rc0; shell syntax rc0. Official checks-only rc1.
Official restart report: seven PASS, one FAIL, one UNKNOWN of nine checks.
All five restarted identities and four untouched identities PASS; migration0023,
REST warmup, BOOT-HOLD, flatness and process flags PASS. Bar continuity UNKNOWN
because the official persisted-history warmup query cannot prove its window.
Tracebacks FAIL: 22 v2 headers since the hotfix restart; new ORB-Schwab zero.
The prior ORB process exit is correctly classified as before bounded startup,
not attributed to the new invocation. Full preopen additionally fails the
before-07:00 fence and inactive paper/guard admission at this evening run.
Catalog 153/155, zero mismatches, two momentum-paper UNKNOWN; no waiver.
Official stdout SHA256
54b39769822c2ad9b3d1f5f7535fce068e761e556941b92bb55d30f70fd37785.

Bounded read-only context confirms actual v2 exceptions, not old ORB shutdown
or lexical timestamp artifacts. Source:
/var/log/project-mai-tai/schwab-1m-v2.log-20261009, lines 45595-45622 and
46135-46162: GRAN on_quote/on_stream_trade at 22:19:27/28 and 22:35:34 UTC
calls _resting_trigger_for_line, then resting_buy_stop, raising
ValueError: resting buy price must be positive and finite. No source fix,
restart, error filtering or permission waiver is included in this mechanics lane.
Retired orb's historical heartbeat is left intact; no DB purge or masking.

All sixteen immutable r3 manifest artifacts and its ABORT/proof/seal were
verified unchanged after this operation. Global install verdict remains FAIL,
never COMPLETE. Current binding pins ORB-Schwab 2121782/NRestarts0 and leaves
the old Redis upgrade ACK historical/inactive, with its original evidence kept.
Future cumulative re-pins must retain this authorized restart/retirement proof;
they must not adopt the old ACK or an arbitrary live identity.
