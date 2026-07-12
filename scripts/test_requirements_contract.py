"""Tests for the workspace runtime requirements security boundary."""
from __future__ import annotations

from pathlib import Path

import pytest

from requirements_contract import (
    PRIVATE_INDEX_URL,
    RequirementsContractError,
    inspect_requirements,
)


def _inspect(tmp_path: Path, content: str):
    path = tmp_path / "requirements.txt"
    path.write_text(content)
    return inspect_requirements(path, root=tmp_path)


def test_extracts_canonical_runtime_requirement(tmp_path):
    contract = _inspect(
        tmp_path,
        "pyyaml>=6\nmolecules-workspace-runtime>=0.3.11,<0.4\n",
    )
    assert contract.runtime_requirement == "molecules-workspace-runtime<0.4,>=0.3.11"


def test_project_name_in_index_url_does_not_count_as_requirement(tmp_path):
    with pytest.raises(RequirementsContractError, match="must declare"):
        _inspect(
            tmp_path,
            "--extra-index-url https://example.invalid/molecules-workspace-runtime\n"
            "fastapi\n",
        )


def test_recursively_rejects_retired_runtime_requirement(tmp_path):
    (tmp_path / "nested.txt").write_text("molecule-ai-workspace-runtime>=0.1\n")
    with pytest.raises(RequirementsContractError, match="retired"):
        _inspect(
            tmp_path,
            "-r nested.txt\nmolecules-workspace-runtime>=0.3\n",
        )


def test_recursively_rejects_retired_runtime_constraint(tmp_path):
    (tmp_path / "constraints.txt").write_text(
        "molecule-ai-workspace-runtime==0.1\n"
    )
    with pytest.raises(RequirementsContractError, match="retired"):
        _inspect(
            tmp_path,
            "-c constraints.txt\nmolecules-workspace-runtime>=0.3\n",
        )


@pytest.mark.parametrize(
    "directive",
    (
        "-r ../outside.txt",
        "--requirement=https://example.invalid/requirements.txt",
    ),
)
def test_rejects_unsafe_requirement_includes(tmp_path, directive):
    with pytest.raises(RequirementsContractError, match="include"):
        _inspect(tmp_path, f"{directive}\nmolecules-workspace-runtime>=0.3\n")


def test_rejects_backslash_line_continuations(tmp_path):
    with pytest.raises(RequirementsContractError, match="continuation"):
        _inspect(
            tmp_path,
            "molecule-ai-workspace-\\\nruntime>=0.1\n"
            "molecules-workspace-runtime>=0.3\n",
        )


def test_rejects_percent_encoded_retired_vcs_egg(tmp_path):
    with pytest.raises(RequirementsContractError, match="retired"):
        _inspect(
            tmp_path,
            "molecules-workspace-runtime>=0.3\n"
            "git+https://example.invalid/runtime.git"
            "#egg=molecule-ai-workspace%2Druntime\n",
        )


def test_rejects_retired_bare_wheel_url(tmp_path):
    with pytest.raises(RequirementsContractError, match="retired"):
        _inspect(
            tmp_path,
            "molecules-workspace-runtime>=0.3\n"
            "https://example.invalid/molecule_ai_workspace_runtime-0.1-py3-none-any.whl\n",
        )


def test_rejects_canonical_runtime_direct_url(tmp_path):
    with pytest.raises(RequirementsContractError, match="private index"):
        _inspect(
            tmp_path,
            "molecules-workspace-runtime @ "
            "https://example.invalid/molecules_workspace_runtime-0.3-py3-none-any.whl\n",
        )


@pytest.mark.parametrize(
    "entry",
    (
        ".",
        "-e .",
        "--editable ../runtime",
        "other-project @ file:///tmp/other-project",
        "other-project @ git+https://example.invalid/other.git",
    ),
)
def test_rejects_local_and_editable_requirements(tmp_path, entry):
    with pytest.raises(RequirementsContractError, match="unsupported"):
        _inspect(
            tmp_path,
            f"molecules-workspace-runtime>=0.3\n{entry}\n",
        )


def test_allows_only_the_trusted_private_extra_index(tmp_path):
    contract = _inspect(
        tmp_path,
        f"--extra-index-url {PRIVATE_INDEX_URL}\n"
        "molecules-workspace-runtime==0.3.125\n",
    )
    assert contract.runtime_requirement == "molecules-workspace-runtime==0.3.125"


def test_rejects_untrusted_package_source_option(tmp_path):
    with pytest.raises(RequirementsContractError, match="package source"):
        _inspect(
            tmp_path,
            "--extra-index-url https://example.invalid/simple/\n"
            "molecules-workspace-runtime>=0.3\n",
        )


@pytest.mark.parametrize(
    "entry",
    (
        "--extra-index-url https://ci-user:credential-sentinel@example.invalid/simple/",
        "--requirement=https://ci-user:credential-sentinel@example.invalid/req.txt",
        "other-project @ https://ci-user:credential-sentinel@example.invalid/other.whl",
        "https://ci-user:credential-sentinel@example.invalid/not-a-wheel",
    ),
)
def test_rejected_sources_redact_embedded_credentials(tmp_path, entry):
    with pytest.raises(RequirementsContractError) as caught:
        _inspect(
            tmp_path,
            f"{entry}\nmolecules-workspace-runtime>=0.3\n",
        )

    message = str(caught.value)
    assert "credential-sentinel" not in message
    assert "ci-user" not in message
    assert "example.invalid" in message or "unsupported" in message


def test_rejects_duplicate_runtime_declarations(tmp_path):
    with pytest.raises(RequirementsContractError, match="exactly once"):
        _inspect(
            tmp_path,
            "molecules-workspace-runtime>=0.3\n"
            "molecules_workspace_runtime<0.4\n",
        )
