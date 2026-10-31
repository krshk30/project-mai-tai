from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/review-pin.yml"


def test_label_event_keeps_the_trusted_exact_head_gate() -> None:
    # BaseLoader preserves the Actions key "on" rather than YAML 1.1 Boolean conversion.
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"] == {"pull_request_target": {"types": ["labeled"]}}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"independent-review-pin"}
    job = workflow["jobs"]["independent-review-pin"]
    assert "if" not in job
    steps = job["steps"]
    assert all("continue-on-error" not in step for step in steps)
    # Operator 10-31: only the final verify step may be conditional, and only on the
    # docs-only scope (every changed path under docs/, renames split by --no-renames).
    assert all("if" not in step for step in steps[:4])
    assert steps[0]["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    fetch = steps[1]
    assert fetch["env"]["EXPECTED_HEAD"] == "${{ github.event.pull_request.head.sha }}"
    assert 'test "$actual" = "$EXPECTED_HEAD"' in fetch["run"]
    assert "exit 3" in fetch["run"]
    assert steps[2]["with"]["ref"] == "review-pins"
    scope = steps[3]
    assert scope["id"] == "scope"
    assert "git diff --no-renames --name-only" in scope["run"]
    assert "grep -qv '^docs/'" in scope["run"]
    verify = steps[4]
    assert verify["if"] == "steps.scope.outputs.docs_only != 'true'"
    assert verify["env"] == {
        "PR_NUMBER": "${{ github.event.pull_request.number }}",
        "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
        "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    }
    assert verify["run"].split() == [
        "python", "scripts/review_pin_gate.py", "verify", "\\",
        "--repo", ".", "\\", "--ledger", "review-pin-ledger", "\\",
        "--pr", '"$PR_NUMBER"', "\\", "--base", '"$BASE_SHA"', "\\",
        "--head", '"$HEAD_SHA"',
    ]
