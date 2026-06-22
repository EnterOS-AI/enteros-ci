"""Tests for validate-plugin.py — pin the plugin base-contract gate.

validate-plugin.py runs all its checks at module top level and calls
sys.exit(), so (unlike the import-safe workspace/org validators) it is
exercised as a subprocess against a materialised plugin dir — which
also tests the exact entrypoint CI invokes (`python3 validate-plugin.py`
with cwd = the plugin repo root).

Contract pinned here, with the kind-aware content check (RFC internal#476
P1 — recognise code-class plugins like kind: env-mutator whose content is
go.mod + entrypoint, not SKILL.md/hooks/skills/rules). Regression guard
for the false positive that red-flagged molecule-gh-identity.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml


VALIDATOR_PATH = Path(__file__).resolve().parent / "validate-plugin.py"


def _run(plugin_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VALIDATOR_PATH)],
        cwd=plugin_dir,
        capture_output=True,
        text=True,
    )


def _write_plugin_yaml(plugin_dir: Path, data: dict) -> None:
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(data))


def _base_manifest(**overrides) -> dict:
    data = {
        "name": "test-plugin",
        "version": "1.0.0",
        "description": "a test plugin",
    }
    data.update(overrides)
    return data


# --- skill-class plugins -------------------------------------------------

def test_skill_plugin_with_skill_md_passes(tmp_path):
    _write_plugin_yaml(tmp_path, _base_manifest())
    (tmp_path / "SKILL.md").write_text("# Test Plugin\n")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_skill_plugin_with_skills_dir_passes(tmp_path):
    _write_plugin_yaml(tmp_path, _base_manifest())
    (tmp_path / "skills").mkdir()
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_skill_plugin_with_no_content_fails(tmp_path):
    _write_plugin_yaml(tmp_path, _base_manifest())
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "at least one of: SKILL.md" in r.stdout


# --- code-class plugins (kind: env-mutator) ------------------------------

def test_env_mutator_with_go_and_entrypoint_passes(tmp_path):
    """The molecule-gh-identity shape: a Go env-mutator with no skill
    markers must validate via go.mod + entrypoint, not be red-flagged."""
    _write_plugin_yaml(
        tmp_path,
        _base_manifest(kind="env-mutator", entrypoint="pluginloader.BuildRegistry"),
    )
    (tmp_path / "go.mod").write_text("module example.com/test\n\ngo 1.25\n")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_env_mutator_missing_go_mod_fails(tmp_path):
    """`kind:` alone must not let an empty repo pass — code content
    (go.mod) is still required."""
    _write_plugin_yaml(
        tmp_path,
        _base_manifest(kind="env-mutator", entrypoint="pluginloader.BuildRegistry"),
    )
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "go.mod" in r.stdout


def test_env_mutator_missing_entrypoint_fails(tmp_path):
    _write_plugin_yaml(tmp_path, _base_manifest(kind="env-mutator"))
    (tmp_path / "go.mod").write_text("module example.com/test\n\ngo 1.25\n")
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "entrypoint" in r.stdout


def test_env_mutator_with_skill_md_also_passes(tmp_path):
    """A code-class plugin that also ships a SKILL.md is fine."""
    _write_plugin_yaml(tmp_path, _base_manifest(kind="env-mutator"))
    (tmp_path / "SKILL.md").write_text("# Test\n")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


# --- required-field / shape checks (unchanged contract) ------------------

def test_missing_plugin_yaml_fails(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "plugin.yaml not found" in r.stdout


def test_missing_required_field_fails(tmp_path):
    data = _base_manifest()
    del data["description"]
    data["kind"] = "env-mutator"
    data["entrypoint"] = "x"
    _write_plugin_yaml(tmp_path, data)
    (tmp_path / "go.mod").write_text("module x\n")
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "Missing required field: description" in r.stdout


def test_invalid_version_fails(tmp_path):
    _write_plugin_yaml(
        tmp_path, _base_manifest(version="1.0.0-beta", kind="env-mutator", entrypoint="x")
    )
    (tmp_path / "go.mod").write_text("module x\n")
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "Invalid version format" in r.stdout


def test_runtimes_must_be_list(tmp_path):
    _write_plugin_yaml(
        tmp_path,
        _base_manifest(kind="env-mutator", entrypoint="x", runtimes="claude_code"),
    )
    (tmp_path / "go.mod").write_text("module x\n")
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "runtimes must be a list" in r.stdout
