# Install1 stopped after application writes

As of 2026-10-06 19:14 ET, own read-only receipts from the attended attempt.
NOT COMPLETE. No recovery, reset-failed, additional stop/start or control restart.

- Plan: 01ed2458eefb603100e2b586dff972d80712b6b6.
- Release manifest: b1b811665e69b943dffd745aa45e10a0935d534e1b39aa52465675d0c168064e.
- Staged artifact verification: all 27 hashes matched before execution.
- Local runner tests: 753 passed; focused sequence/control tests: 198 passed.
- Runner began 18:56 ET. OMS and strategy strict-flat rc0, census rc0,
  Redis rc0, OMS restart preflight rc0, armed reader rc0, v2 preflight rc0.
- A 19:00:10 strict-flat TimeoutError/rc2 recovered on the approved retry at
  19:01:29; raw stdout/stderr preserved. Every measured flat/census gate passed.
- Checkout is now clean 4805ddc81184c76b4d5cef5c483c809edb666fe6.
- Env SHA256: 0faab25f367c1974c17c14f70fb205497a3c521d9967b6e7fa28595d030d857e.
- v2 stop rc0 at 19:09:01 ET; orb-schwab stop rc0 at 19:11:43 ET.
- Refusal 19:11:44 ET: `exact CancelledError missing`, stage `stop-orb-schwab`,
  runner rc1. Completed counter 1: orb stop happened, but its exception proof
  refused before checkpoint/reset. No OMS stop/start happened.

Actual states independently re-read:

| Service | MainPID | State | Result | NRestarts |
|---|---:|---|---|---:|
| schwab-1m-v2 | 0 | inactive | success | 0 |
| orb-schwab | 0 | failed | exit-code, status 1 | 0 |
| oms | 362892 | active | success | 0 |
| control | 2916 | active | success | 0 |

The captured shutdown journal contains only systemd's intended stop and exact
old invocation exit1. The unit reports StandardOutput=append and
StandardError=append. `/var/log/project-mai-tai/orb-schwab.log` ends with the
mai-tai-orb-schwab entrypoint traceback and `asyncio.exceptions.CancelledError`;
that traceback is untimestamped. No claim that the journal contains it, no
relabelling of the refused exception proof as PASS.

The abort adapter delivered the page with rc0. Same-attempt continuation with
tested file-log evidence and the narrow reset was requested; awaiting authority.
Install2 not started. FLAGGATE, new-process keys, bar continuity, final preopen
pin and daily timer installation have NOT been completed.

Raw evidence root:
`/home/trader/roundup1-install1-20261006-01ed2458/attempt-oct6-attended/`.

- `STOP.json`: e08c2fca44bf43a5795d5cb2de113333793a196f5ff4752984b7c02f7be40a4f.
- `abort-delivery.json`: 35e1928f49c4fc52f3d525c9fd83f9e9d11019a8617c140cb455f1cb0625a0b7.
- `763-stdout.txt`: actual bounded shutdown journal; no app traceback present.
- `135-ticket-dispositions.json`: 103 rows, in-flight []; expired11, filled10,
  held_unknown2, refused80. Unknown ownership is retained, never cleared by install.
- `478-command.json`: v2 stop rc0; `708-command.json`: orb-schwab stop rc0.
- `runner.log` in the parent directory: SHA256
  dc968b4f2ba161aa418b1229a1a22bb43711a5f0236de8908535194811fda21c.

Last 30 runner.log lines, unchanged:

```text
2026-10-06T23:11:44.510624+00:00 receipt=790-stdout.txt
2026-10-06T23:11:44.515129+00:00 receipt=791-stderr.txt
2026-10-06T23:11:44.550065+00:00 receipt=792-command.json
2026-10-06T23:11:44.557755+00:00 receipt=793-stdout.txt
2026-10-06T23:11:44.559267+00:00 receipt=794-stderr.txt
2026-10-06T23:11:44.576724+00:00 receipt=795-command.json
2026-10-06T23:11:44.579959+00:00 receipt=796-stdout.txt
2026-10-06T23:11:44.582352+00:00 receipt=797-stderr.txt
2026-10-06T23:11:44.609359+00:00 receipt=798-command.json
2026-10-06T23:11:44.612242+00:00 receipt=799-stdout.txt
2026-10-06T23:11:44.613696+00:00 receipt=800-stderr.txt
2026-10-06T23:11:44.634282+00:00 receipt=801-command.json
2026-10-06T23:11:44.639172+00:00 receipt=802-stdout.txt
2026-10-06T23:11:44.641788+00:00 receipt=803-stderr.txt
2026-10-06T23:11:44.656562+00:00 receipt=804-command.json
2026-10-06T23:11:44.672956+00:00 receipt=805-stdout.txt
2026-10-06T23:11:44.677429+00:00 receipt=806-stderr.txt
2026-10-06T23:11:44.695026+00:00 receipt=807-command.json
2026-10-06T23:11:44.697480+00:00 receipt=808-stdout.txt
2026-10-06T23:11:44.698998+00:00 receipt=809-stderr.txt
2026-10-06T23:11:44.717323+00:00 receipt=810-command.json
2026-10-06T23:11:44.721289+00:00 receipt=811-stdout.txt
2026-10-06T23:11:44.722929+00:00 receipt=812-stderr.txt
2026-10-06T23:11:44.742725+00:00 receipt=813-command.json
2026-10-06T23:11:44.746944+00:00 receipt=814-stdout.txt
2026-10-06T23:11:44.749302+00:00 receipt=815-stderr.txt
2026-10-06T23:11:44.872939+00:00 receipt=816-command.json
2026-10-06T23:11:44.883917+00:00 receipt=817-stdout.txt
2026-10-06T23:11:44.885515+00:00 receipt=818-stderr.txt
STOP attended error_type=Stop reason=exact CancelledError missing
```
