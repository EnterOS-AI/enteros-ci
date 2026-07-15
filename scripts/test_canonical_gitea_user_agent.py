"""Regression tests for the Cloudflare-safe Gitea API User-Agent contract."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
API_CLIENTS = (
    ROOT / ".gitea" / "scripts" / "gitea-merge-queue.py",
    ROOT / "scripts" / "lint_bp_context_emit_match.py",
)


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return b"{}"


def _load(path: Path, monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "test-token")
    monkeypatch.setenv("GITEA_HOST", "git.moleculesai.app")
    monkeypatch.setenv("REPO", "molecule-ai/molecule-ci")
    monkeypatch.setenv("WATCH_BRANCH", "main")
    monkeypatch.setenv("QUEUE_LABEL", "merge-queue")
    name = f"canonical_ua_{path.stem.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("client_path", API_CLIENTS, ids=lambda path: path.name)
def test_every_urllib_gitea_client_sends_canonical_user_agent(client_path, monkeypatch):
    module = _load(client_path, monkeypatch)
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    module.api("GET", "/version")

    assert len(requests) == 1
    request, timeout = requests[0]
    headers = {key.casefold(): value for key, value in request.header_items()}
    assert headers["user-agent"] == "curl/8.4.0"
    assert timeout == 30
