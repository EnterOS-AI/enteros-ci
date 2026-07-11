"""Tests for `scripts/lint_bp_context_emit_match.py` (the canonical copy).

The byte-identical `.molecule-ci/scripts/` mirror is enforced by
check-scripts-in-sync.sh; this suite imports the canonical `scripts/` copy.

Ported from molecule-core's `tests/test_lint_bp_context_emit_match.py`
(Tier 2f, internal#350). Structural enforcement of the BP⊆emitted
invariant: BP `status_check_contexts` must each have an emitter among
`.gitea/workflows/*.yml`.

Bidirectional rule:
  (a) BP-only: every context in `branch_protections/<branch>.status_check_contexts`
      must have at least one EMITTER — a workflow `name:` + job `name:` (or job key)
      + `pull_request` (or `push`) event that produces it. A BP context without
      an emitter blocks merges forever (Gitea treats absent-as-pending, NOT
      absent-as-skipped). This is the phantom-required-check / perma-block class
      (`feedback_phantom_required_check_after_gitea_migration`).

  (b) EMITTER-only: NO automatic flag. Tier 2g's job — a diff-based PR-time
      lint. This asserter runs scheduled/PR and would falsely flag every
      transitional state during a BP rollout. We only flag the BP-empty case in
      this direction as a NOTICE (informational), not as an error.

MODE switch (molecule-ci port):
  - MODE=issue (default): on mismatch, file/PATCH a `[ci-bp-drift]` issue.
  - MODE=assert: PR-time gate — assert BP⊆emitted, exit 1 on orphan, SKIP the
    issue-file path entirely (no token-write side effects on a PR).
  fail-closed-on-auth (exit 2) holds in BOTH modes.

Run:
    python3 -m pytest scripts/test_lint_bp_context_emit_match.py -v
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parent
    / "lint_bp_context_emit_match.py"
)


def _import_lint():
    spec = importlib.util.spec_from_file_location(
        f"lint_bp_emit_{os.getpid()}", SCRIPT_PATH
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def envset(tmp_path, monkeypatch):
    wf = tmp_path / ".gitea" / "workflows"
    wf.mkdir(parents=True)
    monkeypatch.setenv("WORKFLOWS_DIR", str(wf))
    monkeypatch.setenv("GITEA_TOKEN", "stub")
    monkeypatch.setenv("GITEA_HOST", "git.example.test")
    monkeypatch.setenv("REPO", "owner/molecule-ci")
    monkeypatch.setenv("BRANCH", "main")
    monkeypatch.setenv("DRIFT_LABEL", "ci-bp-drift")
    # Default the suite to the scheduled-sweep mode; MODE=assert tests
    # override this explicitly so the issue-path skip is exercised
    # deliberately, not by ambient env.
    monkeypatch.setenv("MODE", "issue")
    return wf


def _write_wf(d: Path, name: str, content: str) -> Path:
    p = d / name
    p.write_text(content)
    return p


def _stub_api(monkeypatch, lint_mod, bp_response, issue_search_response=None, posted_record=None):
    """Stub the module's `api` function.

    bp_response: ("ok", {"status_check_contexts": [...]})
                 or ("forbidden", None) / ("not_found", None)
    issue_search_response: list of issues matching the search query (
                           may be empty; default empty)
    posted_record: dict in which to record any POST/PATCH calls made
                   (so tests can assert idempotency / the assert-mode skip).
    """
    if issue_search_response is None:
        issue_search_response = []
    if posted_record is None:
        posted_record = {}

    def fake_api(method, path, *, body=None, query=None):
        if "branch_protections" in path:
            return bp_response
        if "issues/search" in path or "/issues?" in path or path.endswith("/issues"):
            if method == "GET":
                return ("ok", list(issue_search_response))
            if method == "POST":
                posted_record.setdefault("posts", []).append({"path": path, "body": body})
                return ("ok", {"number": 9001, "html_url": "http://t/9001"})
        if "/issues/" in path and method == "PATCH":
            posted_record.setdefault("patches", []).append({"path": path, "body": body})
            return ("ok", {"number": 9001})
        if "/labels" in path:
            return ("ok", [{"id": 10, "name": "ci-bp-drift"}, {"id": 9, "name": "ci-bp-drift"}])
        return ("ok", {})

    monkeypatch.setattr(lint_mod, "api", fake_api)
    return posted_record


# ---------------------------------------------------------------------------
# Perfect match — both sides agree.
# ---------------------------------------------------------------------------
def test_perfect_match_passes(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(
        monkeypatch,
        m,
        ("ok", {"status_check_contexts": ["CI / all-required (pull_request)"]}),
    )
    rc = m.run()
    assert rc == 0


# ---------------------------------------------------------------------------
# BP-only orphan — context with no emitter.
# ---------------------------------------------------------------------------
def test_bp_orphan_context_fails(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(
        monkeypatch,
        m,
        ("ok", {"status_check_contexts": [
            "CI / all-required (pull_request)",
            "Ghost workflow / ghost (pull_request)",  # the orphan
        ]}),
    )
    rc = m.run()
    assert rc == 1
    out = capsys.readouterr().out
    assert "Ghost workflow" in out or "ghost" in out.lower()


# ---------------------------------------------------------------------------
# Emitter-only direction → notice, not error (Tier 2g territory).
# ---------------------------------------------------------------------------
def test_emitter_orphan_only_warns(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "extra.yml",
        "name: Extra\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  extra-job:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(
        monkeypatch,
        m,
        ("ok", {"status_check_contexts": ["CI / all-required (pull_request)"]}),
    )
    rc = m.run()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Extra" in out or "extra" in out


# ---------------------------------------------------------------------------
# Multiple BP orphans — all surfaced.
# ---------------------------------------------------------------------------
def test_multiple_orphans_aggregated(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(
        monkeypatch,
        m,
        ("ok", {"status_check_contexts": [
            "CI / all-required (pull_request)",
            "Phantom A / a (pull_request)",
            "Phantom B / b (pull_request)",
        ]}),
    )
    rc = m.run()
    assert rc == 1
    out = capsys.readouterr().out
    assert "Phantom A" in out and "Phantom B" in out


# ---------------------------------------------------------------------------
# BP has zero contexts → nothing to lint, pass.
# ---------------------------------------------------------------------------
def test_bp_empty_lints_nothing(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(monkeypatch, m, ("ok", {"status_check_contexts": []}))
    rc = m.run()
    assert rc == 0


# ---------------------------------------------------------------------------
# API 403 — AUTH FAILURE → FAIL CLOSED (exit 2). A token that can't read
# BP must NOT green the lint — holds in MODE=issue AND MODE=assert.
# ---------------------------------------------------------------------------
def test_api_403_fails_closed(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  j:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(monkeypatch, m, ("forbidden", None))
    rc = m.run()
    assert rc == 2
    err = capsys.readouterr().err
    assert "403" in err or "scope" in err.lower() or "token" in err.lower()


def test_api_403_fails_closed_in_assert_mode(envset, monkeypatch, capsys):
    """Fail-closed-on-auth must hold in MODE=assert too (the PR-time gate)."""
    monkeypatch.setenv("MODE", "assert")
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  j:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(monkeypatch, m, ("forbidden", None))
    rc = m.run()
    assert rc == 2


# ---------------------------------------------------------------------------
# API transient/unexpected error → FAIL CLOSED (exit 2).
# ---------------------------------------------------------------------------
def test_api_transient_fails_closed(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  j:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(monkeypatch, m, ("error", None))
    rc = m.run()
    assert rc == 2


# ---------------------------------------------------------------------------
# Malformed workflow YAML → FAIL CLOSED (exit 2). A workflow that does not
# parse means the emitter inventory is INCOMPLETE; skipping it (the old
# `continue`) is fail-OPEN, because the remaining parsed workflows might
# happen to satisfy every BP-required context. The fixture deliberately
# pairs a malformed workflow with a VALID one that DOES emit the single
# BP-required context — so a fail-open implementation would green (exit 0).
# A correct fail-closed implementation returns 2 regardless. "Nothing
# fails open."
# ---------------------------------------------------------------------------
def test_malformed_workflow_yaml_fails_closed(envset, monkeypatch, capsys):
    # Valid workflow that, on its own, satisfies the BP-required context.
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    # Malformed workflow — unparseable YAML (bad indentation / dangling
    # mapping). yaml.safe_load raises yaml.YAMLError on this.
    _write_wf(
        envset,
        "broken.yml",
        "name: Broken\non:\n  pull_request:\n jobs:\n  : : :\n   - [unbalanced\n",
    )
    m = _import_lint()
    posted = _stub_api(
        monkeypatch,
        m,
        # BP requires only the context the VALID workflow emits, so a
        # fail-open (skip-and-continue) impl would exit 0 here.
        ("ok", {"status_check_contexts": ["CI / all-required (pull_request)"]}),
    )
    rc = m.run()
    assert rc == 2, (
        "malformed workflow YAML must fail closed (exit 2), even when the "
        "remaining valid workflows satisfy BP; got exit %r" % rc
    )
    err = capsys.readouterr().err
    assert "broken.yml" in err
    assert "parse" in err.lower()
    # Fail-closed happens before any issue side effect.
    assert not posted.get("posts"), f"no issue write on parse-fail; got {posted!r}"
    assert not posted.get("patches"), f"no issue write on parse-fail; got {posted!r}"


def test_malformed_workflow_yaml_fails_closed_in_assert_mode(envset, monkeypatch, capsys):
    """Fail-closed-on-parse-error must also hold in MODE=assert (PR-time gate)."""
    monkeypatch.setenv("MODE", "assert")
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    _write_wf(
        envset,
        "broken.yml",
        "name: Broken\non:\n  pull_request:\n jobs:\n  : : :\n   - [unbalanced\n",
    )
    m = _import_lint()
    _stub_api(
        monkeypatch,
        m,
        ("ok", {"status_check_contexts": ["CI / all-required (pull_request)"]}),
    )
    rc = m.run()
    assert rc == 2


# ---------------------------------------------------------------------------
# API 404 — authenticated absent resource (branch has no protection) →
# tolerated graceful skip (exit 0 with ::warning::), NOT a fail-open.
# ---------------------------------------------------------------------------
def test_api_404_skips_gracefully(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  j:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(monkeypatch, m, ("not_found", None))
    rc = m.run()
    assert rc == 0


# ---------------------------------------------------------------------------
# Event-suffix match strict: BP says (push), workflow emits (pull_request)
# only. Mismatch — flag.
# ---------------------------------------------------------------------------
def test_context_event_match_required(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(
        monkeypatch,
        m,
        ("ok", {"status_check_contexts": ["CI / all-required (push)"]}),
    )
    rc = m.run()
    assert rc == 1


# ---------------------------------------------------------------------------
# `pull_request_target` in workflow `on:` emits a `(pull_request)` context
# (Gitea convention — verified empirically on molecule-core).
# ---------------------------------------------------------------------------
def test_workflow_event_mapping_pull_request_target(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "secret.yml",
        "name: Secret scan\non:\n  pull_request_target:\n    branches: [main]\njobs:\n"
        "  scan:\n    runs-on: x\n    name: Scan diff for credential-shaped strings\n"
        "    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(
        monkeypatch,
        m,
        ("ok", {"status_check_contexts": [
            "Secret scan / Scan diff for credential-shaped strings (pull_request)",
        ]}),
    )
    rc = m.run()
    assert rc == 0


# ---------------------------------------------------------------------------
# Idempotency — existing open issue is PATCHed, not duplicated.
# (MODE=issue path.)
# ---------------------------------------------------------------------------
def test_idempotent_issue_filing(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    posted = _stub_api(
        monkeypatch,
        m,
        ("ok", {"status_check_contexts": [
            "CI / all-required (pull_request)",
            "Ghost / g (pull_request)",
        ]}),
        issue_search_response=[
            {
                "number": 4242,
                "title": "[ci-bp-drift] owner/molecule-ci/main: BP→emitter mismatch",
                "state": "open",
                "html_url": "http://t/4242",
            }
        ],
    )
    rc = m.run()
    assert rc == 1
    # Should have PATCHed, not POSTed a new one.
    assert posted.get("patches"), f"expected PATCH on existing issue; got {posted!r}"
    assert not posted.get("posts"), f"expected no POSTs; got {posted!r}"


# ---------------------------------------------------------------------------
# MODE=assert — on a BP orphan, exit 1 AND skip the issue-file path
# entirely (no POST, no PATCH). This is the PR-time gate behavior: no
# token-write side effects on a pull_request.
# ---------------------------------------------------------------------------
def test_assert_mode_orphan_exits_1_and_skips_issue(envset, monkeypatch, capsys):
    monkeypatch.setenv("MODE", "assert")
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  validate:\n    runs-on: x\n    name: Org template validation\n"
        "    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    posted = _stub_api(
        monkeypatch,
        m,
        # Mirrors the real molecule-ai-org-template-molecule-production
        # mismatch: BP requires `CI / all-required` but the CI workflow
        # only emits `CI / Org template validation`.
        ("ok", {"status_check_contexts": [
            "CI / all-required (pull_request)",
        ]}),
        # Even if an issue existed, assert-mode must not touch it.
        issue_search_response=[
            {
                "number": 1,
                "title": "[ci-bp-drift] owner/molecule-ci/main: BP→emitter mismatch",
                "state": "open",
                "html_url": "http://t/1",
            }
        ],
    )
    rc = m.run()
    assert rc == 1
    out = capsys.readouterr().out
    assert "all-required" in out
    assert "MODE=assert" in out
    # The defining property: NO issue write in assert mode.
    assert not posted.get("posts"), f"assert mode must not POST issues; got {posted!r}"
    assert not posted.get("patches"), f"assert mode must not PATCH issues; got {posted!r}"


# ---------------------------------------------------------------------------
# MODE=assert — clean match exits 0.
# ---------------------------------------------------------------------------
def test_assert_mode_clean_exits_0(envset, monkeypatch, capsys):
    monkeypatch.setenv("MODE", "assert")
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(
        monkeypatch,
        m,
        ("ok", {"status_check_contexts": ["CI / all-required (pull_request)"]}),
    )
    rc = m.run()
    assert rc == 0


# ---------------------------------------------------------------------------
# Invalid MODE → exit 2 (env contract violation).
# ---------------------------------------------------------------------------
def test_invalid_mode_fails_closed(envset, monkeypatch, capsys):
    monkeypatch.setenv("MODE", "bogus")
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  j:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(monkeypatch, m, ("ok", {"status_check_contexts": []}))
    rc = m.run()
    assert rc == 2


# ---------------------------------------------------------------------------
# GLOB semantics — Gitea matches status_check_contexts entries as globs.
# The `*` wildcard ("require every posted check" gate model,
# status_check_contexts=['*']) is satisfied by ANY emitter it matches and can
# NEVER be a phantom perma-pending context, so it must NOT be flagged as an
# orphan. Regression for the molecule-ci drift-gate going red on BP=['*'].
# ---------------------------------------------------------------------------
def test_bp_wildcard_star_satisfied_by_any_emitter(envset, monkeypatch, capsys):
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(monkeypatch, m, ("ok", {"status_check_contexts": ["*"]}))
    rc = m.run()
    assert rc == 0


def test_assert_mode_wildcard_star_exits_0(envset, monkeypatch, capsys):
    """The `*` wildcard must be clean in MODE=assert (the PR-time gate) too."""
    monkeypatch.setenv("MODE", "assert")
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(monkeypatch, m, ("ok", {"status_check_contexts": ["*"]}))
    rc = m.run()
    assert rc == 0


def test_bp_prefix_glob_matches_emitter(envset, monkeypatch, capsys):
    """A partial glob like `CI / *` is satisfied by any matching emitter."""
    _write_wf(
        envset,
        "ci.yml",
        "name: CI\non:\n  pull_request:\n    branches: [main]\njobs:\n"
        "  all-required:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
    )
    m = _import_lint()
    _stub_api(
        monkeypatch,
        m,
        ("ok", {"status_check_contexts": ["CI / * (pull_request)"]}),
    )
    rc = m.run()
    assert rc == 0


def test_bp_wildcard_star_still_orphan_when_no_emitter(envset, monkeypatch, capsys):
    """A `*` wildcard with NOTHING emitted has nothing to match -> orphan.

    Guards against the glob path turning into an unconditional fail-open: if a
    repo has zero emitters, even `*` is unsatisfied and must be flagged.
    """
    monkeypatch.setenv("MODE", "assert")
    # Workflows dir exists but contains no context-emitting workflow.
    _write_wf(envset, "empty.yml", "name: Empty\non: {}\njobs: {}\n")
    m = _import_lint()
    _stub_api(monkeypatch, m, ("ok", {"status_check_contexts": ["*"]}))
    rc = m.run()
    assert rc == 1
