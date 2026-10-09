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
