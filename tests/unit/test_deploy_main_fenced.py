from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "deploy-main.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_deploy_main_is_a_visible_hard_refusal() -> None:
    text = _workflow()

    assert "workflow_dispatch:" in text, "an attempted use must leave a visible Actions run"
    assert "name: DISABLED - Deploy Main" in text
    assert text.count("run: |") == 1, "there must be only one executable step: the refusal"
    run_block = text.split("run: |", maxsplit=1)[1]
    commands = [line.strip() for line in run_block.splitlines() if line.strip()]
    assert commands == [
        'echo "::error::Deploy Main is prohibited because it can mutate production before '
        'reporting failure. Use Deploy Service with the narrowest service target."',
        "exit 1",
    ], "the refusal step gained executable work before or after its failure"


def test_deploy_main_has_no_route_to_mutate_production() -> None:
    text = _workflow().lower()

    forbidden = {
        "ssh": "remote access",
        "vps_": "production credentials",
        "deploy_main.sh": "the old full-fleet deploy",
        "alembic": "migrations",
        "pip install": "runtime installation",
        "git fetch": "checkout mutation",
        "git merge": "checkout mutation",
        "systemctl": "service mutation",
        "uses:": "an external action that could bypass the sole refusal step",
    }
    hits = [f"{needle} ({meaning})" for needle, meaning in forbidden.items() if needle in text]
    assert hits == [], "Deploy Main regained a production mutation path: " + ", ".join(hits)
