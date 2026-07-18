"""Regression tests for canonical consumer workflow templates."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_TEMPLATE = REPO_ROOT / "templates" / "ci-workspace-template.yml"
MINIMAL_TEMPLATE = REPO_ROOT / "templates" / "ci-minimal.yml"
DIFF_SECRET_TEMPLATE = REPO_ROOT / "templates" / "ci-secret-scan.yml"
CONSUMER_TEMPLATES = tuple(sorted((REPO_ROOT / "templates").glob("ci-*.yml")))
SECRET_SCANNING_WORKFLOWS = (
    WORKSPACE_TEMPLATE,
    REPO_ROOT / "templates" / "ci-plugin.yml",
    REPO_ROOT / "templates" / "ci-org-template.yml",
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


@pytest.mark.parametrize("path", CONSUMER_TEMPLATES)
def test_consumer_templates_only_invoke_scripts_present_in_cloned_ci_repo(
    path: Path,
) -> None:
    references = {
        match
        for command in _all_run_steps(path)
        for match in re.findall(r"\.molecule-ci/[^\s]+\.py", command)
    }
    for reference in references:
        source_path = REPO_ROOT / reference.removeprefix(".molecule-ci/")
        assert source_path.is_file(), f"{path.name} references missing {source_path}"


@pytest.mark.parametrize("path", CONSUMER_TEMPLATES)
def test_consumer_templates_never_use_remote_workflow_call(path: Path) -> None:
    assert not re.search(
        r"uses:\s+\S+/\.gitea/workflows/\S+@", path.read_text()
    ), f"{path.name} uses unsupported cross-repository workflow_call"


@pytest.mark.parametrize("path", CONSUMER_TEMPLATES)
def test_inline_ssot_templates_pin_and_verify_an_immutable_ref(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text())
    job = next(iter(workflow["jobs"].values()))
    ref = job["env"]["MOLECULE_CI_REF"]
    commands = "\n".join(_all_run_steps(path))
    assert re.fullmatch(r"[0-9a-f]{40}", ref)
    assert "git clone" not in commands
    assert 'fetch -q --depth 1 origin "$MOLECULE_CI_REF"' in commands
    assert 'rev-parse HEAD)" = "$MOLECULE_CI_REF"' in commands


def test_inline_ssot_templates_assert_execution_sentinels() -> None:
    assert "minimal-validate:sentinel:executed" in MINIMAL_TEMPLATE.read_text()
    assert "secret-scan:sentinel:executed" in DIFF_SECRET_TEMPLATE.read_text()


@pytest.mark.parametrize(
    "path",
    (WORKSPACE_TEMPLATE,),
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


@pytest.mark.parametrize("path", SECRET_SCANNING_WORKFLOWS)
def test_workflow_secret_scans_use_redacting_canonical_script(path: Path) -> None:
    content = path.read_text()
    commands = _all_run_steps(path)

    assert "match.group(0)" not in content
    scanners = [command for command in commands if "check-secrets.py" in command]
    assert len(scanners) == 1
