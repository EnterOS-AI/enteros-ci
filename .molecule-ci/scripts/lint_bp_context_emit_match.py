#!/usr/bin/env python3
"""lint_bp_context_emit_match — BP⊆emitted asserter (ported from molecule-core).

Ported into molecule-ci's `.molecule-ci/scripts/` SSOT from
`molecule-core/.gitea/scripts/lint_bp_context_emit_match.py` (Tier 2f,
internal#350). The canonical-name/ci.yml-template renaming
(CI/validate standardization) is a planned follow-up — this file is the
foundational port only.

Rule
----
For a given protected branch, every context in
`branch_protections/<branch>.status_check_contexts` MUST be emitted
by at least one workflow in `.gitea/workflows/*.yml`. Two contexts
match when:

  1. The workflow's `name:` equals the context's workflow-part (the
     prefix before ` / `).
  2. Some job in that workflow has a `name:` (or default-fallback
     job-key) equal to the context's job-part (between ` / ` and
     ` (`).
  3. The workflow's `on:` block includes the context's event-part
     (in parens at the end), with Gitea's event-name mapping:
        - `pull_request` and `pull_request_target` BOTH emit
          `(pull_request)` contexts (verified empirically on
          molecule-core/main).
        - `push` emits `(push)`.

A BP context with no emitter blocks merges forever — Gitea treats
absent-as-`pending`, NOT absent-as-`skipped`-as-`success`. This is
the phantom-required-check / perma-block class
(`feedback_phantom_required_check_after_gitea_migration`): a required
context with no emitter is perma-pending → the merge endpoint returns
HTTP 405 "try again later" forever.

The inverse direction (emitter without BP context) is INFORMATIONAL
only — Tier 2g handles that direction at PR-time. Flagging it here
on a daily schedule would falsely surface every transitional state
during a BP rollout.

MODE switch (added in the molecule-ci port)
-------------------------------------------
  MODE=assert  — PR-time / gate use. Assert BP-required ⊆ emitted for
                 the current repo and exit 1 with `::error::` on any
                 orphan. SKIP the issue-file/PATCH path entirely (no
                 token-write side effects on a PR). Suitable for a
                 `pull_request` job. This is what the advisory
                 `bp-context-drift-gate.yml` workflow runs.
  MODE=issue   — (default) the scheduled-sweep behavior: file/update a
                 `ci-bp-drift` issue on mismatch, close it when clean.

FAIL-CLOSED-ON-AUTH (exit 2) holds in BOTH modes: a token that cannot
read branch protections, or a transient/unexpected API error, MUST NOT
green the gate — it has not verified the invariant. The advisory
WORKFLOW wraps this script in `continue-on-error: true` so it never
blocks a merge YET; the SCRIPT stays fail-closed so it is already
correct when later promoted to a required gate.

Exit codes
----------
  0 — clean, OR an authenticated 404 (branch genuinely has no
      protection — surfaces ::warning::, not a fail-open).
  1 — at least one BP context has no emitter.
  2 — env contract violation, workflows-dir missing, YAML parse
      error, OR a fail-closed verification failure: 401/403 auth
      failure (token can't read BP) or transient/unexpected API
      error. The script MUST NOT green when it cannot verify.

Env
---
  GITEA_TOKEN     — DRIFT_BOT_TOKEN (repo-admin for branch_protections)
  GITEA_HOST      — e.g. git.moleculesai.app
  REPO            — owner/name
  BRANCH          — defaults to `main`
  WORKFLOWS_DIR   — defaults to `.gitea/workflows`
  DRIFT_LABEL     — defaults to `ci-bp-drift`
  MODE            — `assert` | `issue` (default `issue`)

Memory cross-links
------------------
  - internal#350 (the RFC that specs this lint)
  - feedback_phantom_required_check_after_gitea_migration
  - feedback_label_ids_are_per_repo
  - reference_post_suspension_pipeline
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "::error::PyYAML is required. Install with: pip install PyYAML\n"
    )
    sys.exit(2)


# Status-check context regex (mirrors lint-required-no-paths.py).
_CONTEXT_RE = re.compile(
    r"^(?P<workflow>.+?) / (?P<job>.+) \((?P<event>[^)]+)\)$"
)

# Map a workflow `on:` event-key to the context's event-part. Gitea's
# emitter convention (verified on molecule-core):
#   - pull_request          → `(pull_request)`
#   - pull_request_target   → `(pull_request)` (same surface)
#   - push                  → `(push)`
#   - schedule              → no PR status; scheduled runs don't post
#     commit-statuses unless the workflow itself does so explicitly.
#   - workflow_dispatch     → manually dispatched runs may or may not
#     emit; safest to treat as "no PR status" (informational notice
#     only).
_EVENT_MAP = {
    "pull_request": "pull_request",
    "pull_request_target": "pull_request",
    "push": "push",
}


# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------
def _env(key: str, default: str | None = None) -> str:
    v = os.environ.get(key, default)
    return v if v is not None else ""


def _require_env(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        sys.stderr.write(f"::error::missing required env var: {key}\n")
        sys.exit(2)
    return v


# ---------------------------------------------------------------------------
# API helper. Mirrors lint-required-no-paths.py's contract: returns
# (status, payload) tuple with status ∈ {"ok", "not_found", "forbidden",
# "error"}.
# ---------------------------------------------------------------------------
def api(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    query: dict[str, str] | None = None,
) -> tuple[str, Any]:
    host = _env("GITEA_HOST")
    token = _env("GITEA_TOKEN")
    url = f"https://{host}/api/v1{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = None
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        # CF WAF in front of git.moleculesai.app 1010-bans the default
        # Python-urllib UA; send a non-urllib UA so this reaches Gitea
        # (transport-only — auth/method/semantics unchanged).
        "User-Agent": "molecule-ci-gate/1.0 (+gitea-api)",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, method=method, data=data, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if not raw:
                return ("ok", None)
            return ("ok", json.loads(raw))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ("not_found", None)
        if e.code in (401, 403):
            return ("forbidden", None)
        return ("error", None)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return ("error", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_on(d: Any) -> Any:
    """YAML 1.1 boolean quirk: bare `on:` may parse to True. Handle both."""
    if not isinstance(d, dict):
        return None
    if "on" in d:
        return d["on"]
    if True in d:
        return d[True]
    return None


def _on_events(doc: Any) -> set[str]:
    """Return the set of event keys in a workflow's `on:` block.

    Accepts all three shapes (string / list / mapping). String/list
    shapes can't carry filters but they DO emit. Returns the
    Gitea-mapped event names per `_EVENT_MAP`.
    """
    on = _get_on(doc)
    raw_events: set[str] = set()
    if on is None:
        return raw_events
    if isinstance(on, str):
        raw_events.add(on)
    elif isinstance(on, list):
        for e in on:
            if isinstance(e, str):
                raw_events.add(e)
    elif isinstance(on, dict):
        for k in on:
            if isinstance(k, str):
                raw_events.add(k)
    return {_EVENT_MAP[e] for e in raw_events if e in _EVENT_MAP}


def _job_display(jbody: dict, jkey: str) -> str:
    """Return job's `name:` if set, else fall back to the job-key.

    Gitea formats status contexts with the job's `name:` when set;
    when unset it uses the job key. Matches lint-required-no-paths
    convention.
    """
    n = jbody.get("name") if isinstance(jbody, dict) else None
    if isinstance(n, str) and n:
        return n
    return jkey


def workflow_contexts(doc: Any) -> set[str]:
    """Return the set of contexts a workflow emits."""
    contexts: set[str] = set()
    if not isinstance(doc, dict):
        return contexts
    wf_name = doc.get("name")
    if not isinstance(wf_name, str) or not wf_name:
        return contexts  # no name => no addressable context
    events = _on_events(doc)
    if not events:
        return contexts
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return contexts
    for jkey, jbody in jobs.items():
        if jkey == "__lines__":  # tolerate line-tracking annotations
            continue
        if not isinstance(jbody, dict):
            continue
        disp = _job_display(jbody, jkey)
        for ev in events:
            contexts.add(f"{wf_name} / {disp} ({ev})")
    return contexts


def parse_context(ctx: str) -> tuple[str, str, str] | None:
    m = _CONTEXT_RE.match(ctx)
    if not m:
        return None
    return (m.group("workflow"), m.group("job"), m.group("event"))


def _iter_workflow_files(wf_dir: Path) -> list[Path]:
    return sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml")))


# ---------------------------------------------------------------------------
# Issue idempotency — search for an open issue with the canonical
# title prefix; PATCH if found, POST if not. Mirrors ci-required-drift.
# Only used in MODE=issue (the scheduled sweep).
# ---------------------------------------------------------------------------
def _canonical_title(repo: str, branch: str) -> str:
    return f"[ci-bp-drift] {repo}/{branch}: BP→emitter mismatch"


def _ensure_labels(repo: str, names: list[str]) -> list[int]:
    status, labels = api("GET", f"/repos/{repo}/labels", query={"limit": "50"})
    if status != "ok" or not isinstance(labels, list):
        return []
    out: list[int] = []
    by_name = {label["name"]: label["id"] for label in labels if isinstance(label, dict)}
    for n in names:
        if n in by_name:
            out.append(by_name[n])
    return out


def file_or_update_issue(
    repo: str, branch: str, orphans: list[str], emitter_orphans: list[str]
) -> None:
    title = _canonical_title(repo, branch)
    body_lines = [
        f"BP→emitter drift detected on `{branch}` at "
        f"{os.environ.get('GITHUB_RUN_URL', '(run url unavailable)')}.",
        "",
        f"## Orphan BP contexts ({len(orphans)})",
        "",
        "These contexts are required by branch protection but NO workflow "
        "emits them. PRs merging into this branch will wait forever for a "
        "status that never arrives (Gitea treats absent-as-`pending`, NOT "
        "absent-as-`skipped`). See "
        "`feedback_phantom_required_check_after_gitea_migration`.",
        "",
    ]
    for o in orphans:
        body_lines.append(f"- `{o}`")
    if emitter_orphans:
        body_lines += [
            "",
            f"## Workflows emitting contexts NOT in BP ({len(emitter_orphans)})",
            "",
            "Informational — Tier 2g handles this direction at PR-time. "
            "Listed here for completeness.",
            "",
        ]
        for o in emitter_orphans:
            body_lines.append(f"- `{o}`")
    body_lines += [
        "",
        "Fix options:",
        "  1. PATCH `branch_protections/{branch}.status_check_contexts` "
        "  to remove the orphan.",
        "  2. Restore the emitting workflow (if it was deleted/renamed).",
        "",
        "Linted by `.gitea/workflows/bp-context-drift-gate.yml` "
        "(BP⊆emitted asserter, ported from molecule-core Tier 2f, internal#350).",
    ]
    body = "\n".join(body_lines)

    # Idempotency search — find an open issue with the canonical title.
    status, hits = api(
        "GET",
        f"/repos/{repo}/issues",
        query={
            "type": "issues",
            "state": "open",
            "q": title,
        },
    )
    existing = None
    if status == "ok" and isinstance(hits, list):
        for h in hits:
            if (
                isinstance(h, dict)
                and h.get("state") == "open"
                and isinstance(h.get("title"), str)
                and h["title"].startswith(title)
            ):
                existing = h
                break

    label_ids = _ensure_labels(repo, ["ci-bp-drift"])

    if existing:
        api(
            "PATCH",
            f"/repos/{repo}/issues/{existing['number']}",
            body={"body": body, "labels": label_ids} if label_ids else {"body": body},
        )
        print(
            f"::notice::Updated existing drift issue "
            f"#{existing['number']}: {existing.get('html_url', '')}"
        )
    else:
        status, posted = api(
            "POST",
            f"/repos/{repo}/issues",
            body={"title": title, "body": body, "labels": label_ids},
        )
        if status == "ok" and isinstance(posted, dict):
            print(
                f"::notice::Filed new drift issue "
                f"#{posted.get('number')}: {posted.get('html_url', '')}"
            )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def run() -> int:
    _require_env("GITEA_TOKEN")
    _require_env("GITEA_HOST")
    repo = _require_env("REPO")
    branch = _env("BRANCH", "main")
    wf_dir = Path(_env("WORKFLOWS_DIR", ".gitea/workflows"))
    mode = _env("MODE", "issue").strip().lower() or "issue"
    if mode not in ("assert", "issue"):
        sys.stderr.write(
            f"::error::invalid MODE={mode!r}; expected 'assert' or 'issue'.\n"
        )
        return 2

    if not wf_dir.is_dir():
        sys.stderr.write(f"::error::workflows directory not found: {wf_dir}\n")
        return 2

    # 1. Pull BP.
    #
    # FAIL-CLOSED contract (was fail-open with exit 0 — fixed). This lint
    # is a HARD gate at the script level even when the WORKFLOW wraps it
    # in continue-on-error (advisory rollout). The DRIFT_BOT_TOKEN secret
    # is the trusted reader, so an auth failure or transient error is a
    # real inability-to-verify, not a legitimate degradation. We MUST fail
    # loud (`::error::` + nonzero) rather than green a gate we could not
    # check. Holds in BOTH MODE=assert and MODE=issue.
    status, bp = api("GET", f"/repos/{repo}/branch_protections/{branch}")
    if status == "forbidden":
        sys.stderr.write(
            f"::error::GET branch_protections/{branch} returned HTTP "
            f"401/403 — DRIFT_BOT_TOKEN cannot read branch protections "
            f"(needs repo-admin scope; Gitea requires it for this "
            f"endpoint). This is an AUTH FAILURE, not an absent resource: "
            f"the lint CANNOT verify the BP↔emitter invariant, so it FAILS "
            f"CLOSED instead of greening a gate it could not check. Fix: "
            f"set the DRIFT_BOT_TOKEN org secret to a read-only repo-admin "
            f"token — fix the token, not the lint.\n"
        )
        return 2
    if status == "not_found":
        # Genuine 404 WITH a valid token = branch has no protection
        # configured. On `main` this is itself suspicious (main should
        # always be protected) but it is a real, authenticated read of an
        # absent resource — not an auth failure — so we surface it loudly
        # but do not hard-fail on the genuinely-absent case.
        print(
            f"::warning::branch '{branch}' has no protection configured "
            f"(authenticated 404); nothing to lint. If '{branch}' SHOULD be "
            f"protected, this is a real finding — configure branch "
            f"protection."
        )
        return 0
    if status != "ok" or not isinstance(bp, dict):
        sys.stderr.write(
            f"::error::branch_protections/{branch} read failed with "
            f"status={status} (transient/unexpected). The lint CANNOT "
            f"verify the BP↔emitter invariant on this run; FAILING CLOSED "
            f"rather than greening unverified. Re-run; if it persists, "
            f"investigate Gitea API health / token validity.\n"
        )
        return 2

    bp_contexts: list[str] = list(bp.get("status_check_contexts") or [])
    if not bp_contexts:
        print(
            f"::notice::branch_protections/{branch} has 0 required "
            f"status_check_contexts; nothing to lint."
        )
        return 0

    # 2. Enumerate emitter contexts from all workflows.
    #
    # FAIL-CLOSED on YAML parse errors. A malformed workflow means we
    # CANNOT enumerate the contexts it emits, so the emitter inventory is
    # INCOMPLETE. Silently skipping it (the old `continue`) is fail-OPEN:
    # the remaining parsed workflows might happen to satisfy every
    # BP-required context, greening a gate built on an incomplete
    # inventory. Per the exit-code contract (2 = cannot-verify), we
    # accumulate every parse error, report each with an `::error::`
    # message, and return 2 after the scan if ANY workflow failed to
    # parse — same fail-closed code used for auth/transient errors, and
    # consistent across BOTH MODE=assert and MODE=issue (this runs before
    # the mode branch). "Nothing fails open."
    all_emitter: set[str] = set()
    parse_errors: list[str] = []
    for path in _iter_workflow_files(wf_dir):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            parse_errors.append(str(path))
            sys.stderr.write(
                f"::error file={path}::YAML parse error: {e}. The emitter "
                f"inventory would be INCOMPLETE, so this lint CANNOT verify "
                f"the BP↔emitter invariant; FAILING CLOSED (exit 2) instead "
                f"of greening on a partial inventory. Fix the malformed "
                f"workflow.\n"
            )
            continue
        all_emitter |= workflow_contexts(doc)

    if parse_errors:
        sys.stderr.write(
            f"::error::{len(parse_errors)} workflow file(s) failed to parse "
            f"({', '.join(parse_errors)}); the emitter inventory is "
            f"INCOMPLETE. FAILING CLOSED (exit 2) — the BP↔emitter invariant "
            f"cannot be verified against a partial inventory. Holds in both "
            f"MODE=assert and MODE=issue.\n"
        )
        return 2

    print(
        f"::notice::[MODE={mode}] Linting {len(bp_contexts)} BP context(s) "
        f"for {branch} against {len(all_emitter)} workflow-emitted "
        f"context(s)."
    )

    bp_set = set(bp_contexts)

    # 3. Find orphans (BP-side: required but no emitter).
    bp_orphans = sorted(bp_set - all_emitter)

    # Informational: workflow emits but BP doesn't list. Tier 2g
    # territory at PR-time. We list these as NOTICE only.
    emitter_orphans = sorted(all_emitter - bp_set)

    if bp_orphans:
        print(
            f"::error::Found {len(bp_orphans)} BP context(s) with no "
            f"emitter — these would block merges forever (Gitea treats "
            f"absent-as-pending, not skipped; the merge endpoint returns "
            f"HTTP 405 'try again later' forever):"
        )
        for o in bp_orphans:
            # Closest-match hint: name a workflow whose name-part is a
            # near-match (lev-1 typo, or same workflow with a different
            # event).
            parsed = parse_context(o)
            hint = ""
            if parsed:
                wf, _job, _ev = parsed
                candidates = sorted(
                    {c for c in all_emitter if c.startswith(wf + " / ")}
                )
                if candidates:
                    hint = (
                        f" — closest emitter(s): {', '.join(candidates[:3])}"
                    )
            print(f"::error::  - {o}{hint}")
        if emitter_orphans:
            print(
                f"::notice::Also: {len(emitter_orphans)} workflow-emitted "
                f"context(s) not in BP (informational; Tier 2g handles at "
                f"PR-time):"
            )
            for o in emitter_orphans:
                print(f"::notice::  - {o}")
        # MODE=issue files/patches a tracking issue. MODE=assert (PR-time
        # gate) SKIPS the issue path entirely — no token-write side
        # effects on a PR — and just returns nonzero.
        if mode == "issue":
            try:
                file_or_update_issue(repo, branch, bp_orphans, emitter_orphans)
            except Exception as e:
                sys.stderr.write(
                    f"::error::failed to file drift issue: {e}\n"
                )
        return 1

    if emitter_orphans:
        print(
            f"::notice::{len(emitter_orphans)} workflow-emitted context(s) "
            f"not in BP (informational; Tier 2g handles at PR-time):"
        )
        for o in emitter_orphans:
            print(f"::notice::  - {o}")

    print(
        f"::notice::BP/emitter match clean: all {len(bp_contexts)} required "
        f"context(s) have an emitter."
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
