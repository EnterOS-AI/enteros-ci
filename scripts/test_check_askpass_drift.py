"""Unit tests for the molecule-askpass byte-identity drift gate.

Exercises the pure comparison logic offline (no network) and the fail-closed
contract. The negative control (a single-byte divergence MUST fail) is the
load-bearing assertion — it proves the gate can actually catch drift, not just
green a happy path.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-askpass-drift.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_askpass_drift", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_askpass_drift"] = module
    spec.loader.exec_module(module)
    return module


mod = _load()

_CANON = b"#!/bin/sh\n# molecule-askpass helper body\nexit 0\n"


def test_identical_copies_pass_and_return_shared_sha() -> None:
    blobs = {
        "molecule-ai/tpl-claude-code": _CANON,
        "molecule-ai/tpl-codex": _CANON,
        "molecule-ai/tpl-hermes": _CANON,
        "molecule-ai/tpl-openclaw": _CANON,
    }
    shared = mod.assert_identical(blobs)
    import hashlib

    assert shared == hashlib.sha256(_CANON).hexdigest()


def test_single_byte_divergence_fails() -> None:
    # NEGATIVE CONTROL: flip exactly one byte in one copy → must raise DriftError.
    drifted = bytearray(_CANON)
    drifted[-2] ^= 0x01  # one-byte change
    blobs = {
        "molecule-ai/tpl-claude-code": _CANON,
        "molecule-ai/tpl-codex": _CANON,
        "molecule-ai/tpl-hermes": bytes(drifted),
        "molecule-ai/tpl-openclaw": _CANON,
    }
    with pytest.raises(mod.DriftError):
        mod.assert_identical(blobs)


def test_empty_copy_fails_closed() -> None:
    blobs = {
        "molecule-ai/tpl-claude-code": _CANON,
        "molecule-ai/tpl-codex": b"",
    }
    with pytest.raises(mod.FetchError):
        mod.assert_identical(blobs)


def test_no_copies_fails_closed() -> None:
    with pytest.raises(mod.FetchError):
        mod.assert_identical({})


def test_fetch_tries_candidate_paths_and_falls_back(monkeypatch) -> None:
    # hermes-style: molecule-askpass 404s, git-askpass.sh resolves → returns bytes.
    import base64

    calls: list[str] = []

    def fake_api(method, path, *, query=None):
        calls.append(path)
        if path.endswith("molecule-askpass"):
            return ("not_found", None)
        if path.endswith("git-askpass.sh"):
            return ("ok", {"encoding": "base64",
                           "content": base64.b64encode(_CANON).decode()})
        return ("error", None)

    monkeypatch.setattr(mod, "api", fake_api)
    data = mod.fetch_askpass("molecule-ai/tpl-hermes")
    assert data == _CANON
    assert any("molecule-askpass" in c for c in calls)
    assert any("git-askpass.sh" in c for c in calls)


def test_fetch_forbidden_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(mod, "api", lambda *a, **k: ("forbidden", None))
    with pytest.raises(mod.FetchError):
        mod.fetch_askpass("molecule-ai/tpl-codex")


def test_fetch_all_missing_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(mod, "api", lambda *a, **k: ("not_found", None))
    with pytest.raises(mod.FetchError):
        mod.fetch_askpass("molecule-ai/tpl-openclaw")
