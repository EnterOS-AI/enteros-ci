#!/usr/bin/env python3
"""check_repo_meta_presence — the repo-meta presence gate (CI-RFC Piece 3).

Rule
----
Every KNOWN-LAYER repo in the org MUST carry a valid ``repo-meta.yaml`` at its
root (schema_version 1 + a layer from the SDK-owned enum). A known-layer repo
that lacks it — or carries an unparseable / schema-invalid one — is a hole in
the capability→bundle router: it silently attaches NO derived CI bundle, the
exact "zero-workflow repo merges green" class this gate closes.

Scope (Phase 1, deliberately)
-----------------------------
KNOWN_LAYER_REPOS is an explicit allowlist of the MAINTAINED service / template /
contract / org-template repos. Plugin repos (`molecule-ai-plugin-*`) and retired
runtime templates are intentionally OUT of scope for now — adopt them, then add
them here to widen the gate. An allowlist (not "enumerate every org repo") keeps
the gate deterministic and avoids failing on inactive/ambiguous repos.

Why fail-CLOSED
---------------
An auth failure, an API error, or a missing/invalid manifest all return nonzero.
A presence gate that greened when it could not verify would be worse than none.

Sentinel
--------
Prints ``repo-meta-presence:executed`` on every run. Under molecule-core-style
BP=['*'] a hollow-green (job cancelled / never really ran) must not pass as
success; a downstream check can assert the sentinel was printed (internal#1000).

Exit codes
----------
  0 — every known-layer repo has a valid repo-meta.yaml.
  1 — at least one known-layer repo is missing / has an invalid manifest.
  2 — env contract violation, or a fail-closed verification error (auth / API).

Env
---
  GITEA_TOKEN  — repo-read token (org-read for the listing is NOT needed; this
                 gate GETs each repo's contents by explicit name).
  GITEA_HOST   — e.g. git.moleculesai.app
  ORG          — org owner (default: molecule-ai)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

try:
    import yaml
except ImportError:
    sys.stderr.write("::error::PyYAML is required (pip install PyYAML)\n")
    sys.exit(2)

# The SDK-owned layer enum (schemas/repo-meta.schema.json). Kept in sync by the
# schema-sync gate; duplicated here only as the presence check's acceptance set.
VALID_LAYERS = {"service", "runtime-template", "plugin", "org-template", "contract"}

# Phase-1 maintained known-layer set. Widen as adoption expands (plugins later).
KNOWN_LAYER_REPOS = [
    # services
    "molecule-core",
    "molecule-controlplane",
    "molecule-app",
    "molecule-mcp-server",
    "molecule-ai-workspace-runtime",
    "molecule-ci",
    # contract
    "molecule-ai-sdk",
    # runtime templates (the maintained runtimes)
    "molecule-ai-workspace-template-claude-code",
    "molecule-ai-workspace-template-codex",
    "molecule-ai-workspace-template-hermes",
    "molecule-ai-workspace-template-openclaw",
    # org templates
    "molecule-ai-org-template-molecule-dev",
    "molecule-ai-org-template-reno-stars",
    "molecule-ai-org-template-molecule-worker-gemini",
    "molecule-ai-org-template-ux-ab-lab",
]


def _env(key: str, default: str | None = None) -> str:
    val = os.environ.get(key, default)
    if val is None:
        sys.stderr.write(f"::error::missing required env var: {key}\n")
        sys.exit(2)
    return val


# CF-safe UA (matches the sibling drift gates): the Cloudflare edge fronting the
# forge answers a bot challenge to a bare `Python-urllib/*` UA.
_UA = "curl/8.4.0"


def _get(url: str, token: str, timeout: int = 15):
    """One authenticated GET, retried once. Returns (http_code|None, body|None, err|None).

    http_code is the definite HTTP status when the server answered; it is None
    (with `err` set) on a transport failure or a 200 whose body is not JSON (e.g.
    a Cloudflare challenge page). A non-JSON 200 is NEVER treated as a real
    result — the caller fails closed rather than mistaking it for 'missing'.
    Retry-once so a single transient blip cannot red the gate (15s×2 keeps 15
    repos well under the 5-minute job cap).
    """
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"token {token}", "User-Agent": _UA, "Accept": "application/json"},
    )
    last_err = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            try:
                return (200, json.loads(raw.decode("utf-8")), None)
            except (ValueError, UnicodeDecodeError) as exc:
                return (None, None, f"non-JSON 200 body ({exc})")
        except urllib.error.HTTPError as exc:
            return (exc.code, None, None)  # a definite HTTP status — do not retry
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = str(exc)
            continue
    return (None, None, last_err or "request failed")


def fetch_repo_meta(api: str, org: str, repo: str, token: str) -> tuple[str, str | None]:
    """Return (status, raw_yaml). status ∈ {'ok','missing','error'}.

    A manifest 404 is 'missing' ONLY when the repo itself is reachable. A 404
    caused by a renamed/archived repo or a token that cannot see the repo (Gitea
    404s inaccessible repos to avoid leaking their existence) is 'error' — fail
    closed, NOT a false 'add a manifest'. A non-JSON / undecodable body is also
    'error', never a silent crash.
    """
    code, body, err = _get(f"{api}/repos/{org}/{repo}/contents/repo-meta.yaml", token)
    if code == 200:
        try:
            import base64

            content = base64.b64decode((body or {}).get("content", "")).decode("utf-8")
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            sys.stderr.write(f"::error::{repo}: repo-meta.yaml body undecodable ({exc})\n")
            return ("error", None)
        return ("ok", content)
    if code == 404:
        rcode, _, rerr = _get(f"{api}/repos/{org}/{repo}", token)
        if rcode == 200:
            return ("missing", None)
        sys.stderr.write(
            f"::error::{repo}: manifest 404 but repo GET returned {rcode or rerr} — "
            f"repo renamed/archived or token lacks access; cannot verify\n"
        )
        return ("error", None)
    # 401/403/5xx, transport failure, or non-JSON 200 — cannot verify; fail closed.
    sys.stderr.write(f"::error::{repo}: repo-meta.yaml GET {code or err}\n")
    return ("error", None)


def validate_manifest(raw: str) -> str | None:
    """Return an error string if invalid, else None."""
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return f"unparseable YAML ({exc})"
    if not isinstance(doc, dict):
        return "not a mapping"
    if doc.get("schema_version") != 1:
        return f"schema_version must be 1 (got {doc.get('schema_version')!r})"
    layer = doc.get("layer")
    if layer not in VALID_LAYERS:
        return f"layer {layer!r} not in {sorted(VALID_LAYERS)}"
    return None


def run() -> int:
    print("repo-meta-presence:executed")
    token = _env("GITEA_TOKEN")
    host = _env("GITEA_HOST")
    org = _env("ORG", "molecule-ai")
    api = f"https://{host}/api/v1"

    missing: list[str] = []
    invalid: list[str] = []
    errors: list[str] = []
    ok = 0
    for repo in KNOWN_LAYER_REPOS:
        status, raw = fetch_repo_meta(api, org, repo, token)
        if status == "missing":
            missing.append(repo)
        elif status == "error":
            errors.append(repo)
        else:
            err = validate_manifest(raw or "")
            if err:
                invalid.append(f"{repo}: {err}")
            else:
                ok += 1

    # Print EVERY category first, so a transient/unverifiable repo can never HIDE
    # a genuinely-missing or invalid manifest surfaced in the same run.
    for r in missing:
        print(
            f"::error::{r} is a known-layer repo but has NO repo-meta.yaml. "
            f"Add one (schema_version: 1 + layer) so meta-CI can derive its "
            f"bundle. See molecule-ci/schemas/repo-meta.schema.json."
        )
    for r in invalid:
        print(f"::error::invalid repo-meta.yaml — {r}")
    if errors:
        print(
            f"::error::could NOT verify {len(errors)} repo(s) "
            f"(auth/API/rename): {', '.join(errors)}. Failing closed."
        )

    # A definite missing/invalid is the most actionable outcome — surface it
    # (exit 1) even alongside an unverifiable repo. Only an unverifiable-with-no-
    # definite-finding run exits 2 ("could not verify"). Both are merge-blocking.
    if missing or invalid:
        print(
            f"::error::repo-meta presence gate FAILED: {len(missing)} missing, "
            f"{len(invalid)} invalid, {len(errors)} unverifiable, {ok} ok, of "
            f"{len(KNOWN_LAYER_REPOS)} known-layer repos."
        )
        return 1
    if errors:
        return 2

    print(
        f"::notice::repo-meta presence gate OK — all "
        f"{len(KNOWN_LAYER_REPOS)} known-layer repos carry a valid repo-meta.yaml."
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
