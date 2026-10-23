# ALERTS1 SAIQ: additive acceptance controls / ALL NEW CASES UNRUN

Append from frozen985389bb, production/source rule/incident fixture UNCHANGED.
Main owns all runtime and approval. No author tests/imports/AST/compile/DB/PG/
provider/SSH; this file and test source do NOT authorize starting a cluster.

Existing38 functional cases remain;1 additive health-route case (total39) makes
classification succeed but actual checker commit fail. Original RED count and
urgent phone call must remain. This closes a coverage gap: previous classifier
exception test did not exercise the later commit boundary. Helper's default
commit behavior and existing38 expectations remain unchanged.

3 opt-in PostgreSQL cases, ALL UNRUN, in
tests/integration/test_alerts1_saiq_operator_closed_postgres.py:
- Two real, distinct live transactions: owner locks reviewed Fill and stages
audit/tags; contender must receive PostgreSQL55P03 NOWAIT refusal (not generic
exception or SQLite). After rollback+owner commit, contender repeats and only
ONE audit remains durable. All3 original incident payloads/status preserved.
- Creator stages/flushed audit/tags then ROLLS BACK. Independent read confirms
zero audits and all3 original critical payloads; successor can commit ONE audit.
- Committed bot-sell intent with no Fill prevents exclusion and keeps all3 exact
incidents critical. No fabricated sell Fill, order/entry mutation or position fix.

This is isolated CONTROLLED PostgreSQL, seeded from retained39@7.62 buy facts /
actual15:22:04.337 timestamp and original three incident payloads. Generated test
order/fill UUIDs are explicitly not native DB identity claims. It is NOT a restore
of production, a live broker-flat read, an actual phone notification, or proof
production rows were changed. The original native order/coid and reviewed row
bindings remain exact. Function candidate IDs are explicit controlled inputs;
this does not claim CURRENT_DATE still selects the historical SAIQ after midnight.

## Strict main-only protocol

Module skips without ALERTS1_SAIQ_PG_CLAIM=MAIN_OWNED_ISOLATED. That skip is not
qualification; the native gate stays UNRUN until explicitly claimed. With claim,
missing/wrong URL FAILS rather than skipping/falling back to SQLite. It accepts
only psycopg/current known main-owned private CI socket55442/database/user with
exact host/port keys, no TCP/password/general foreign-cluster adoption. Main must
independently verify actual PID/data directory/ownership/schema/PG16 before run;
matching URL is NOT evidence of those facts. The known owned CI cluster was
stopped after earlier work; no start/reuse is granted by this source checkpoint.

Fixture creates one unique schema per case, creates required tables only there,
sets search_path to that schema, bounds lock/statement waits at2/5s and drops
only its own generated schema in finally. No public-schema truncate/drop. No
production-copy adoption, foreignPG action, provider, SDK or app startup needed.

Main proposed functional claim (fresh raw/XML required, unchanged serial policy):

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q --tb=short -o addopts= -p no:cacheprovider tests/unit/test_alerts1_saiq_operator_closed.py --junitxml=<fresh-functional.xml>

Native claim ONLY after separate actual-owned-PG preflight/authorization:

    ALERTS1_SAIQ_PG_CLAIM=MAIN_OWNED_ISOLATED MAI_TAI_DATABASE_URL='postgresql+psycopg://postgres@/project_mai_tai_test?host=/private/tmp/mai-tai-night-ci-20261022/socket&port=55442' PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q --tb=short -o addopts= -p no:cacheprovider tests/integration/test_alerts1_saiq_operator_closed_postgres.py --junitxml=<fresh-native.xml>

Capture raw output and preserve failures; no automatic retry or baseline waiver.
Actual complete checker SQL/production3row effects/phone policy/main nightly
preservation still require main qualification. This is not whole ALERTS1 GO.

Resume: L13new11-case source00ac1d8f remains UNRUN and production unchanged.
Claude16:21 explicitly requires actual systemdP4, latest ALL-ONP5/boot/P6; no
manual test supervisor or this SAIQ module substitutes. Consumed L13P1/P3 not
repeated. L2 retains first runtime capacity. No shared queue/status/LATEST edits.
