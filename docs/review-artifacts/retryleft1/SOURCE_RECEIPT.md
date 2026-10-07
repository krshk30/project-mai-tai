# RETRYLEFT1 source and unit receipt

Source checkpoint215d3853298204c43db756c264d7a938a26b412c, base
994f08aee2c35b3809b3c3384e0d8628807dcfb7. Sole source writer, clean source
through the completed full run; this result update changes docs only.

Worktree imports individually verified before result publication:
/Users/velkris/.codex/worktrees/retryleft1-20261007/project-mai-tai/src/.
Python /Users/velkris/Projects/project-mai-tai/.venv/bin/python, PYTHONPATH=src:.
Command: python -m pytest tests/unit -q --tb=short
--junitxml=/tmp/retryleft1-complete-unit-20261007.xml.

| Path | SHA256 |
| --- | --- |
| src/project_mai_tai/strategy_core/schwab_1m_v2.py | c1dabfa879ae9260d613b0bb70ff3901e0afb8ae3e4dda9ab669b73f24f17616 |
| src/project_mai_tai/services/schwab_1m_v2_bot.py | 693bac1de8f1f53e3f7ad11ff8d0b5777c9fdcc74068eb5e92e40bd9488b2687 |
| src/project_mai_tai/v2_removed_wait.py | f95ddeaddf041e3e1ae1b7cb17c208ef4be77a2eb72ec187a934581008ab85a0 |
| tests/unit/test_retryleft1.py | 5070e914c615accbffe502bf3cdb0fd206633d174f1abb090b415316e89a35de |

Full run:7,155 passed/56 failed460.41s; baseline7,114/56 on identical current
main,455.80s,19:03:43.202843-19:11:24.386758UTC today. Final head may advance
for documentation; require identical source/tests/ops to this checkpoint.
Ruff/scoped whitespace checks pass. Exact failures both56, added=[],removed=[].
Baseline actual stdout hash and metadata are in main-unit-result.json;
candidate actual stdout hash and names are in UNIT_PAIR.json. Interrupted
pre-correction full runs are not counted. No benchmark or live cancellation
latency claim is inferred from this full suite.
