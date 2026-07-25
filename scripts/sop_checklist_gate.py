#!/usr/bin/env python3
# sop-checklist-gate — evaluate whether a PR has peer-acked each
# SOP-checklist item. Posts a commit-status that branch protection
# can require.
#
# RFC#351 Step 2 of 6 (implementation MVP).
#
# ===========================================================================
# THIS FILE IS THE SSOT. Consumer repositories must NOT vendor a copy.
# ===========================================================================
# It was consolidated (2026-07-25) from three hand-maintained copies that had
# drifted in BOTH directions and were, between them, the entire defect:
#
#   molecule-app         had the Cloudflare-UA fix + the OPERATOR RELAX block,
#                        but NOT the #369 OOM guardrail.
#   molecule-ai-status   had the #369 OOM guardrail (RLIMIT_AS cap, streaming
#                        minimal-dict comment pagination, --max-comments,
#                        per-body byte cap), the trusted-acker / unverifiable
#                        team-probe path, and the 403-tolerant status POST —
#                        but NOT the UA fix or the relax.
#   molecules-market     had none of the above.
#
# Every capability above is preserved here; nothing was dropped. Consumers
# install templates/ci-sop-checklist-gate.yml, which fetches this repo at a
# reviewed immutable pin and executes THIS file — the same inline-SSOT pattern
# the other canonical gates use (cross-repo `workflow_call` is not a valid gate
# on Gitea 1.26.4; see README).
#
# Invoked by .gitea/workflows/sop-checklist-gate.yml on:
#   - pull_request_target: [opened, edited, synchronize, reopened]
#   - issue_comment:       [created, edited, deleted]
#
# Flow:
#   1. Load .gitea/sop-checklist-config.yaml (from BASE ref — trusted).
#   2. GET /repos/{R}/pulls/{N}          — author, head.sha, tier label
#   3. GET /repos/{R}/issues/{N}/comments — extract /sop-ack and /sop-revoke
#   4. For each checklist item:
#        a. Is the section marker present in PR body? (author answered)
#        b. Is there ≥1 unrevoked /sop-ack from a non-author whose
#           team-membership matches required_teams?
#   5. POST /repos/{R}/statuses/{sha}    — context
#      `sop-checklist / all-items-acked (pull_request)`,
#      state=success | failure | pending, description=`acked: N/M …`.
#      OPERATOR RELAX: while the org Actions var MERGE_REQUIRE_SOP_ACK
#      != 'true', a would-be failure/pending is posted as SUCCESS with
#      description `ack advisory until agent-team — <tally>` (full
#      evaluation still runs; see the relax block in main()).
#
# Transport: all API calls send CANONICAL_GITEA_USER_AGENT and target
# `--api-base` (org var MOLECULE_GITEA_API_HOST), which is deliberately
# separate from `--gitea-host` (the public host used for the status link).
#
# Trust boundary (mirrors RFC#324 §A4):
#   This script is loaded from the BASE branch. The workflow's
#   actions/checkout step pins ref=base.sha. PR-HEAD code is never
#   executed. We only HTTP-call the Gitea API.
#
# Token scope:
#   - read:repository / read:organization to enumerate PR + comments
#     + team membership (Gitea 1.22.6 quirk: team-membership endpoint
#     returns 403 if token owner is not in the team; see review-check.sh
#     for the same gotcha — we surface the same fail-closed message).
#   - write:repository for `POST /repos/{R}/statuses/{sha}`. Unlike
#     RFC#324's pattern (which uses the JOB's own pass/fail as the
#     status), we POST the status explicitly because the gate posts
#     a single multi-item status with a richer description than a
#     bare success/failure context can carry.
#
# Slug normalization rules (canonical form: kebab-case):
#   - Lowercase
#   - Whitespace + underscores → single dash
#   - Strip non [a-z0-9-] characters
#   - Collapse adjacent dashes
#   - Strip leading/trailing dashes
#   - If the result is a digit string (e.g. "1"), look up via
#     config.items[*].numeric_alias to get the kebab-case slug.
#
#   Examples:
#       "Comprehensive_Testing"  → "comprehensive-testing"
#       "comprehensive testing"  → "comprehensive-testing"
#       "1"                      → "comprehensive-testing"
#       "Five-Axis-Review"       → "five-axis-review"
#
# Revoke semantics:
#   /sop-revoke <slug> [reason] — most-recent comment per (slug, user)
#   wins. So if Alice posts /sop-ack X then later /sop-revoke X, her ack
#   for X is invalidated. Bob's prior /sop-ack X is unaffected. If Alice
#   posts /sop-revoke X then later /sop-ack X again, the ack is restored.

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

try:  # POSIX-only; absent on Windows. The guardrail below self-skips.
    import resource
except ImportError:  # pragma: no cover - platform dependent
    resource = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Address-space guardrail (task #369 follow-up to mc#1242-class OOM).
#
# `get_issue_comments` paginates the full comment history of a PR. On
# bot-relay-heavy PRs this can balloon past the runner's cgroup memory
# limit and 137 the job. Cap virtual-address-space at 2 GiB so the script
# OOMs as a `MemoryError` (catchable / surfaceable) rather than a SIGKILL
# we can't post a status for.
#
# Set SOP_CHECKLIST_NO_RLIMIT=1 to opt out (e.g. for the test suite).
if resource is not None and not os.environ.get("SOP_CHECKLIST_NO_RLIMIT"):
    try:
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    except (ValueError, OSError):
        pass

# Per-comment body cap. The directive parser only needs the first ~8 KiB
# to find /sop-ack /sop-revoke markers — anything past that is filler.
_MAX_BODY_BYTES = int(os.environ.get("SOP_CHECKLIST_MAX_BODY_BYTES") or 8 * 1024)


# ---------------------------------------------------------------------------
# Slug normalization
# ---------------------------------------------------------------------------

_NORMALIZE_REPLACE_RE = re.compile(r"[\s_]+")
_NORMALIZE_STRIP_RE = re.compile(r"[^a-z0-9-]")
_NORMALIZE_DASH_RE = re.compile(r"-+")


def normalize_slug(raw: str, numeric_aliases: dict[int, str] | None = None) -> str:
    """Normalize a user-supplied slug to canonical kebab-case form.

    See module header for the rules.

    If the input is a pure digit string AND numeric_aliases is provided,
    the alias mapping is consulted. Unknown digits return "" so the caller
    can flag the comment as unparseable.
    """
    if raw is None:
        return ""
    s = raw.strip().lower()
    s = _NORMALIZE_REPLACE_RE.sub("-", s)
    s = _NORMALIZE_STRIP_RE.sub("", s)
    s = _NORMALIZE_DASH_RE.sub("-", s)
    s = s.strip("-")
    if s.isdigit() and numeric_aliases is not None:
        return numeric_aliases.get(int(s), "")
    return s


# ---------------------------------------------------------------------------
# Comment parsing — /sop-ack and /sop-revoke
# ---------------------------------------------------------------------------

# A directive must be on its own line. Permits leading whitespace.
# Optional trailing note after the slug for /sop-ack and required reason
# for /sop-revoke (RFC#351 open question 4 — reason is captured but not
# yet validated; future iteration may require a min-length).
_DIRECTIVE_RE = re.compile(
    r"^[ \t]*/(sop-ack|sop-revoke)[ \t]+([A-Za-z0-9_\- ]+?)(?:[ \t]+(.*))?[ \t]*$",
    re.MULTILINE,
)


def parse_directives(
    comment_body: str,
    numeric_aliases: dict[int, str],
) -> list[tuple[str, str, str]]:
    """Extract /sop-ack and /sop-revoke directives from a comment body.

    Returns a list of (kind, canonical_slug, note) tuples where:
      kind is "sop-ack" or "sop-revoke"
      canonical_slug is the normalized form (or "" if unparseable)
      note is the trailing free-text (may be "")
    """
    out: list[tuple[str, str, str]] = []
    if not comment_body:
        return out
    for m in _DIRECTIVE_RE.finditer(comment_body):
        kind = m.group(1)
        raw_slug = (m.group(2) or "").strip()
        # If the raw match included trailing words, the regex non-greedy
        # captured only the first token; strip again for safety.
        # We split on whitespace to keep the FIRST word as the slug, and
        # everything after as the note.
        parts = raw_slug.split()
        if not parts:
            continue
        first = parts[0]
        # If the slug-capture greedily matched multiple words (e.g.
        # "comprehensive testing"), preserve normalize behavior: join
        # the WHOLE first-word-token only; trailing words get appended to
        # the note. The regex limits group(2) to [A-Za-z0-9_\- ] so we
        # may have multi-word forms here — normalize handles them.
        if len(parts) > 1:
            # User wrote "/sop-ack comprehensive testing extra-note"
            # → treat "comprehensive testing" as the slug source if it
            # normalizes to a known item; otherwise treat "comprehensive"
            # as slug and "testing extra-note" as note. We defer the
            # disambiguation to the caller via the returned canonical
            # slug. For simplicity: try the WHOLE captured string first.
            canonical = normalize_slug(raw_slug, numeric_aliases)
        else:
            canonical = normalize_slug(first, numeric_aliases)
        note_from_group = (m.group(3) or "").strip()
        # If we collapsed multi-word slug into kebab and there's a
        # trailing-text group too, append it.
        out.append((kind, canonical, note_from_group))
    return out


# ---------------------------------------------------------------------------
# PR body section detection
# ---------------------------------------------------------------------------


def section_marker_present(body: str, marker: str) -> bool:
    """Return True if `marker` appears in `body` case-insensitively
    on a non-empty line (i.e. the author actually filled it in).

    We require the marker substring AND non-whitespace content on the
    same line OR within the next line — this prevents trivially-empty
    checklists like:

        ## SOP-Checklist
        - [ ] **Comprehensive testing performed**:
        - [ ] **Local-postgres E2E run**:

    from auto-passing the section-present check. The peer-ack is still
    required, but answering with empty content is captured as a soft
    finding via the section-present test alone.
    """
    if not body or not marker:
        return False
    body_lower = body.lower()
    marker_lower = marker.lower()
    idx = body_lower.find(marker_lower)
    if idx < 0:
        return False
    # Walk to end of line.
    line_end = body.find("\n", idx)
    if line_end < 0:
        line_end = len(body)
    line = body[idx + len(marker):line_end]
    # Strip the colon + checkbox tail patterns; require at least one
    # non-whitespace, non-punctuation char.
    stripped = re.sub(r"[\s\*:\-\[\]]+", "", line)
    if stripped:
        return True
    # Fall through: check the NEXT line (multi-line answers).
    next_line_end = body.find("\n", line_end + 1)
    if next_line_end < 0:
        next_line_end = len(body)
    next_line = body[line_end + 1:next_line_end]
    stripped_next = re.sub(r"[\s\*:\-\[\]]+", "", next_line)
    return bool(stripped_next)


# ---------------------------------------------------------------------------
# Ack-state computation
# ---------------------------------------------------------------------------


def compute_ack_state(
    comments: list[dict[str, Any]],
    pr_author: str,
    items_by_slug: dict[str, dict[str, Any]],
    numeric_aliases: dict[int, str],
    team_membership_probe: "callable[[str, list[str]], tuple[list[str], list[str]]]",
    trusted_ackers: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute per-item ack state.

    Each comment is processed in chronological order. The most-recent
    directive per (commenter, slug) wins.

    ``team_membership_probe`` returns ``(approved, unverifiable)`` where
    ``approved`` are users confirmed to be in a required team and
    ``unverifiable`` are users whose membership could not be determined
    (e.g., the gate token got a 403 on every team probe). Unverifiable
    ackers are accepted only if they appear in ``trusted_ackers``.

    Returns a dict keyed by canonical slug:
       {
         "comprehensive-testing": {
           "ackers": ["bob"],         # non-author, team-verified or trusted
           "rejected_ackers": {        # debugging info
             "self_ack": ["alice"],
             "unknown_slug": [],
             "not_in_team": ["eve"],
           }
         },
         ...
       }
    """
    trusted = set(trusted_ackers or [])
    # Step 1: collapse directives per (commenter, slug) — most recent wins.
    # comments are expected to come in chronological order from the
    # API (Gitea returns oldest-first by default for issues/{N}/comments).
    latest_directive: dict[tuple[str, str], str] = {}  # (user, slug) → kind
    unparseable_per_user: dict[str, int] = {}
    for c in comments:
        body = c.get("body", "") or ""
        user = (c.get("user") or {}).get("login", "")
        if not user:
            continue
        for kind, slug, _note in parse_directives(body, numeric_aliases):
            if not slug:
                unparseable_per_user[user] = unparseable_per_user.get(user, 0) + 1
                continue
            latest_directive[(user, slug)] = kind

    # Step 2: build candidate ackers per slug.
    # Filter out self-acks and unknown slugs.
    ackers_per_slug: dict[str, list[str]] = {s: [] for s in items_by_slug}
    rejected_self: dict[str, list[str]] = {s: [] for s in items_by_slug}
    pending_team_check: dict[str, list[str]] = {s: [] for s in items_by_slug}

    for (user, slug), kind in latest_directive.items():
        if kind != "sop-ack":
            continue  # revokes leave the (user,slug) state as "no ack"
        if slug not in items_by_slug:
            # Slug normalized to something not in our config — store
            # under a synthetic key for diagnostic surfacing. Don't add
            # to any item.
            continue
        if user == pr_author:
            rejected_self[slug].append(user)
            continue
        pending_team_check[slug].append(user)

    # Step 3: team membership probe per slug (batched per slug to keep
    # API call count down — same user may ack multiple items but the
    # required_teams differ per item, so we MUST probe per (user, item)).
    rejected_not_in_team: dict[str, list[str]] = {s: [] for s in items_by_slug}
    for slug, candidates in pending_team_check.items():
        if not candidates:
            continue
        required = items_by_slug[slug]["required_teams"]
        approved, unverifiable = team_membership_probe(slug, candidates)
        # Accept unverifiable ackers only when they are explicitly
        # trusted reviewer bots and no required team probe gave a
        # definitive False answer for them.
        trusted_fallback = [u for u in unverifiable if u in trusted and u not in approved]
        if trusted_fallback:
            approved = approved + trusted_fallback
        rejected_not_in_team[slug] = [u for u in candidates if u not in approved]
        ackers_per_slug[slug] = approved
        # Stash required teams for description rendering.
        items_by_slug[slug]["_required_resolved"] = required

    return {
        slug: {
            "ackers": ackers_per_slug[slug],
            "rejected": {
                "self_ack": rejected_self[slug],
                "not_in_team": rejected_not_in_team[slug],
            },
        }
        for slug in items_by_slug
    }


# ---------------------------------------------------------------------------
# Gitea API client
# ---------------------------------------------------------------------------


# Canonical Cloudflare-safe Gitea API User-Agent.
#
# ROOT CAUSE (2026-07-25 incident, molecules-market#9 / molecule-ai-status#51):
# Browser Integrity Check is ON for the `moleculesai.app` zone. BIC is a
# browser-UA heuristic and it denylists `Python-urllib/<ver>` — the UA
# `urllib.request` sends by default. Every gate call therefore came back
# `HTTP 403 … error_code 1010 browser_signature_banned`, the script raised on
# `GET pulls/{N}`, and the REQUIRED merge context went red for reasons that had
# nothing to do with the PR. curl-based CI never saw it, which is why the ban
# went unnoticed.
#
# The value is the org-wide constant already ratcheted by
# scripts/test_canonical_gitea_user_agent.py for every other urllib Gitea
# client in this repo. Keep them identical — one constant, one contract.
# Transport-only: method/auth/body/semantics are unchanged.
CANONICAL_GITEA_USER_AGENT = "curl/8.4.0"


def resolve_api_base(api_base: str | None, gitea_host: str) -> str:
    """Return the `/api/v1` base URL the gate should call.

    Precedence:
      1. explicit ``api_base`` (``--api-base`` / ``MOLECULE_GITEA_API_HOST``),
         which may be a bare host or a full scheme-qualified origin;
      2. ``https://{gitea_host}`` — the public Cloudflare-fronted edge.

    Why this is configurable rather than hardcoded: internal CI has no business
    traversing the public CDN edge to reach its own forge. Once a mesh endpoint
    for Gitea is actually provisioned on the runner hosts (name resolvable +
    a TLS listener), pointing the gate at it is a one-variable change — the org
    Actions variable `MOLECULE_GITEA_API_HOST` — with no code edit. It is NOT
    defaulted to the mesh today because the mesh name is not resolvable from the
    runner fleet and no TLS listener exists there; a required gate must not
    fail closed on unprovisioned private-network state.
    """
    base = (api_base or "").strip()
    if not base:
        base = f"https://{gitea_host}"
    if "://" not in base:
        base = f"https://{base}"
    return base.rstrip("/") + "/api/v1"


class GiteaClient:
    def __init__(self, host: str, token: str, api_base: str | None = None):
        self.base = resolve_api_base(api_base, host)
        self.token = token
        # Cache team-name → team-id resolutions per org.
        self._team_id_cache: dict[tuple[str, str], int | None] = {}

    def _req(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        ok_codes: tuple[int, ...] = (200, 201, 204),
    ) -> tuple[int, Any]:
        url = self.base + path
        data = None
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/json",
            "User-Agent": CANONICAL_GITEA_USER_AGENT,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                code = r.getcode()
        except urllib.error.HTTPError as e:
            code = e.code
            raw = e.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            parsed = raw.decode("utf-8", errors="replace") if raw else None
        return code, parsed

    def get_pr(self, owner: str, repo: str, pr: int) -> dict[str, Any]:
        code, data = self._req("GET", f"/repos/{owner}/{repo}/pulls/{pr}")
        if code != 200:
            raise RuntimeError(f"GET pulls/{pr} → HTTP {code}: {data!r}")
        return data

    def iter_issue_comments(
        self, owner: str, repo: str, issue: int, page_size: int = 50
    ) -> Iterator[dict[str, Any]]:
        """Stream comments page-by-page, yielding ONE minimal-dict per comment.

        Each yielded comment carries ONLY the fields the gate actually
        reads — `{"user": {"login": str}, "body": str}` — DROPPING the
        much larger Gitea-API extras (html_url, assets, timestamps, etc.).

        Task #369 / mc#1242-class OOM fix: full Gitea comment dicts are
        ~2 KiB median + ~3 KiB p95. Bot-relay-heavy PRs ballooned the
        runner past its cgroup memory limit. The minimal-dict shape is
        ~10-20x smaller per comment.
        """
        page = 1
        while True:
            code, data = self._req(
                "GET",
                f"/repos/{owner}/{repo}/issues/{issue}/comments?limit={page_size}&page={page}",
            )
            if code != 200:
                raise RuntimeError(
                    f"GET issues/{issue}/comments page={page} → HTTP {code}: {data!r}"
                )
            if not data:
                break
            for c in data:
                user_login = ((c.get("user") or {}).get("login") or "") if isinstance(c, dict) else ""
                body = (c.get("body") if isinstance(c, dict) else "") or ""
                if len(body) > _MAX_BODY_BYTES:
                    body = body[:_MAX_BODY_BYTES]
                yield {"user": {"login": user_login}, "body": body}
            if len(data) < page_size:
                break
            page += 1

    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        issue: int,
        max_comments: int | None = None,
    ) -> list[dict[str, Any]]:
        """Paginate + collect minimal comment dicts. See `iter_issue_comments`
        for the per-comment shape + OOM-prevention rationale. `max_comments`
        provides a hard volume cap (task #369)."""
        out: list[dict[str, Any]] = []
        for c in self.iter_issue_comments(owner, repo, issue):
            out.append(c)
            if max_comments is not None and len(out) >= max_comments:
                break
        return out

    def resolve_team_id(self, org: str, team_name: str) -> int | None:
        key = (org, team_name)
        if key in self._team_id_cache:
            return self._team_id_cache[key]
        code, data = self._req("GET", f"/orgs/{org}/teams/search?q={urllib.parse.quote(team_name)}")
        team_id = None
        if code == 200 and isinstance(data, dict):
            for t in data.get("data", []):
                if t.get("name") == team_name:
                    team_id = t.get("id")
                    break
        if team_id is None and code == 200 and isinstance(data, list):
            for t in data:
                if t.get("name") == team_name:
                    team_id = t.get("id")
                    break
        self._team_id_cache[key] = team_id
        return team_id

    def is_team_member(self, team_id: int, login: str) -> bool | None:
        """Return True / False / None (unknown — 403 from API)."""
        code, _ = self._req(
            "GET", f"/teams/{team_id}/members/{urllib.parse.quote(login)}"
        )
        if code in (200, 204):
            return True
        if code == 404:
            return False
        # 403 means the token owner isn't in this team, so the API
        # refuses to confirm membership. Fail-closed at the caller.
        return None

    def post_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        state: str,
        context: str,
        description: str,
        target_url: str = "",
    ) -> None:
        body = {
            "state": state,
            "context": context,
            "description": description[:140],  # Gitea truncates to 255 but be safe
            "target_url": target_url or "",
        }
        code, data = self._req(
            "POST",
            f"/repos/{owner}/{repo}/statuses/{sha}",
            body=body,
            ok_codes=(201,),
        )
        if code not in (200, 201):
            raise RuntimeError(
                f"POST statuses/{sha} → HTTP {code}: {data!r}"
            )


# ---------------------------------------------------------------------------
# Config loader (PyYAML-free — config file is intentionally tiny + flat)
# ---------------------------------------------------------------------------


def load_config(path: str) -> dict[str, Any]:
    """Load .gitea/sop-checklist-config.yaml.

    Uses PyYAML if available, otherwise falls back to a built-in
    minimal parser sufficient for our flat config shape. Bundling
    PyYAML on the runner is one apt install away but we avoid the
    dep by keeping the config shape constrained.
    """
    try:
        import yaml  # type: ignore[import-not-found]
        with open(path) as f:
            return yaml.safe_load(f)
    except ImportError:
        return _load_config_minimal(path)


def _load_config_minimal(path: str) -> dict[str, Any]:
    """Minimal YAML subset parser for our config shape.

    Supports: top-level scalar:value, top-level map-of-map (e.g.
    tier_failure_mode), top-level list of maps (items:), and within an
    item map: scalars + lists of scalars. Does NOT support nested lists,
    YAML anchors, multi-doc, or flow style.
    """
    with open(path) as f:
        lines = f.readlines()
    return _parse_minimal_yaml(lines)


def _parse_minimal_yaml(lines: list[str]) -> dict[str, Any]:  # noqa: C901
    """Hand-rolled subset parser. See _load_config_minimal docstring."""
    # Strip comments + blank lines but preserve indentation.
    cleaned: list[tuple[int, str]] = []
    for raw in lines:
        # Don't strip a "#" that is inside a quoted value.
        body = raw.rstrip("\n")
        # Remove trailing comment.
        idx = body.find("#")
        if idx >= 0 and (idx == 0 or body[idx - 1] in " \t"):
            body = body[:idx].rstrip()
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip(" "))
        cleaned.append((indent, body.strip()))

    root: dict[str, Any] = {}
    i = 0
    n = len(cleaned)

    def parse_scalar(s: str) -> Any:
        s = s.strip()
        if s.startswith('"') and s.endswith('"'):
            return s[1:-1]
        if s.startswith("'") and s.endswith("'"):
            return s[1:-1]
        if s.lower() in ("true", "yes"):
            return True
        if s.lower() in ("false", "no"):
            return False
        try:
            return int(s)
        except ValueError:
            pass
        return s

    def parse_inline_list(s: str) -> list[Any]:
        s = s.strip()
        if not (s.startswith("[") and s.endswith("]")):
            return [parse_scalar(s)]
        inner = s[1:-1]
        if not inner.strip():
            return []
        return [parse_scalar(x.strip()) for x in inner.split(",")]

    while i < n:
        indent, line = cleaned[i]
        if indent != 0:
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            # Block — could be map or list.
            i += 1
            # Look ahead for first child.
            if i < n and cleaned[i][1].startswith("- "):
                # List of items.
                items: list[Any] = []
                while i < n and cleaned[i][0] > indent and cleaned[i][1].startswith("- "):
                    item_indent = cleaned[i][0]
                    first_kv = cleaned[i][1][2:].strip()  # strip "- "
                    item: dict[str, Any] = {}
                    if ":" in first_kv:
                        k, _, v = first_kv.partition(":")
                        k = k.strip()
                        v = v.strip()
                        if v == "":
                            item[k] = ""
                        elif v.startswith(">-") or v.startswith(">"):
                            # Folded scalar continues on subsequent indented lines
                            collected: list[str] = []
                            i += 1
                            while i < n and cleaned[i][0] > item_indent:
                                collected.append(cleaned[i][1])
                                i += 1
                            item[k] = " ".join(collected)
                            items.append(item)
                            continue
                        elif v.startswith("["):
                            item[k] = parse_inline_list(v)
                        else:
                            item[k] = parse_scalar(v)
                    i += 1
                    # Subsequent k:v lines at deeper indent belong to this item.
                    while i < n and cleaned[i][0] > item_indent and not cleaned[i][1].startswith("- "):
                        sub_indent, sub_line = cleaned[i]
                        if ":" in sub_line:
                            k, _, v = sub_line.partition(":")
                            k = k.strip()
                            v = v.strip()
                            if v == "":
                                item[k] = ""
                                i += 1
                            elif v.startswith(">-") or v.startswith(">"):
                                collected = []
                                i += 1
                                while i < n and cleaned[i][0] > sub_indent:
                                    collected.append(cleaned[i][1])
                                    i += 1
                                item[k] = " ".join(collected)
                            elif v.startswith("["):
                                item[k] = parse_inline_list(v)
                                i += 1
                            else:
                                item[k] = parse_scalar(v)
                                i += 1
                        else:
                            i += 1
                    items.append(item)
                root[key] = items
            else:
                # Sub-map.
                submap: dict[str, Any] = {}
                while i < n and cleaned[i][0] > indent:
                    sub_indent, sub_line = cleaned[i]
                    if ":" in sub_line:
                        k, _, v = sub_line.partition(":")
                        k = k.strip().strip('"').strip("'")
                        v = v.strip()
                        if v.startswith("[") and v.endswith("]"):
                            submap[k] = parse_inline_list(v)
                        else:
                            submap[k] = parse_scalar(v)
                    i += 1
                root[key] = submap
        else:
            # Inline scalar or list.
            if rest.startswith("[") and rest.endswith("]"):
                root[key] = parse_inline_list(rest)
            else:
                root[key] = parse_scalar(rest)
            i += 1
    return root


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def render_status(
    items: list[dict[str, Any]],
    ack_state: dict[str, dict[str, Any]],
    body_state: dict[str, bool],
) -> tuple[str, str]:
    """Return (state, description) for the commit-status post.

    state is "success" if every item has at least one valid ack
    (body section presence is informational only — peer-ack is the
    real gate).  "pending" is reserved for the soft-fail path
    (tier:low) and is set by the caller.
    """
    n = len(items)
    fully_acked = [
        it["slug"] for it in items if ack_state[it["slug"]]["ackers"]
    ]
    missing = [
        it["slug"] for it in items if not ack_state[it["slug"]]["ackers"]
    ]
    missing_body = [it["slug"] for it in items if not body_state.get(it["slug"], False)]

    desc_parts = [f"acked: {len(fully_acked)}/{n}"]
    if missing:
        # Show up to 3 missing slugs to stay inside the 140-char budget.
        shown = ", ".join(missing[:3])
        if len(missing) > 3:
            shown += f", +{len(missing) - 3}"
        desc_parts.append(f"missing: {shown}")
    if missing_body:
        desc_parts.append(f"body-unfilled: {len(missing_body)}")
    state = "success" if not missing else "failure"
    return state, " — ".join(desc_parts)


def get_tier_mode(pr: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Read tier label, return 'hard' or 'soft' per cfg.tier_failure_mode."""
    labels = pr.get("labels") or []
    tier_labels = [lbl.get("name", "") for lbl in labels if (lbl.get("name", "") or "").startswith("tier:")]
    mode_map = cfg.get("tier_failure_mode") or {}
    default_mode = cfg.get("default_mode", "hard")
    for tl in tier_labels:
        if tl in mode_map:
            return mode_map[tl]
    return default_mode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--owner", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--config", default=".gitea/sop-checklist-config.yaml")
    p.add_argument(
        "--gitea-host",
        default="git.moleculesai.app",
        help=(
            "PUBLIC web host. Only used to build the human-clickable "
            "`target_url` on the posted status, and as the fallback API "
            "origin when --api-base is unset."
        ),
    )
    p.add_argument(
        "--api-base",
        default=os.environ.get("MOLECULE_GITEA_API_HOST") or "",
        help=(
            "Origin the gate calls for the Gitea REST API (bare host or "
            "scheme-qualified). Defaults to the org Actions variable "
            "MOLECULE_GITEA_API_HOST, else https://<--gitea-host>. Kept "
            "separate from --gitea-host so the API can move off the public "
            "edge without breaking the status link."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute state but do not POST the status.",
    )
    p.add_argument(
        "--status-context",
        default="sop-checklist / all-items-acked (pull_request)",
    )
    p.add_argument(
        "--exit-on-state",
        action="store_true",
        help=(
            "If set, exit non-zero when state=failure. Default OFF so the "
            "job-level conclusion is independent of ack-state — the only "
            "thing BP sees is the POSTed status. Useful for local debugging."
        ),
    )
    p.add_argument(
        "--max-comments",
        type=int,
        default=int(os.environ.get("SOP_CHECKLIST_MAX_COMMENTS") or 5000),
        help=(
            "Hard cap on comments fetched from the PR. Above this we post "
            "a SOFT-pending status with a 'skipping due to volume' note "
            "instead of OOM'ing the runner (task #369). Set 0 to disable."
        ),
    )
    args = p.parse_args(argv)

    token = os.environ.get("GITEA_TOKEN", "")
    if not token and not args.dry_run:
        print("::error::GITEA_TOKEN env required", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    items: list[dict[str, Any]] = cfg["items"]
    items_by_slug = {it["slug"]: it for it in items}
    numeric_aliases = {
        int(it["numeric_alias"]): it["slug"] for it in items if it.get("numeric_alias")
    }

    client = (
        GiteaClient(args.gitea_host, token, api_base=args.api_base)
        if token
        else None
    )
    if not client:
        print("::error::No client (dry-run without token has nothing to do)", file=sys.stderr)
        return 2

    pr = client.get_pr(args.owner, args.repo, args.pr)
    if pr.get("state") != "open":
        print(f"::notice::PR #{args.pr} is {pr.get('state')} — gate is a no-op")
        return 0

    author = (pr.get("user") or {}).get("login", "")
    head_sha = (pr.get("head") or {}).get("sha", "")
    body = pr.get("body", "") or ""

    if not author or not head_sha:
        print("::error::PR payload missing user.login or head.sha", file=sys.stderr)
        return 1

    max_comments_cap = args.max_comments if args.max_comments and args.max_comments > 0 else None
    comments = client.get_issue_comments(
        args.owner, args.repo, args.pr, max_comments=max_comments_cap
    )
    volume_skipped = bool(max_comments_cap and len(comments) >= max_comments_cap)

    # Build team-membership probe closure that caches results per
    # (user, team-id) so a user acking multiple items only triggers
    # one membership lookup per team.
    team_member_cache: dict[tuple[str, int], bool | None] = {}

    def probe(slug: str, users: list[str]) -> tuple[list[str], list[str]]:
        item = items_by_slug[slug]
        team_names: list[str] = item["required_teams"]
        # Resolve names → ids. NOTE: orgs/{org}/teams/search may not be
        # available — fall back to the list endpoint.
        team_ids: list[int] = []
        for tn in team_names:
            tid = client.resolve_team_id(args.owner, tn)
            if tid is None:
                # Try the list endpoint as a fallback.
                code, data = client._req(  # noqa: SLF001
                    "GET", f"/orgs/{args.owner}/teams"
                )
                if code == 200 and isinstance(data, list):
                    for t in data:
                        if t.get("name") == tn:
                            tid = t.get("id")
                            client._team_id_cache[(args.owner, tn)] = tid  # noqa: SLF001
                            break
            if tid is not None:
                team_ids.append(tid)
            else:
                print(
                    f"::warning::could not resolve team-id for '{tn}' "
                    f"in org '{args.owner}' — item '{slug}' will fail closed",
                    file=sys.stderr,
                )
        approved: list[str] = []
        unverifiable: list[str] = []
        for u in users:
            all_unknown = True if team_ids else False
            for tid in team_ids:
                cache_key = (u, tid)
                if cache_key not in team_member_cache:
                    team_member_cache[cache_key] = client.is_team_member(tid, u)
                result = team_member_cache[cache_key]
                if result is True:
                    approved.append(u)
                    all_unknown = False
                    break
                if result is False:
                    all_unknown = False
                if result is None:
                    print(
                        f"::warning::team-probe for {u} in team-id {tid} returned 403 "
                        "(token owner not in that team — fail-closed per RFC#324)",
                        file=sys.stderr,
                    )
                    # Keep all_unknown True for this team; loop may still
                    # find membership in another team.
            if u not in approved and all_unknown:
                unverifiable.append(u)
        return approved, unverifiable

    trusted_ackers: list[str] = cfg.get("trusted_ackers", [])
    ack_state = compute_ack_state(
        comments, author, items_by_slug, numeric_aliases, probe,
        trusted_ackers=trusted_ackers,
    )
    body_state = {it["slug"]: section_marker_present(body, it["pr_section_marker"]) for it in items}

    state, description = render_status(items, ack_state, body_state)
    mode = get_tier_mode(pr, cfg)
    if state == "failure" and mode == "soft":
        state = "pending"
        description = f"[soft-fail tier:low] {description}"
    if volume_skipped:
        # Above the comment-cap — partial view; soft-pend so the gate
        # doesn't block on a possibly-incomplete read. Task #369.
        state = "pending"
        description = (
            f"[volume-skipped] comment-cap={max_comments_cap} hit; please file "
            f"a fresh PR with bot-relay history split off (#369). {description}"
        )

    # OPERATOR RELAX (2026-07-05 standing directive): until the agent team
    # exists, peer-ack is ADVISORY — post SUCCESS with the real ack tally
    # appended, instead of failure/pending. Gated on the org Actions
    # variable MERGE_REQUIRE_SOP_ACK (injected by the workflow via
    # `vars.MERGE_REQUIRE_SOP_ACK`; org default: false). Setting it to
    # 'true' restores full enforcement with ZERO code change — the entire
    # evaluation above (parsing, team probes, volume guardrail, diagnostics
    # below) runs unconditionally either way. An absent env var counts as
    # relax so reruns of pre-relax workflow definitions (which don't inject
    # the env; the script itself is always loaded from the trusted default
    # branch) also resolve advisory rather than red.
    #
    # ORDERING: this runs LAST, after the tier soft-fail and the #369
    # volume-skip guardrail, so the description it wraps is the fully
    # evaluated one and no earlier branch can re-redden a relaxed result.
    require_ack = (
        os.environ.get("MERGE_REQUIRE_SOP_ACK", "").strip().lower() == "true"
    )
    if not require_ack and state != "success":
        print(
            "::notice::MERGE_REQUIRE_SOP_ACK != 'true' — operator relax: "
            f"posting advisory success (full evaluation was {state}: {description!r})"
        )
        state = "success"
        description = f"ack advisory until agent-team — {description}"

    # Diagnostics to job log.
    print(f"::notice::PR #{args.pr} author={author} head={head_sha[:7]} mode={mode}")
    for it in items:
        slug = it["slug"]
        ackers = ack_state[slug]["ackers"]
        if ackers:
            print(f"::notice::  [PASS] {slug} — acked by {','.join(ackers)}")
        else:
            r = ack_state[slug]["rejected"]
            extras: list[str] = []
            if r["self_ack"]:
                extras.append(f"self-acks-rejected:{','.join(r['self_ack'])}")
            if r["not_in_team"]:
                extras.append(f"not-in-team:{','.join(r['not_in_team'])}")
            extra = " (" + "; ".join(extras) + ")" if extras else ""
            print(f"::notice::  [WAIT] {slug} — no valid peer-ack yet{extra}")

    print(f"::notice::posting status: state={state} desc={description!r}")

    if args.dry_run:
        print("::notice::--dry-run: not posting status")
        if args.exit_on_state:
            return 0 if state in ("success", "pending") else 1
        return 0

    target_url = f"https://{args.gitea_host}/{args.owner}/{args.repo}/pulls/{args.pr}"
    try:
        client.post_status(
            args.owner, args.repo, head_sha,
            state=state, context=args.status_context,
            description=description, target_url=target_url,
        )
        print(f"::notice::status posted: {args.status_context} → {state}")
    except RuntimeError as exc:
        # The gate token may lack write:repository (e.g. GITHUB_TOKEN in
        # Gitea Actions). When the gate logic itself succeeded, do not let
        # a 403 on the optional status post flip the job to failure — the
        # job's own check-run conclusion is what branch protection sees.
        # If the gate failed we re-raise so the job stays red.
        if state == "success" and "HTTP 403" in str(exc):
            print(
                "::warning::status post returned 403 (token lacks write:repository); "
                "gate logic succeeded so treating job as successful"
            )
            return 0
        raise
    # By default exit 0 — the POSTed status IS the gate, NOT the job
    # conclusion. If the job exits 1 BP will see TWO failure signals
    # (one from the job's auto-status, one from our POST), making the
    # description less actionable. --exit-on-state restores the old
    # behavior for local debugging.
    if args.exit_on_state:
        return 0 if state in ("success", "pending") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
