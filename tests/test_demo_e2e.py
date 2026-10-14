"""Real-container generic workflow demonstrations with explicit test-only model fixtures."""
from __future__ import annotations

import ast
import os

import pytest

from scripts.fixture_demo import SCENARIOS, run_scenario, scenario_contract
from workbench.query import sql_preview
from workbench.schemas import BuildResult, ProposalSet
from workbench.settings import Settings
from workbench.workspace import WorkspaceManager


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_reviewed_fixture_contracts_and_source_compile(scenario):
    fixture = scenario_contract(scenario)
    proposal = ProposalSet.model_validate(fixture["proposal_set"]).proposals[0]
    build = BuildResult.model_validate(fixture["build"])
    assert build.expected_outputs == proposal.expected_outputs
    for file in build.files:
        if file.path.endswith(".py"):
            ast.parse(file.content)


@pytest.mark.docker
@pytest.mark.skipif(os.getenv("RUN_DOCKER_TESTS") != "1", reason="Set RUN_DOCKER_TESTS=1 to execute real Docker integration tests")
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_generic_workflow_real_docker_all_approaches(tmp_path, scenario):
    settings = Settings(workspace_root=tmp_path / "workspaces", sandbox_timeout_seconds=90, max_repair_attempts=0)
    workspace, state = run_scenario(settings, scenario, approve=True)
    assert state["status"] == "COMPLETED", state.get("error") or state.get("validation")
    manager = WorkspaceManager(settings.workspace_root)
    reopened = manager.get(workspace.name)
    assert manager.read(reopened, "metadata/validation.json")["passed"] is True
    manifest = manager.read(reopened, "metadata/manifest.json")
    locations = {artifact["location"] for artifact in manifest["artifacts"]}
    assert "notebooks/solution.ipynb" in locations
    assert "generated/solution.py" in locations
    contract = scenario_contract(scenario)
    expected = contract["build"]["expected_outputs"]
    assert {item["location"] for item in expected} <= locations
    assertion = contract["build"]["validation_checks"][0]["sql"]
    assert sql_preview(reopened, assertion, limit=1)["rows"] == [[True]]
    assert manager.read(reopened, "metadata/demo_fixture.json")["real_model_used"] is False
