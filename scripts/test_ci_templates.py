"""Regression tests for canonical consumer workflow templates."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_TEMPLATE = REPO_ROOT / "templates" / "ci-workspace-template.yml"
REUSABLE_WORKSPACE_WORKFLOW = (
    REPO_ROOT / ".gitea" / "workflows" / "validate-workspace-template.yml"
)


def _workspace_run_steps() -> list[str]:
    workflow = yaml.safe_load(WORKSPACE_TEMPLATE.read_text())
    return [
        step["run"]
        for step in workflow["jobs"]["validate"]["steps"]
        if "run" in step
    ]


def _all_run_steps(path: Path) -> list[str]:
    workflow = yaml.safe_load(path.read_text())
    return [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]


def test_workspace_template_only_invokes_scripts_present_in_cloned_ci_repo() -> None:
    references = {
        match
        for command in _workspace_run_steps()
        for match in re.findall(r"\.molecule-ci/[^\s]+\.py", command)
    }
    assert references
    for reference in references:
        source_path = REPO_ROOT / reference.removeprefix(".molecule-ci/")
        assert source_path.is_file(), f"template references missing {source_path}"


@pytest.mark.parametrize(
    "path",
    (WORKSPACE_TEMPLATE, REUSABLE_WORKSPACE_WORKFLOW),
)
def test_workspace_runtime_install_uses_source_pinned_installer(path: Path) -> None:
    commands = _all_run_steps(path)
    installers = [
        command for command in commands
        if "install_workspace_dependencies.py" in command
    ]
    assert len(installers) == 1
    assert all(
        not (
            "--extra-index-url" in command
            and "molecules-workspace-runtime" in command
        )
        for command in commands
    )
