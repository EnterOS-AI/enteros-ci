"""Capability-preservation tests for the consolidated sop-checklist gate SSOT.

`scripts/sop_checklist_gate.py` replaces three hand-maintained copies
(molecule-app, molecule-ai-status, molecules-market) that had drifted in both
directions. Each test below pins a capability that existed in at least one of
those copies, so a future edit cannot silently drop it again — which is exactly
how the drift happened the first time.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "sop_checklist_gate.py"


@pytest.fixture(scope="module")
def gate():
    os.environ.setdefault("SOP_CHECKLIST_NO_RLIMIT", "1")
    spec = importlib.util.spec_from_file_location("sop_checklist_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sop_checklist_gate"] = module
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload=b"{}", code=200):
        self._payload = payload
        self._code = code

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._payload

    def getcode(self):
        return self._code


def _capture(gate, monkeypatch, payload=b"{}", code=200):
    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append((request, timeout))
        return _Response(payload, code)

    monkeypatch.setattr(gate.urllib.request, "urlopen", fake_urlopen)
    return seen


# --------------------------------------------------------------------------
# Cloudflare / transport contract (was: molecule-app only)
# --------------------------------------------------------------------------


def test_canonical_user_agent_matches_the_org_constant(gate):
    """The org already ratchets this exact value for every other urllib
    Gitea client (scripts/test_canonical_gitea_user_agent.py). One constant."""
    assert gate.CANONICAL_GITEA_USER_AGENT == "curl/8.4.0"


def test_never_sends_the_default_python_urllib_user_agent(gate, monkeypatch):
    """Regression for the 2026-07-25 outage: Cloudflare Browser Integrity
    Check 403s `Python-urllib/*` with error 1010 browser_signature_banned,
    which took a REQUIRED merge gate red on two repos."""
    seen = _capture(gate, monkeypatch)
    client = gate.GiteaClient("git.moleculesai.app", "t")
    client._req("GET", "/version")

    (request, _timeout), = seen
    headers = {k.casefold(): v for k, v in request.header_items()}
    assert headers["user-agent"] == gate.CANONICAL_GITEA_USER_AGENT
    assert "python-urllib" not in headers["user-agent"].casefold()


def test_every_verb_carries_the_user_agent(gate, monkeypatch):
    """POST /statuses must not regress separately from the GETs."""
    seen = _capture(gate, monkeypatch)
    client = gate.GiteaClient("git.moleculesai.app", "t")
    client.post_status(
        "o", "r", "0" * 40, state="success", context="c", description="d"
    )

    (request, _timeout), = seen
    headers = {k.casefold(): v for k, v in request.header_items()}
    assert headers["user-agent"] == gate.CANONICAL_GITEA_USER_AGENT
    assert request.get_method() == "POST"


# --------------------------------------------------------------------------
# API base is configurable and separate from the public host
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "api_base,expected",
    [
        ("", "https://git.moleculesai.app/api/v1"),
        (None, "https://git.moleculesai.app/api/v1"),
        ("gitea.mesh.moleculesai.app", "https://gitea.mesh.moleculesai.app/api/v1"),
        ("http://gitea.mesh.moleculesai.app", "http://gitea.mesh.moleculesai.app/api/v1"),
        ("http://gitea.mesh.moleculesai.app/", "http://gitea.mesh.moleculesai.app/api/v1"),
    ],
)
def test_resolve_api_base(gate, api_base, expected):
    assert gate.resolve_api_base(api_base, "git.moleculesai.app") == expected


def test_status_target_url_stays_on_the_public_host(gate):
    """Moving the API off the CDN edge must not turn the status link into a
    private URL a human cannot open."""
    source = GATE_PATH.read_text(encoding="utf-8")
    assert 'target_url = f"https://{args.gitea_host}/' in source
    assert "target_url = f\"{args.api_base}" not in source


# --------------------------------------------------------------------------
# OOM guardrail (was: molecule-ai-status only) — task #369
# --------------------------------------------------------------------------


def test_comment_pagination_yields_minimal_dicts(gate, monkeypatch):
    fat = {
        "user": {"login": "bob", "avatar_url": "x" * 500},
        "body": "/sop-ack 1",
        "html_url": "y" * 500,
        "assets": ["z"] * 50,
    }
    pages = [[fat], []]

    def fake_req(self, method, path, body=None, ok_codes=(200, 201, 204)):
        return 200, pages.pop(0)

    monkeypatch.setattr(gate.GiteaClient, "_req", fake_req)
    client = gate.GiteaClient("h", "t")
    out = client.get_issue_comments("o", "r", 1)

    assert out == [{"user": {"login": "bob"}, "body": "/sop-ack 1"}]


def test_comment_body_is_capped(gate, monkeypatch):
    huge = {"user": {"login": "bob"}, "body": "a" * (gate._MAX_BODY_BYTES + 5000)}
    pages = [[huge], []]

    def fake_req(self, method, path, body=None, ok_codes=(200, 201, 204)):
        return 200, pages.pop(0)

    monkeypatch.setattr(gate.GiteaClient, "_req", fake_req)
    client = gate.GiteaClient("h", "t")
    out = client.get_issue_comments("o", "r", 1)

    assert len(out[0]["body"]) == gate._MAX_BODY_BYTES


def test_max_comments_cap_stops_pagination(gate, monkeypatch):
    page = [{"user": {"login": "bob"}, "body": "hi"} for _ in range(50)]

    def fake_req(self, method, path, body=None, ok_codes=(200, 201, 204)):
        return 200, list(page)

    monkeypatch.setattr(gate.GiteaClient, "_req", fake_req)
    client = gate.GiteaClient("h", "t")
    out = client.get_issue_comments("o", "r", 1, max_comments=75)

    assert len(out) == 75


def test_rlimit_guardrail_is_still_wired(gate):
    source = GATE_PATH.read_text(encoding="utf-8")
    assert "RLIMIT_AS" in source
    assert "SOP_CHECKLIST_NO_RLIMIT" in source
    assert "volume-skipped" in source


# --------------------------------------------------------------------------
# Ack semantics (shared by all three copies) + trusted-acker fallback
# (was: molecule-ai-status only)
# --------------------------------------------------------------------------


ITEMS = {
    "comprehensive-testing": {
        "slug": "comprehensive-testing",
        "required_teams": ["qa"],
        "numeric_alias": 1,
        "pr_section_marker": "Comprehensive testing performed",
    }
}


def _ack_state(gate, comments, author, probe, trusted=None):
    return gate.compute_ack_state(
        comments,
        author,
        {k: dict(v) for k, v in ITEMS.items()},
        {1: "comprehensive-testing"},
        probe,
        trusted_ackers=trusted,
    )


def test_author_self_ack_is_rejected(gate):
    state = _ack_state(
        gate,
        [{"user": {"login": "alice"}, "body": "/sop-ack comprehensive-testing"}],
        "alice",
        lambda slug, users: (users, []),
    )
    assert state["comprehensive-testing"]["ackers"] == []


def test_revoke_then_reack_restores(gate):
    comments = [
        {"user": {"login": "bob"}, "body": "/sop-ack 1"},
        {"user": {"login": "bob"}, "body": "/sop-revoke 1"},
        {"user": {"login": "bob"}, "body": "/sop-ack 1"},
    ]
    state = _ack_state(gate, comments, "alice", lambda slug, users: (users, []))
    assert state["comprehensive-testing"]["ackers"] == ["bob"]


def test_unverifiable_acker_accepted_only_when_trusted(gate):
    comments = [{"user": {"login": "reviewbot"}, "body": "/sop-ack 1"}]
    probe = lambda slug, users: ([], list(users))  # noqa: E731 - every probe 403'd

    untrusted = _ack_state(gate, comments, "alice", probe)
    assert untrusted["comprehensive-testing"]["ackers"] == []

    trusted = _ack_state(gate, comments, "alice", probe, trusted=["reviewbot"])
    assert trusted["comprehensive-testing"]["ackers"] == ["reviewbot"]


# --------------------------------------------------------------------------
# OPERATOR RELAX (was: molecule-app only)
# --------------------------------------------------------------------------


def test_relax_block_runs_after_the_volume_guardrail(gate):
    """Ordering matters: if the #369 volume-skip ran after the relax it would
    re-redden an already-relaxed result. molecule-app and molecule-ai-status
    each only had one of the two blocks, so the ordering was never expressed."""
    source = GATE_PATH.read_text(encoding="utf-8")
    volume_branch = source.index("if volume_skipped:")
    relax_branch = source.index(
        'require_ack = (\n        os.environ.get("MERGE_REQUIRE_SOP_ACK"'
    )
    assert volume_branch < relax_branch


def test_relax_is_opt_out_via_the_org_variable(gate):
    source = GATE_PATH.read_text(encoding="utf-8")
    # Enforcement is restored by setting the org var to 'true' — no code edit.
    assert 'os.environ.get("MERGE_REQUIRE_SOP_ACK", "").strip().lower() == "true"' in source
    assert "ack advisory until agent-team" in source


# --------------------------------------------------------------------------
# 403-tolerant status POST (was: molecule-ai-status only)
# --------------------------------------------------------------------------


def test_status_post_403_is_tolerated_only_on_success(gate):
    source = GATE_PATH.read_text(encoding="utf-8")
    assert 'if state == "success" and "HTTP 403" in str(exc):' in source
    assert "raise" in source


# --------------------------------------------------------------------------
# Drift guard: no consumer may vendor a copy of this file.
# --------------------------------------------------------------------------


def test_ssot_declares_itself_the_single_source(gate):
    source = GATE_PATH.read_text(encoding="utf-8")
    assert "THIS FILE IS THE SSOT" in source
    assert "must NOT vendor a copy" in source


def test_render_status_needs_every_item_acked(gate):
    items = [ITEMS["comprehensive-testing"]]
    acked = {"comprehensive-testing": {"ackers": ["bob"]}}
    unacked = {"comprehensive-testing": {"ackers": []}}

    state, _ = gate.render_status(items, acked, {"comprehensive-testing": True})
    assert state == "success"

    state, desc = gate.render_status(items, unacked, {"comprehensive-testing": False})
    assert state == "failure"
    assert "body-unfilled" in desc
