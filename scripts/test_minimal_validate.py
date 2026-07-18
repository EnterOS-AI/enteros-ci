from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).with_name("minimal_validate.py")


def _run(root: Path, *, readme_required: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["README_REQUIRED"] = "true" if readme_required else "false"
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_valid_repo_emits_execution_sentinel(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# valid\n")
    (tmp_path / "config.yml").write_text("enabled: true\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "minimal-validate:sentinel:executed" in result.stdout


def test_invalid_yaml_fails(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# invalid\n")
    (tmp_path / "broken.yml").write_text("key: [\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "YAML parse error: broken.yml" in result.stdout


def test_readme_requirement_is_explicit(tmp_path: Path) -> None:
    assert _run(tmp_path).returncode == 1
    assert _run(tmp_path, readme_required=False).returncode == 0


def test_manifest_requires_a_name(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# manifest\n")
    (tmp_path / "manifest.json").write_text('{"version":"1"}\n')
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "Manifest missing non-empty string 'name'" in result.stdout


def test_invalid_fixture_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# fixtures\n")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "intentionally-broken.yml").write_text("key: [\n")
    assert _run(tmp_path).returncode == 0
