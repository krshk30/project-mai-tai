# Reproduce exact-current-main follow-up

Run in `/Users/velkris/Projects/project-mai-tai-slotclear1-side` with the shared
Python 3.12.13 environment. Frozen candidate receipt head is
`95cfbae86c737b522051f6ff57c5b9e285cf55ef`, tree
`e085678096f55ef7634c0cc170d065eb56a43769`. Runtime source/tests/ops match d9ac.
Later receipt-only commits do not change runtime. Exact baseline head is
`994f08aee2c35b3809b3c3384e0d8628807dcfb7`, tree
`7d827a407825d1d93aae8c3946d8b55320b9680f`.

Use fresh output folders; do not overwrite prior failure receipts.

```sh
PY=/Users/velkris/Projects/project-mai-tai/.venv/bin/python
ROOT=/Users/velkris/Projects/project-mai-tai-slotclear1-side
ART=docs/review-artifacts/slotclear1
env PYTHONPATH=src:. PROOF_TIMEOUT_SECONDS=660 "$PY" "$ART/run_receipt.py" "$ROOT" /tmp/slot-candidate-repeat "$PY" "$ART/run_timed_pytest.py" /tmp/slot-candidate-repeat/test-times.jsonl tests/unit -q --tb=short -vv --capture=tee-sys
env PYTHONPATH=src PROOF_TIMEOUT_SECONDS=660 "$PY" "$ART/run_receipt.py" "$ROOT" /tmp/slot-candidate-standard-repeat "$PY" -m pytest tests/unit -q --tb=short -vv
env PYTHONPATH=src:. "$PY" -m pytest tests/unit/test_slotclear1_fresh_flip.py tests/unit/test_slotclear1_fresh_sell.py "$ART/test_both_paths.py" -q --tb=short
env PYTHONPATH=src:. "$PY" "$ART/check_ticket_veto_mutation.py"
env PYTHONPATH=src:. "$PY" "$ART/check_mutations.py" S1
env PYTHONPATH=src:. "$PY" "$ART/check_sell_mutations.py" F1
```

Mutation children intentionally return 1 for a named assertion failure; run S1-S7,
F1-F11 and the T1 ticket-veto child separately. Existing receipts preserve commands,
heads, actual RED failed names, output hashes and wall-clock UTC. Do not interpret
an import/collection failure as mutation proof. The current-main baseline was run
with PYTHONPATH=src, pytest tests/unit -q --tb=short -vv in the clean scratch
`/tmp/codex-slotclear-sell-current-main-994-20261007`; candidate adds only an
observational UTC-node hook and tee-sys capture, not modified gates.

IMPORTANT: frozen95cf's first timing launcher lacked a __main__ guard and spawned
children re-entered pytest. Full first run58fail/7181pass is retained, with two
added momentum handoff failures. Corrected launcher makes all7 handoff tests pass
in a bounded supplemental receipt, but that does not waive the full failure.
Standard candidate rerun uses the exact baseline command, environment and capture,
without launcher/plugin at all. Source/tests/ops remain frozen; no trading fix.
Any timing-assisted repeat must use the corrected launcher in later receipt head.
Standard full-suite runs preserve exact suite UTC but do not separately record
internal HOTFIX gate UTC or passing captured metrics; first timed gates remain
disclosed separately, not fabricated for the standard runs.

Flags: both SLOT paths default OFF; BUY and SELL are independent. Supplemental
both-path controls turn both ON. Present-ticket control additionally sets
ATR reprice handoff OFF, but restores the exact durable ticket payloads. Unit
suite fixtures vary their own flags. No service/environment/production flag changed.

Read-only evidence helpers `pull_sell_evidence.py` and `pull_rpg_ownership.py`
execute bounded SQL and log reads on mai-tai-vps, with rollback and no reconciliation.
Re-pulls reflect new present state; they cannot reproduce a historical ticket book.
Use retained JSON for reproducible probes/ticket controls. See
`RECORDED_INPUT_BOUNDARIES.md` for every recorded versus injected boundary.

Initial local command using a nonexistent SLOT .venv path returned 127 before
pytest; corrected to the shared environment above. This was a harness launch
failure, not a source test result. Old suite failures and NFQ clean-only composition
conflicts remain preserved. No #1115/RPGSTALE changes or forced ownership releases.
