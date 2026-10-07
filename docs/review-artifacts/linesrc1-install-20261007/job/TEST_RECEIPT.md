# [codex] Oct7 Local Runner Receipt

Base APP: f9c9bd332392e2c905fc39b954421c88970844d7.
APP tree: 0fa89d5d40473e29e1039cd6a80200b36945d696.
Production BOX binding: 5b8b4f642bbc3c312be436d0e92adbc22d9e9f95.

Independent local final run: **84 passed in 0.56s**, zero failures/errors.
Raw XML: /tmp/linesrc1-install-20261007-runner.xml.
XML SHA256: a9a236631d9e3402f9ba767ff63f11cd0b155139b1341436d6276a2c21d55d40.

```sh
env PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=src:docs/review-artifacts/linesrc1-install-20261007/job \
  /Users/velkris/Projects/project-mai-tai/.venv/bin/python -B -m pytest \
  -q -p no:cacheprovider --junitxml=/tmp/linesrc1-install-20261007-runner.xml \
  docs/review-artifacts/linesrc1-install-20261007/job/test_runner.py
bash -n docs/review-artifacts/linesrc1-install-20261007/job/run.sh
```

The tests cover the direct official repository report collector before ack repin;
byte-exact recorded daily/gate/ack/numeric fixtures; actual daily.verify_runtime
and upgrade_ack.current code after a controlled repin; unchanged ORB identity
rejection; retained original historical pins; 147+10 catalog checks; source8
numeric refusal; phase and duplicate fences; rc2 retry bounds; 64MB archive
bound; clock-only native gate pending; clean stop/start and midnight crossover;
strict env byte change; real STOP receipts despite notification failure;
exact release/artifact/approval/owner-mode/symlink tamper cases; absent active
approval at assembly; direct collector invocation; genuine empty watched list;
exact restart-minute evidence; final late-startup-error refusal.

Recorded facts are fixture byte hashes and imported helper implementations.
Service, clock, SQL, package assembly Git replies and notification replies are
CONTROLLED local cases, not production proof. Only unavailable remote historical
evidence reads in the daily integration test are explicitly simulated; those
historic bytes have NOT been independently remeasured. All daily artifact byte
hashes and new local evidence hashes are checked without substituting hashes.

Production activation, final after-close full fleet metadata, staged Linux unit
verification, real runtime acknowledgements/157 checks, actual bar-hole outcome,
notification delivery and next07:00 restoration are UNMEASURED in this lane.
No trading/source files, production state or existing daily units were changed.
Parent disables the dated installer timer after COMPLETE/STOP and removes the
approval latch whenever pending attendance ends. Daily preopen stays unchanged.

Parent-reported merge-source acceptance is separate from these 84 runner tests.
Literal LINE-REBUILD-CYCLE markers are not promised zero: parent reports one
provider GET plus nine live suffix passes produced ten markers. Pinned source
is preserved; future acceptance must count provider authority separately.
