"""Tests for private-only runtime artifact acquisition."""
from __future__ import annotations

import pytest

from install_workspace_dependencies import (
    build_download_command,
    build_install_command,
    select_runtime_wheel,
)
from requirements_contract import PRIVATE_INDEX_URL


def test_download_uses_only_private_index(tmp_path):
    command = build_download_command(
        "molecules-workspace-runtime==0.3.125",
        tmp_path,
        python="python3",
    )
    assert "--index-url" in command
    assert command[command.index("--index-url") + 1] == PRIVATE_INDEX_URL
    assert "--extra-index-url" not in command
    assert "--isolated" in command
    assert "--no-deps" in command
    assert "--only-binary=:all:" in command


def test_install_pins_local_runtime_wheel_with_public_requirements(tmp_path):
    wheel = tmp_path / "molecules_workspace_runtime-0.3.125-py3-none-any.whl"
    requirements = tmp_path / "requirements.txt"
    command = build_install_command(
        wheel,
        requirements,
        python="python3",
        break_system_packages=True,
    )
    assert str(wheel) in command
    assert command[-2:] == ["-r", str(requirements)]
    assert "--index-url" not in command
    assert "--extra-index-url" not in command
    assert "--isolated" in command


def test_select_runtime_wheel_requires_exactly_one_canonical_wheel(tmp_path):
    good = tmp_path / "molecules_workspace_runtime-0.3.125-py3-none-any.whl"
    good.touch()
    assert select_runtime_wheel(tmp_path) == good

    (tmp_path / "molecules_workspace_runtime-0.3.124-py3-none-any.whl").touch()
    with pytest.raises(RuntimeError, match="exactly one"):
        select_runtime_wheel(tmp_path)


def test_select_runtime_wheel_rejects_wrong_project(tmp_path):
    (tmp_path / "other_project-1.0-py3-none-any.whl").touch()
    with pytest.raises(RuntimeError, match="canonical runtime"):
        select_runtime_wheel(tmp_path)
