"""Unit tests for the repo-meta presence gate.

Stubs the per-repo fetch so the acceptance logic is exercised without the
network. Negative-controlled: a missing manifest → 1, an unverifiable repo → 2,
all-present → 0.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def _import():
    spec = importlib.util.spec_from_file_location(
        "check_repo_meta_presence", _HERE / "check_repo_meta_presence.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VALID = "schema_version: 1\nlayer: service\n"


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "stub")
    monkeypatch.setenv("GITEA_HOST", "git.example.test")
    monkeypatch.setenv("ORG", "molecule-ai")
    return monkeypatch


def _stub(monkeypatch, mod, table):
    """table: repo -> (status, raw). Any repo absent from table defaults ok/VALID."""

    def fake_fetch(api, org, repo, token):
        return table.get(repo, ("ok", VALID))

    monkeypatch.setattr(mod, "fetch_repo_meta", fake_fetch)


def test_all_present_passes(env, monkeypatch):
    m = _import()
    _stub(monkeypatch, m, {})  # everything ok/VALID
    assert m.run() == 0


def test_one_missing_fails(env, monkeypatch, capsys):
    m = _import()
    _stub(monkeypatch, m, {"molecule-core": ("missing", None)})
    assert m.run() == 1
    assert "molecule-core" in capsys.readouterr().out


def test_invalid_manifest_fails(env, monkeypatch, capsys):
    m = _import()
    _stub(monkeypatch, m, {"molecule-ci": ("ok", "schema_version: 2\nlayer: nope\n")})
    assert m.run() == 1
    out = capsys.readouterr().out
    assert "molecule-ci" in out


def test_unverifiable_repo_fails_closed(env, monkeypatch):
    m = _import()
    _stub(monkeypatch, m, {"molecule-ai-sdk": ("error", None)})
    assert m.run() == 2  # cannot verify → fail closed, never green


def test_sentinel_always_printed(env, monkeypatch, capsys):
    m = _import()
    _stub(monkeypatch, m, {"molecule-core": ("missing", None)})
    m.run()
    assert "repo-meta-presence:executed" in capsys.readouterr().out
