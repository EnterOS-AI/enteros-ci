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


# ==========================================================================
# Checklist tiering — proportional gate (operator decision, 2026-07-25)
# ==========================================================================
#
# Two directions must hold, and BOTH are mutation-proved below:
#   * a genuinely trivial diff reaches the light tier, so the gate is
#     satisfiable rather than resented;
#   * a diff touching a reserved or code path CANNOT reach it, by any route.


def _load_mutant(source: str, name: str):
    """Exec a MUTATED copy of the gate as its own module.

    Used to prove a guard assertion is load-bearing: we break the property in
    the source, then assert the guard test actually goes red against the
    broken copy. A guard nobody has ever seen fail is not evidence.
    """
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(name, loader=None)
    )
    module.__dict__["__file__"] = str(GATE_PATH)
    exec(compile(source, "<mutant:" + name + ">", "exec"), module.__dict__)
    return module


# --- glob semantics --------------------------------------------------------


def test_globs_use_path_semantics_not_fnmatch(gate):
    """fnmatch's `*` crosses `/`, so `*.md` would match `deploy/rollout.md`.
    Permissive in exactly the direction that must never be permissive."""
    assert gate.path_matches("README.md", "*.md")
    assert not gate.path_matches("deploy/rollout.md", "*.md")
    assert gate.path_matches("deploy/rollout.md", "**/*.md")
    assert gate.path_matches("docs/a/b/c.md", "docs/**")
    assert gate.path_matches(".gitea/workflows/ci.yml", ".gitea/workflows/**")
    assert not gate.path_matches(
        ".gitea/sop-checklist-config.yaml", ".gitea/workflows/**"
    )
    # `**/` matches ZERO segments too, so a root-level file still matches.
    assert gate.path_matches("Dockerfile", "**/Dockerfile")


# --- direction 1: a trivial diff really does get the light tier ------------


def test_docs_only_pr_gets_the_light_tier(gate):
    tier, reason = gate.classify_tier(
        ["README.md", "docs/runbook.md"],
        gate.DEFAULT_RESERVED_PATHS,
        gate.DEFAULT_TRIVIAL_PATHS,
    )
    assert tier == gate.TIER_LIGHT
    assert "trivial-only" in reason


def test_upptime_monitor_repoint_gets_the_light_tier(gate):
    """The operator's canonical example: asking for a staging smoke test on a
    monitor repoint is how a gate gets worked around."""
    tier, _ = gate.classify_tier(
        [".upptimerc.yml"], gate.DEFAULT_RESERVED_PATHS, gate.DEFAULT_TRIVIAL_PATHS
    )
    assert tier == gate.TIER_LIGHT


def test_light_tier_applies_only_the_light_items(gate):
    items = [
        {"slug": s}
        for s in (
            "comprehensive-testing",
            "local-postgres-e2e",
            "staging-smoke",
            "root-cause",
            "five-axis-review",
            "no-backwards-compat",
            "memory-consulted",
        )
    ]
    applied, skipped, tier = gate.select_items(
        items, gate.TIER_LIGHT, gate.DEFAULT_LIGHT_ITEMS
    )
    assert tier == gate.TIER_LIGHT
    assert [it["slug"] for it in applied] == ["five-axis-review", "memory-consulted"]
    assert len(skipped) == 5
    # ... and the full tier still means every item.
    applied_full, skipped_full, tier_full = gate.select_items(
        items, gate.TIER_FULL, gate.DEFAULT_LIGHT_ITEMS
    )
    assert tier_full == gate.TIER_FULL
    assert len(applied_full) == 7 and skipped_full == []


# --- direction 2: reserved / code paths CANNOT be downgraded ---------------


def _assert_reserved_paths_force_the_full_tier(mod):
    """The load-bearing guard. Called by the real test AND by the mutation
    tests, so the two can never drift apart."""
    for path in (
        ".gitea/workflows/sop-checklist-gate.yml",
        ".gitea/scripts/gitea-merge-queue.py",
        ".gitea/sop-checklist-config.yaml",
        "cp/internal/migrations/0057_add_column.sql",
        "Dockerfile",
        "deploy/rollout.sh",
        "infra/k8s/tenant.yaml",
        "cp/internal/auth/session.go",
        "app/billing/stripe.ts",
        "cp/internal/provisioner/workspace.go",
    ):
        tier, reason = mod.classify_tier(
            [path], mod.DEFAULT_RESERVED_PATHS, mod.DEFAULT_TRIVIAL_PATHS
        )
        assert tier == mod.TIER_FULL, (path, tier, reason)
        assert "reserved path" in reason, (path, reason)


def test_reserved_paths_force_the_full_tier(gate):
    _assert_reserved_paths_force_the_full_tier(gate)


def test_one_reserved_file_poisons_an_otherwise_trivial_diff(gate):
    """Line count is not an input: one line in a migration is not trivial."""
    tier, reason = gate.classify_tier(
        ["README.md", "docs/a.md", "cp/migrations/0058_x.sql"],
        gate.DEFAULT_RESERVED_PATHS,
        gate.DEFAULT_TRIVIAL_PATHS,
    )
    assert tier == gate.TIER_FULL
    assert "0058_x.sql" in reason


def test_code_paths_are_full_tier_even_without_a_reserved_match(gate):
    for path in ("components/Nav.tsx", "lib/site.ts", "site/app.js", "main.go"):
        tier, reason = gate.classify_tier(
            [path], gate.DEFAULT_RESERVED_PATHS, gate.DEFAULT_TRIVIAL_PATHS
        )
        assert tier == gate.TIER_FULL, (path, tier)
        assert "non-trivial path" in reason


def test_reserved_beats_trivial_when_a_path_matches_both(gate):
    """`docs/**` says trivial, `**/deploy/**` says reserved. Reserved wins —
    this is the ordering the whole downgrade-proof rests on."""
    tier, reason = gate.classify_tier(
        ["docs/deploy/runbook.md"],
        gate.DEFAULT_RESERVED_PATHS,
        gate.DEFAULT_TRIVIAL_PATHS,
    )
    assert tier == gate.TIER_FULL
    assert "reserved path" in reason


def test_an_over_broad_trivial_glob_cannot_downgrade_a_reserved_path(gate):
    """A consumer repo writing `trivial_paths: ['**']` still cannot wave a
    workflow edit through."""
    tier, reason = gate.classify_tier(
        [".gitea/workflows/deploy.yml"], gate.DEFAULT_RESERVED_PATHS, ["**"]
    )
    assert tier == gate.TIER_FULL
    assert "reserved path" in reason


def test_a_consumer_config_cannot_narrow_the_reserved_set(gate):
    """`reserved_paths` in a repo config is UNION-ed with the SSOT defaults,
    never substituted for them."""
    reserved, trivial, light, _max = gate.load_tiering(
        {"tiering": {"reserved_paths": ["custom/**"]}}
    )
    assert "custom/**" in reserved
    for default in gate.DEFAULT_RESERVED_PATHS:
        assert default in reserved
    assert trivial == list(gate.DEFAULT_TRIVIAL_PATHS)
    assert light == list(gate.DEFAULT_LIGHT_ITEMS)


def test_renamed_files_are_classified_on_both_paths(gate, monkeypatch):
    """Moving a file OUT of a reserved directory is a reserved-path change."""
    payload = json.dumps(
        [
            {
                "filename": "docs/old-workflow.md",
                "previous_filename": ".gitea/workflows/deploy.yml",
            }
        ]
    ).encode()
    pages = [payload, b"[]"]

    def fake_urlopen(request, timeout=None):
        return _Response(pages.pop(0) if pages else b"[]")

    monkeypatch.setattr(gate.urllib.request, "urlopen", fake_urlopen)
    client = gate.GiteaClient("git.moleculesai.app", "t")
    paths, truncated = client.get_pr_files("o", "r", 1, 300)
    assert not truncated
    assert ".gitea/workflows/deploy.yml" in paths
    tier, _ = gate.classify_tier(
        paths, gate.DEFAULT_RESERVED_PATHS, gate.DEFAULT_TRIVIAL_PATHS
    )
    assert tier == gate.TIER_FULL


# --- fail-closed -----------------------------------------------------------


def test_tier_fails_closed_without_a_readable_file_list(gate):
    for paths, truncated in (([], False), (None, False), (["README.md"], True)):
        tier, reason = gate.classify_tier(
            paths,
            gate.DEFAULT_RESERVED_PATHS,
            gate.DEFAULT_TRIVIAL_PATHS,
            truncated=truncated,
        )
        assert tier == gate.TIER_FULL, (paths, truncated)
        assert "fail-closed" in reason


def test_files_api_error_fails_closed(gate, monkeypatch):
    monkeypatch.setattr(
        gate.urllib.request,
        "urlopen",
        lambda request, timeout=None: _Response(b"{}", 403),
    )
    client = gate.GiteaClient("git.moleculesai.app", "t")
    paths, truncated = client.get_pr_files("o", "r", 1, 300)
    assert truncated is True and paths == []


def test_too_many_changed_files_fails_closed(gate, monkeypatch):
    big = json.dumps([{"filename": "docs/f%d.md" % i} for i in range(100)]).encode()

    monkeypatch.setattr(
        gate.urllib.request, "urlopen", lambda request, timeout=None: _Response(big)
    )
    client = gate.GiteaClient("git.moleculesai.app", "t")
    _paths, truncated = client.get_pr_files("o", "r", 1, max_files=50)
    assert truncated is True


def test_a_light_tier_that_would_check_nothing_escalates_to_full(gate):
    """A vacuously-passing gate is worse than a heavy one."""
    items = [{"slug": "comprehensive-testing"}, {"slug": "staging-smoke"}]
    applied, skipped, tier = gate.select_items(
        items, gate.TIER_LIGHT, ["five-axis-review"]
    )
    assert tier == gate.TIER_FULL
    assert len(applied) == 2 and skipped == []


# --- tier visibility -------------------------------------------------------


def test_tier_is_disclosed_in_the_status_description_and_a_sentinel(gate):
    source = GATE_PATH.read_text(encoding="utf-8")
    assert gate.TIER_SENTINEL_PREFIX == "sop-checklist:tier:"
    # Printed unconditionally, after the execution sentinel.
    assert 'print(f"{TIER_SENTINEL_PREFIX}{tier}:{tier_reason}")' in source
    # And carried on the status row itself, not only in the job log.
    assert 'description = f"[tier:{tier}] {description}"' in source
    assert source.index("print(SENTINEL)") < source.index("TIER_SENTINEL_PREFIX}{tier}")


def test_consumer_template_asserts_the_tier_disclosure(gate):
    """A run that decided a tier without SAYING so must not be able to pass."""
    template = (ROOT / "templates" / "ci-sop-checklist-gate.yml").read_text(
        encoding="utf-8"
    )
    assert "sop-checklist:sentinel:executed" in template
    assert "sop-checklist:tier:" in template


# --- MUTATION PROOF: the guards above are load-bearing ---------------------


def test_mutation_disabling_the_reserved_check_turns_the_guard_red(gate):
    """NEGATIVE CONTROL.

    Break the classifier so a reserved path stops forcing the full tier, then
    prove `_assert_reserved_paths_force_the_full_tier` — the exact assertion
    the real test runs — goes RED against the broken copy. Without this, that
    test could be passing vacuously and nobody would know.
    """
    source = GATE_PATH.read_text(encoding="utf-8")
    target = '            return TIER_FULL, f"reserved path {path} (~ {hit})"'
    assert target in source, "mutation target moved — this negative control is stale"
    mutant_source = source.replace(
        target, "            pass  # MUTANT: reserved-path check disabled"
    )
    mutant = _load_mutant(mutant_source, "sop_gate_mutant_reserved")

    # The mutant genuinely mis-classifies: a reserved+trivial overlap that the
    # real module calls `full` now falls through to the trivial allowlist.
    assert (
        mutant.classify_tier(
            ["docs/deploy/runbook.md"],
            mutant.DEFAULT_RESERVED_PATHS,
            mutant.DEFAULT_TRIVIAL_PATHS,
        )[0]
        == mutant.TIER_LIGHT
    )
    assert (
        gate.classify_tier(
            ["docs/deploy/runbook.md"],
            gate.DEFAULT_RESERVED_PATHS,
            gate.DEFAULT_TRIVIAL_PATHS,
        )[0]
        == gate.TIER_FULL
    )

    # ... and the shipped guard assertion FAILS on it.
    with pytest.raises(AssertionError):
        _assert_reserved_paths_force_the_full_tier(mutant)


def _assert_a_mixed_diff_is_never_downgraded(mod):
    """The other load-bearing guard: LIGHT requires EVERY path to be trivial,
    not merely SOME path. `docs/` next to a code file is a code change."""
    for paths in (
        ["README.md", "components/Nav.tsx"],
        ["docs/a.md", "cp/internal/server.go"],
        ["CHANGELOG.md", "site/app.js"],
    ):
        tier, _reason = mod.classify_tier(
            paths, mod.DEFAULT_RESERVED_PATHS, mod.DEFAULT_TRIVIAL_PATHS
        )
        assert tier == mod.TIER_FULL, (paths, tier)


def test_a_mixed_docs_plus_code_diff_is_never_downgraded(gate):
    _assert_a_mixed_diff_is_never_downgraded(gate)


def test_mutation_any_trivial_instead_of_all_trivial_turns_the_guard_red(gate):
    """NEGATIVE CONTROL for the ALL-vs-ANY quantifier — the single most
    plausible way to mis-write this classifier, and the one that would wave a
    real code change through behind a README edit."""
    source = GATE_PATH.read_text(encoding="utf-8")
    target = (
        "    for path in changed_paths:\n"
        "        if first_match(path, trivial_globs) is None:\n"
        '            return TIER_FULL, f"non-trivial path {path}"\n'
    )
    assert target in source, "mutation target moved — this negative control is stale"
    mutant_source = source.replace(
        target,
        "    for path in changed_paths:\n"
        "        if first_match(path, trivial_globs) is not None:\n"
        '            return TIER_LIGHT, "MUTANT: any-trivial"\n',
        1,
    )
    mutant = _load_mutant(mutant_source, "sop_gate_mutant_any_trivial")

    # The mutant downgrades a diff that is mostly code because ONE file is a
    # README ...
    assert (
        mutant.classify_tier(
            ["README.md", "components/Nav.tsx"],
            mutant.DEFAULT_RESERVED_PATHS,
            mutant.DEFAULT_TRIVIAL_PATHS,
        )[0]
        == mutant.TIER_LIGHT
    )
    # ... and the shipped guard assertion FAILS on it.
    with pytest.raises(AssertionError):
        _assert_a_mixed_diff_is_never_downgraded(mutant)
    # The real module still refuses.
    _assert_a_mixed_diff_is_never_downgraded(gate)


def test_mutation_letting_a_consumer_replace_reserved_turns_the_guard_red(gate):
    """NEGATIVE CONTROL for `load_tiering`: if a consumer config could
    SUBSTITUTE reserved_paths instead of adding to them, a repo could opt its
    own deploy workflow out of the full checklist."""
    source = GATE_PATH.read_text(encoding="utf-8")
    target = (
        "    reserved = list(DEFAULT_RESERVED_PATHS) + [\n"
        '        str(x) for x in (t.get("reserved_paths") or []) if str(x).strip()\n'
        "    ]"
    )
    assert target in source, "mutation target moved — this negative control is stale"
    mutant_source = source.replace(
        target,
        "    reserved = [\n"
        '        str(x) for x in (t.get("reserved_paths") or []) if str(x).strip()\n'
        "    ] or list(DEFAULT_RESERVED_PATHS)",
    )
    mutant = _load_mutant(mutant_source, "sop_gate_mutant_narrow")

    cfg = {"tiering": {"reserved_paths": ["nothing-real/**"], "trivial_paths": ["**"]}}
    m_reserved, m_trivial, _l, _m = mutant.load_tiering(cfg)
    assert (
        mutant.classify_tier([".gitea/workflows/deploy.yml"], m_reserved, m_trivial)[0]
        == mutant.TIER_LIGHT
    )

    r_reserved, r_trivial, _l2, _m2 = gate.load_tiering(cfg)
    assert (
        gate.classify_tier([".gitea/workflows/deploy.yml"], r_reserved, r_trivial)[0]
        == gate.TIER_FULL
    )
