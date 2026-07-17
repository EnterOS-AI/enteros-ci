#!/usr/bin/env python3
"""check-askpass-drift — byte-identity gate for the molecule-askpass helper.

The `molecule-askpass` GIT_ASKPASS helper is byte-copied into every maintained
workspace-runtime TEMPLATE repo (claude-code, codex, hermes, openclaw). molecule-
core's applyAgentGitIdentity wires `GIT_ASKPASS=/usr/local/bin/molecule-askpass`
into every workspace, so all four images MUST ship the identical helper bytes —
a silent divergence in one template would give that runtime a subtly different
credential-prompt behaviour than the platform contract assumes.

This is the same "two byte-identical copies with no guard WILL diverge silently"
class that `scripts-sync.yml` guards for the vendored `.molecule-ci/scripts/`
mirror — except the four copies live in FOUR separate repos, so the compare is a
cross-repo network fetch rather than a local diff. It is therefore FAIL-CLOSED on
any fetch/auth error (exit 2): a token that cannot read a template repo, or a
transient API error, MUST NOT green the gate — it has not verified the invariant.

The durable fix for this whole drift class is a shared workspace-base image
(separate owner-gated RFC); this gate is the interim guard so a divergence is
caught loudly instead of shipping into a runtime image.

Filename note (E1 drift-audit fix): the helper is installed in-container at the
uniform path /usr/local/bin/molecule-askpass in all four images, but historically
hermes carried the SOURCE file as `scripts/git-askpass.sh` while the others use
`scripts/molecule-askpass`. This gate compares BYTES regardless of source
filename by trying each candidate source path per repo, so it stays correct
during the filename-unification transition and after.

Env
---
  GITEA_TOKEN     — repo-read token (DRIFT_BOT_TOKEN works; repo-admin ⊇ read)
  GITEA_HOST      — defaults to git.moleculesai.app
  ASKPASS_REPOS   — comma-separated owner/name list; defaults to the 4 templates
  ASKPASS_REF     — git ref to read; defaults to main

Exit codes
----------
  0 — all copies present and byte-identical.
  1 — copies fetched but they DIVERGE (real drift).
  2 — fail-closed: env contract violation, or a copy could not be fetched
      (auth/404/transient). The gate MUST NOT green when it cannot verify.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# The four maintained workspace-runtime template repos (the audit's set).
DEFAULT_REPOS = (
    "molecule-ai/molecule-ai-workspace-template-claude-code",
    "molecule-ai/molecule-ai-workspace-template-codex",
    "molecule-ai/molecule-ai-workspace-template-hermes",
    "molecule-ai/molecule-ai-workspace-template-openclaw",
)

# Candidate SOURCE paths for the helper, tried in order per repo. The
# in-container install path is uniformly /usr/local/bin/molecule-askpass; only
# the source filename differed historically (hermes: git-askpass.sh).
CANDIDATE_PATHS = (
    "scripts/molecule-askpass",
    "scripts/git-askpass.sh",
)


class DriftError(Exception):
    """Raised when the fetched copies are not byte-identical."""


class FetchError(Exception):
    """Raised (fail-closed) when a copy cannot be fetched/verified."""


def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key, default)
    return v if v is not None else default


def api(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
) -> tuple[str, Any]:
    """Gitea REST helper. Returns (status, payload) with status ∈
    {"ok", "not_found", "forbidden", "error"}. Mirrors the canonical
    Cloudflare-safe UA contract used by the other molecule-ci Gitea clients
    (curl/8.4.0 UA, 30s timeout) — see test_canonical_gitea_user_agent.py.
    """
    import json

    host = _env("GITEA_HOST", "git.moleculesai.app")
    token = _env("GITEA_TOKEN")
    url = f"https://{host}/api/v1{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers = {
        "Accept": "application/json",
        # CF WAF in front of git.moleculesai.app 1010-bans the default
        # Python-urllib UA; send a non-urllib UA so this reaches Gitea
        # (transport-only — auth/method/semantics unchanged).
        "User-Agent": "curl/8.4.0",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, method=method, headers=headers)
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
    except (urllib.error.URLError, TimeoutError, ValueError):
        return ("error", None)


def fetch_askpass(repo: str, ref: str = "main") -> bytes:
    """Fetch the helper bytes from one repo, trying each candidate source path.

    Fail-closed: raises FetchError on auth failure, transient error, or when no
    candidate path exists (a template that dropped the helper entirely is a
    verification failure, not a pass).
    """
    saw_not_found_all = True
    for candidate in CANDIDATE_PATHS:
        owner, name = repo.split("/", 1)
        status, payload = api(
            "GET",
            f"/repos/{owner}/{name}/contents/{urllib.parse.quote(candidate)}",
            query={"ref": ref},
        )
        if status == "ok" and isinstance(payload, dict):
            if payload.get("encoding") != "base64" or "content" not in payload:
                raise FetchError(
                    f"{repo}:{candidate} contents API returned no base64 content"
                )
            try:
                return base64.b64decode(payload["content"])
            except (ValueError, TypeError) as e:
                raise FetchError(f"{repo}:{candidate} base64 decode failed: {e}")
        if status in ("forbidden", "error"):
            # Auth/transient — cannot verify. Fail closed immediately.
            raise FetchError(
                f"{repo}:{candidate} fetch failed (status={status}); "
                "failing closed"
            )
        # status == "not_found": try the next candidate path.
        saw_not_found_all = saw_not_found_all and True
    raise FetchError(
        f"{repo}: no askpass helper found at any of {list(CANDIDATE_PATHS)}"
    )


def assert_identical(blobs: dict[str, bytes]) -> str:
    """Assert every blob is byte-identical. Returns the shared sha256 hex.

    Raises DriftError on divergence, FetchError on an empty/missing blob.
    Pure function — no network — so the negative control is unit-testable.
    """
    if not blobs:
        raise FetchError("no askpass copies to compare")
    digests: dict[str, str] = {}
    for repo, data in blobs.items():
        if not data:
            raise FetchError(f"{repo}: empty askpass copy")
        digests[repo] = hashlib.sha256(data).hexdigest()
    distinct = set(digests.values())
    if len(distinct) != 1:
        lines = "\n".join(
            f"  {digests[r]}  {r}  ({len(blobs[r])} B)" for r in sorted(digests)
        )
        raise DriftError(
            "molecule-askpass copies DIVERGE across template repos:\n" + lines
        )
    return next(iter(distinct))


def main() -> int:
    repos = [
        r.strip()
        for r in _env("ASKPASS_REPOS", ",".join(DEFAULT_REPOS)).split(",")
        if r.strip()
    ]
    ref = _env("ASKPASS_REF", "main")
    if not _env("GITEA_TOKEN"):
        sys.stderr.write("::error::missing required env var: GITEA_TOKEN\n")
        return 2
    try:
        blobs = {repo: fetch_askpass(repo, ref) for repo in repos}
    except FetchError as e:
        sys.stderr.write(f"::error::fail-closed: {e}\n")
        return 2
    try:
        shared = assert_identical(blobs)
    except DriftError as e:
        sys.stderr.write(f"::error::{e}\n")
        return 1
    except FetchError as e:
        sys.stderr.write(f"::error::fail-closed: {e}\n")
        return 2
    sys.stdout.write(
        f"OK: {len(blobs)} molecule-askpass copies byte-identical "
        f"(sha256={shared}, {len(next(iter(blobs.values())))} B)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
