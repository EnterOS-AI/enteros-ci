#!/usr/bin/env python3
"""meta-ci — the capability→bundle router / dispatcher (task internal#57, RFC: org CI-enforcement).

PHASE 1 (ADVISORY). This is the *spine* of the SSOT-owned, capability-auto-attached
CI-enforcement design. It reads a repo's ``repo-meta.yaml`` (the SDK-owned SSOT
manifest — schema vendored at ``schemas/repo-meta.schema.json``), and DERIVES the
set of CI capability-bundles that repo should carry, from two axes UNION'd + deduped:

  * its ``layer`` (service | runtime-template | plugin | org-template | contract), and
  * each declared ``capability`` (go-service, adapter, mcp-server-bake, ...).

Live ``waivers`` (``until`` in the future) suppress a named bundle; expired waivers
are ignored (the bundle re-attaches) and warned. Unknown capabilities attach no
bundle and are warned (never error) — the forward-compat posture the schema mandates.

WHAT PHASE 1 DOES vs DOES NOT DO (capture-first, enforce-later)
--------------------------------------------------------------
Phase 1 is the DERIVATION + REPORT spine plus the cheap, universally-safe bundle
runners. It:
  * schema-validates ``repo-meta.yaml`` (a malformed manifest is a HARD error), then
  * derives + prints the bundle PLAN (which bundles attach, and why), then
  * EXECUTES the bundle runners that are already canonical + safe to run in-repo
    (today: ``secret-scan``, the self-guarding ``node-install-lint-typecheck-build``
    node bundle, and credential-free immutable-artifact ``mcp-pin-lockstep``), and
    REPORTS the rest as ``planned`` (execution wired in Phase 2 — this file
    deliberately does not fork heavy go / docker-build / t4 / codegen bundles yet).

The aggregate result is: manifest-valid AND every EXECUTED runner passed. ``planned``
bundles are surfaced but neutral. This keeps the advisory spine honest — it reds a
genuinely broken repo-meta or a real secret leak, and is quiet otherwise — without
pretending to run bundles it has not wired.

EMITTING A SINGLE AGGREGATE CONTEXT (Gitea Actions 1.26.4)
---------------------------------------------------------
The matrix over bundles is executed IN-PROCESS (a loop here), NOT as a GHA ``matrix:``
fan-out and NOT via cross-repo ``workflow_call``. Two deployment facts force this:
  * a GHA ``matrix:`` emits ONE commit-status context per leg, but the design (and the
    ``["*"]`` branch-protection posture) needs exactly ONE aggregate context; and
  * cross-repo ``workflow_call`` is NOT a trustworthy gate on this Gitea (internal#1000:
    a consumer can be recorded green with ``steps=[]``).
So the repository-local workflow runs THIS one script in a single job and emits one
job context. A sentinel line is printed and asserted so a hollow/no-op invocation
(internal#1000) cannot be counted green.

Exit codes
----------
  0 — manifest valid AND all EXECUTED runners passed (planned bundles neutral).
  1 — manifest INVALID, or an executed runner FAILED.
  2 — usage / environment error (no repo-meta.yaml, unreadable schema, bad args).

Usage
-----
  python3 scripts/meta-ci.py --repo-root .            # derive + run against a repo
  python3 scripts/meta-ci.py --repo-root . --plan-json  # print the derived plan as JSON, no runners
  python3 scripts/meta-ci.py --repo-root . --plan-only  # derive + print, skip runners (dry)
"""
from __future__ import annotations

import argparse
import ast
import base64
import datetime as _dt
import gzip
import hashlib
import hmac
import http.client
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SENTINEL = "meta-ci:sentinel:executed"  # printed once; proves this script actually ran.

# ---------------------------------------------------------------------------
# The capability→bundle map (SSOT for the router). Kept in this one place so the
# derivation has a single machine-readable home. LAYER_BUNDLES is the per-repo-kind
# baseline; CAPABILITY_BUNDLES is the per-capability add-on; the derived set is their
# UNION (deduped). ``secret-scan`` is a universal baseline attached to every repo.
# ---------------------------------------------------------------------------
UNIVERSAL_BUNDLES: tuple[str, ...] = ("secret-scan",)

LAYER_BUNDLES: dict[str, tuple[str, ...]] = {
    "service": ("go-build-vet-lint-test", "secret-scan"),
    "runtime-template": (
        "adapter-conformance",
        "docker-build-smoke",
        "t4-assert",
        "secret-scan",
    ),
    "plugin": ("plugin-manifest-validate", "secret-scan"),
    "org-template": ("org-template-validate", "secret-scan"),
    "contract": ("contracts-codegen-drift", "secret-scan"),
}

CAPABILITY_BUNDLES: dict[str, tuple[str, ...]] = {
    "go-service": ("go-build-vet-lint-test",),
    "python-package": ("py-ruff-pytest-build",),
    "node-package": ("node-install-lint-typecheck-build",),
    "adapter": ("adapter-conformance",),
    "mcp-server-bake": ("mcp-pin-lockstep",),
    "skills": ("skill-lint",),
    "settings-fragment": ("settings-fragment-validate",),
    "env-mutator": ("go-env-mutator-checks",),
    "docker-image": ("docker-build-smoke",),
}

# Kept byte-in-sync with the vendored schema $defs/knownCapability + the SDK
# validator's KNOWN_CAPABILITIES (asserted by test_meta_ci.py).
KNOWN_CAPABILITIES = frozenset(CAPABILITY_BUNDLES)
LAYERS = frozenset(LAYER_BUNDLES)
CAPABILITY_RE = re.compile(r"^(x-)?[a-z0-9]+(-[a-z0-9]+)*$")

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "repo-meta.schema.json"

# Credential-free, read-only package endpoints used by the mcp-pin-lockstep
# runner. The runtime index publishes an immutable sha256 with every exact wheel;
# the exact runtime wheel then names the exact MCP npm package artifact to check.
MOLECULE_RUNTIME_INDEX_URL = (
    "https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/"
    "molecules-workspace-runtime/"
)
_PACKAGE_HOST = "git.moleculesai.app"
_PACKAGE_ORIGIN = ("https", _PACKAGE_HOST, 443)
_HTTP_ATTEMPT_TIMEOUT_SECONDS = 10
_HTTP_MAX_ATTEMPTS = 3
_HTTP_RETRY_DELAY_SECONDS = 0.25
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_WHEEL_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
_MAX_TAR_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_BYTES = 2 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 10_000
_STABLE_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class MetaCIError(Exception):
    """Fatal, well-formed error (usage / environment). Maps to exit 2."""


# ---------------------------------------------------------------------------
# Manifest load + validation
# ---------------------------------------------------------------------------
def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - CI always has pyyaml
        raise MetaCIError("pyyaml is required to parse repo-meta.yaml") from exc
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise MetaCIError(f"repo-meta.yaml is not valid YAML: {exc}") from exc


def _schema_errors(manifest: Any) -> list[str]:
    """Validate against the vendored schema. Empty list if jsonschema/schema absent
    (the structural checks below still run — same degrade posture as the SDK validator)."""
    try:
        schema = json.loads(_SCHEMA_PATH.read_text())
    except (OSError, ValueError):
        return []
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:  # pragma: no cover - CI installs jsonschema
        return []
    v = Draft202012Validator(schema, format_checker=FormatChecker())
    out: list[str] = []
    for err in sorted(v.iter_errors(manifest), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        out.append(f"{loc}: {err.message}")
    return out


def validate_manifest(manifest: Any) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Mirrors the SDK validate_repo_meta semantics:
    strict structure is an error; an unknown-but-well-formed capability is a warning."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(manifest, dict):
        return (["repo-meta.yaml must be a mapping/object"], warnings)

    errors.extend(_schema_errors(manifest))

    if manifest.get("schema_version") != 1:
        errors.append(
            f"schema_version must be the integer 1; got {manifest.get('schema_version')!r}"
        )

    layer = manifest.get("layer")
    if "layer" not in manifest:
        errors.append("missing required field: layer")
    elif layer not in LAYERS:
        errors.append(f"layer={layer!r} — must be one of {sorted(LAYERS)}")

    caps = manifest.get("capabilities", [])
    if caps is not None and not isinstance(caps, list):
        errors.append("capabilities must be a list")
        caps = []
    seen: set[str] = set()
    for i, cap in enumerate(caps or []):
        if not isinstance(cap, str):
            errors.append(f"capabilities[{i}] must be a string; got {cap!r}")
            continue
        if cap in seen:
            errors.append(f"capabilities[{i}]: duplicate capability {cap!r}")
        seen.add(cap)
        if not CAPABILITY_RE.match(cap):
            errors.append(
                f"capabilities[{i}]={cap!r} — must be lowercase kebab-case "
                f"(pattern {CAPABILITY_RE.pattern}); optionally 'x-'-prefixed"
            )
        elif cap not in KNOWN_CAPABILITIES:
            warnings.append(
                f"capabilities[{i}]={cap!r} is not a KNOWN capability — attaches no CI "
                "bundle (forward-compat placeholder or a typo)"
            )

    waivers = manifest.get("waivers")
    if waivers is not None and not isinstance(waivers, list):
        errors.append("waivers must be a list")
    return (errors, warnings)


# ---------------------------------------------------------------------------
# Bundle derivation
# ---------------------------------------------------------------------------
def _live_waived_bundles(manifest: dict, today: _dt.date) -> tuple[set[str], list[str]]:
    """Return (bundles suppressed by a LIVE waiver, notices). A waiver is live while
    today < until; on/after until it is dead (bundle re-attaches) and we warn."""
    suppressed: set[str] = set()
    notices: list[str] = []
    for w in manifest.get("waivers") or []:
        if not isinstance(w, dict):
            continue
        bundle = w.get("bundle")
        until = w.get("until")
        if not (isinstance(bundle, str) and isinstance(until, str)):
            continue
        try:
            exp = _dt.date.fromisoformat(until)
        except ValueError:
            continue
        if today < exp:
            suppressed.add(bundle)
            notices.append(f"waiver LIVE: bundle {bundle!r} suppressed until {until} — {w.get('reason','')}")
        else:
            notices.append(
                f"waiver EXPIRED: bundle {bundle!r} until {until} is past — bundle re-attached"
            )
    return suppressed, notices


def derive_bundles(manifest: dict, today: _dt.date | None = None) -> dict[str, Any]:
    """Derive the attached bundle set from a VALID manifest. UNION of the layer
    baseline, per-capability add-ons, and the universal baseline; minus live-waived
    bundles; deduped + sorted. Returns the plan (also serialisable for --plan-json)."""
    today = today or _dt.date.today()
    layer = manifest.get("layer")
    caps = [c for c in (manifest.get("capabilities") or []) if isinstance(c, str)]

    attached: set[str] = set(UNIVERSAL_BUNDLES)
    attached.update(LAYER_BUNDLES.get(layer, ()))
    unknown_caps: list[str] = []
    for cap in caps:
        if cap in CAPABILITY_BUNDLES:
            attached.update(CAPABILITY_BUNDLES[cap])
        elif cap not in KNOWN_CAPABILITIES:
            unknown_caps.append(cap)

    suppressed, waiver_notices = _live_waived_bundles(manifest, today)
    effective = sorted(attached - suppressed)

    return {
        "layer": layer,
        "capabilities": caps,
        "unknown_capabilities": unknown_caps,
        "bundles_all": sorted(attached),
        "bundles_waived": sorted(attached & suppressed),
        "bundles_effective": effective,
        "waiver_notices": waiver_notices,
    }


# ---------------------------------------------------------------------------
# Bundle runners (Phase 1: only the cheap, universally-safe ones execute)
# ---------------------------------------------------------------------------
def _run_secret_scan(repo_root: Path) -> tuple[bool, str]:
    """Run the canonical secret-scan over the repo. Executes when check-secrets.py is
    co-located with this script (i.e. run from a molecule-ci checkout)."""
    scanner = Path(__file__).resolve().parent / "check-secrets.py"
    if not scanner.exists():
        return True, "skipped (check-secrets.py not co-located)"
    proc = subprocess.run(
        [sys.executable, str(scanner)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    return proc.returncode == 0, " | ".join(tail)


# ---------------------------------------------------------------------------
# node-package runner. UNLIKE the go / python / docker language bundles (still
# 'planned' — they need heavyweight toolchains / registries wired in Phase 2),
# the node bundle is safe to EXECUTE in-process in Phase 1: it no-ops to a clean
# PASS when there is no package.json (nothing to check) and only runs the scripts
# the repo actually declares. It does NOT green-skip a missing package manager,
# though — a repo that DECLARES node-package on a runner lacking its manager is a
# mis-provisioned runner and FAILS CLOSED (an unrun lint/typecheck/build must
# never masquerade as a passing leg). Every step is also run under a bounded
# timeout so a hang can never wedge the job. This gives the deferred Node/TS repos
# REAL coverage now instead of a planned placeholder.
#
# It detects the package manager from the lockfile (precedence pnpm > yarn > npm;
# a package.json with NO lockfile can't be frozen-installed, so it degrades to a
# non-frozen `npm install`), runs a frozen install, then runs ONLY the repo's OWN
# declared lint / typecheck / build scripts (skip-if-absent — it never invents a
# script the repo does not declare). This one capability covers frontend apps
# (which declare `build`) and TS/JS services alike; a distinct `frontend`
# capability is deliberately NOT added (see docs/meta-ci.md).
# ---------------------------------------------------------------------------
# lockfile basename -> (manager, frozen-install argv). Ordered = precedence.
_NODE_LOCKFILES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("pnpm-lock.yaml", "pnpm", ("pnpm", "install", "--frozen-lockfile")),
    ("yarn.lock", "yarn", ("yarn", "install", "--frozen-lockfile")),
    ("package-lock.json", "npm", ("npm", "ci")),
    ("npm-shrinkwrap.json", "npm", ("npm", "ci")),
)
# The scripts the bundle opts into IF the repo declares them, in run order.
_NODE_BUNDLE_SCRIPTS: tuple[str, ...] = ("lint", "typecheck", "build")
# Per-step wall-clock cap for the node install / lint / typecheck / build
# subprocesses. A watch/hanging build (or one blocked on missing env/secrets) must
# never block meta-ci indefinitely — on expiry the step is a clear FAILURE, so a
# hang can never wedge the job. 10 min comfortably covers a cold frozen install +
# a real build while bounding the pathological hang.
_NODE_STEP_TIMEOUT_SEC = 600


def _node_install_plan(repo_root: Path) -> tuple[str, list[str]] | None:
    """Return (manager, install_argv) for the repo, or None when there is no
    package.json (the bundle then no-ops). Package-manager precedence is
    pnpm > yarn > npm by lockfile; a package.json with NO lockfile can't be
    frozen-installed, so it degrades to a plain (non-frozen) `npm install`."""
    if not (repo_root / "package.json").exists():
        return None
    for lock, manager, argv in _NODE_LOCKFILES:
        if (repo_root / lock).exists():
            return manager, list(argv)
    return "npm", ["npm", "install", "--no-audit", "--no-fund"]


def _node_declared_scripts(repo_root: Path) -> set[str]:
    """The set of npm-script names the repo declares (empty on any parse error)."""
    try:
        pkg = json.loads((repo_root / "package.json").read_text())
    except (OSError, ValueError):
        return set()
    scripts = pkg.get("scripts")
    return set(scripts) if isinstance(scripts, dict) else set()


def node_bundle_steps(repo_root: Path) -> list[tuple[str, list[str]]] | None:
    """Ordered (label, argv) steps for the node bundle: the install, then each of
    lint / typecheck / build the repo DECLARES (skip-if-absent). None => there is
    no package.json (the bundle no-ops). Pure + deterministic so it is unit-tested
    without shelling out to a package manager."""
    plan = _node_install_plan(repo_root)
    if plan is None:
        return None
    manager, install_argv = plan
    steps: list[tuple[str, list[str]]] = [("install", install_argv)]
    declared = _node_declared_scripts(repo_root)
    for script in _NODE_BUNDLE_SCRIPTS:
        if script in declared:
            steps.append((script, [manager, "run", script]))
    return steps


def _run_node_package(repo_root: Path) -> tuple[bool, str]:
    """Execute the node bundle: frozen install + the repo's declared lint /
    typecheck / build. No-ops to a clean PASS only when there is no package.json
    (the repo has no node bundle to run). If the repo DECLARES node-package but the
    package-manager binary is absent, the runner is mis-provisioned and this FAILS
    CLOSED — an unrun lint/typecheck/build must never count as a passing leg in the
    'every executed runner green' aggregate (that was a silent false-green)."""
    steps = node_bundle_steps(repo_root)
    if steps is None:
        return True, "skipped (no package.json)"
    manager = steps[0][1][0]
    if shutil.which(manager) is None:
        # Fail closed, not a green skip: this repo declares node-package, so a
        # missing manager means the RUNNER is wrong (image/lockfile disagree), not
        # that the repo has nothing to check. Report it actionably.
        return False, (
            f"{manager} not installed on runner but repo '{repo_root.name}' "
            f"declares node-package — lint/typecheck/build UNVERIFIED (runner "
            f"mis-provisioned; install {manager} on the runner image)"
        )
    ran: list[str] = []
    for label, argv in steps:
        try:
            proc = subprocess.run(
                argv,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=_NODE_STEP_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            # A hanging/watch step (or one blocked on missing env/secrets) must
            # surface as a clear failure, never block the job indefinitely.
            return False, (
                f"{label} timed out after {_NODE_STEP_TIMEOUT_SEC}s ({manager}) "
                f"in repo '{repo_root.name}' — a hanging step must never wedge "
                f"meta-ci"
            )
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
            return False, f"{label} failed ({manager}): " + " | ".join(tail)
        ran.append(label)
    if len(ran) > 1:
        return True, f"{manager}: ran " + ", ".join(ran)
    return True, f"{manager}: installed; no lint/typecheck/build scripts declared"


# ---------------------------------------------------------------------------
# mcp-server-bake runner. Runtime templates do not own a hand-typed MCP pin:
# they pin one exact molecules-workspace-runtime wheel and delegate their image
# bake to that wheel's prebake-mgmt-mcp.sh. This runner follows that real chain:
#
#   template .runtime-version -> immutable runtime wheel + sha256
#     -> packaged executable MCP constants + compatible range + prebake helper
#       -> immutable exact npm tarball + sha512/sha1
#
# Every missing/malformed/network/integrity condition is a hard failure. The
# check is credential-free and read-only; both registries are public. It does
# not run Docker or alter the existing Tier-4 live-container conformance gate.
# The source repo's top-level mcp-plugin-delivery contract is not in the wheel;
# SDK/runtime contract byte-sync remains its own gate. This runner verifies the
# executable constants and helper that the published image actually consumes.
# ---------------------------------------------------------------------------
class MCPPinLockstepError(Exception):
    """A fail-closed mcp-pin-lockstep contract violation."""


class _RuntimeWheelLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.hrefs.append(href)


def _https_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    return ("https", parsed.hostname.lower(), port or 443)


def _same_origin(left: str, right: str) -> bool:
    origin = _https_origin(left)
    return origin is not None and origin == _https_origin(right)


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject an off-origin Location before urllib issues the next request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _https_origin(newurl) != _PACKAGE_ORIGIN or not _same_origin(
            req.full_url, newurl
        ):
            raise MCPPinLockstepError(
                f"package request redirected off origin: {req.full_url} -> {newurl}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_PACKAGE_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


def _open_package_url(request: urllib.request.Request, *, timeout: int):
    return _PACKAGE_OPENER.open(request, timeout=timeout)


def _fetch_bytes(url: str) -> bytes:
    """Bounded credential-free GET with transient-only bounded retries.

    Transport failures, 429, and 5xx retry up to three 10-second attempts.
    Authentication and all other 4xx responses fail immediately.
    """
    if _https_origin(url) != _PACKAGE_ORIGIN:
        raise MCPPinLockstepError(f"refusing untrusted package URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0"})
    last_error: Exception | None = None
    for attempt in range(1, _HTTP_MAX_ATTEMPTS + 1):
        try:
            with _open_package_url(request, timeout=_HTTP_ATTEMPT_TIMEOUT_SECONDS) as response:
                final_url = response.geturl()
                if not _same_origin(url, final_url):
                    raise MCPPinLockstepError(
                        f"package request redirected off origin: {url} -> {final_url}"
                    )
                length = response.headers.get("Content-Length")
                if length and int(length) > _MAX_ARTIFACT_BYTES:
                    raise MCPPinLockstepError(
                        f"package response exceeds {_MAX_ARTIFACT_BYTES} bytes: {url}"
                    )
                payload = response.read(_MAX_ARTIFACT_BYTES + 1)
                if len(payload) > _MAX_ARTIFACT_BYTES:
                    raise MCPPinLockstepError(
                        f"package response exceeds {_MAX_ARTIFACT_BYTES} bytes: {url}"
                    )
                return payload
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and not 500 <= exc.code <= 599:
                raise MCPPinLockstepError(
                    f"package fetch failed for {url}: HTTP {exc.code} (not retryable)"
                ) from exc
            last_error = exc
        except (
            TimeoutError,
            ConnectionError,
            urllib.error.URLError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            last_error = exc
        except ValueError as exc:
            raise MCPPinLockstepError(f"malformed package response for {url}: {exc}") from exc
        if attempt < _HTTP_MAX_ATTEMPTS:
            time.sleep(_HTTP_RETRY_DELAY_SECONDS * attempt)
    raise MCPPinLockstepError(
        f"package fetch failed for {url} after {_HTTP_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def _exact_semver(value: str, label: str) -> tuple[int, int, int]:
    match = _STABLE_SEMVER_RE.fullmatch(value)
    if not match:
        raise MCPPinLockstepError(f"{label} must be an exact stable semver; got {value!r}")
    return tuple(int(part) for part in match.groups())


def _caret_contains(compatible: str, pinned: str) -> bool:
    if not compatible.startswith("^"):
        raise MCPPinLockstepError(
            f"MCP compatible range must be a caret stable semver; got {compatible!r}"
        )
    floor = _exact_semver(compatible[1:], "MCP compatible range floor")
    version = _exact_semver(pinned, "runtime MCP pinned version")
    if floor[0] > 0:
        ceiling = (floor[0] + 1, 0, 0)
    elif floor[1] > 0:
        ceiling = (0, floor[1] + 1, 0)
    else:
        ceiling = (0, 0, floor[2] + 1)
    return floor <= version < ceiling


def _continued_lines(contents: str) -> list[str]:
    logical: list[str] = []
    pending = ""
    for raw in contents.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pending += (" " if pending else "") + line.removesuffix("\\").rstrip()
        if line.endswith("\\"):
            continue
        logical.append(pending)
        pending = ""
    if pending:
        logical.append(pending)
    return logical


def _shell_commands(command: str) -> list[tuple[list[str], str | None]]:
    """Tokenize only the command/control edges this static gate needs.

    This is deliberately not a general shell parser. Quoting remains delegated to
    ``shlex``; the returned operator is the control token immediately following each
    command. Including redirection punctuation keeps ``>&2`` distinct from background
    ``&`` so masking decisions are made on real control edges.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    commands: list[tuple[list[str], str | None]] = []
    current: list[str] = []
    for word in lexer:
        if re.fullmatch(r"[;&|]+", word):
            if current:
                commands.append((current, word))
                current = []
            continue
        current.append(word)
    if current:
        commands.append((current, None))
    return commands


def _assignment_write(word: str) -> tuple[str, str, bool] | None:
    """Return a shell assignment write and whether it is a direct scalar ``=``."""
    match = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_]*)(\[[^]]+\])?(\+?=)(.*)", word, re.S
    )
    if match is None:
        return None
    return (
        match.group(1),
        match.group(4),
        match.group(2) is None and match.group(3) == "=",
    )


def _assignment(word: str) -> tuple[str, str] | None:
    write = _assignment_write(word)
    return (write[0], write[1]) if write is not None and write[2] else None


def _command_words(segment: list[str]) -> list[str]:
    index = 0
    while index < len(segment) and _assignment_write(segment[index]) is not None:
        index += 1
    return segment[index:]


def _assignment_updates(
    segment: list[str],
) -> list[tuple[str, str, bool, bool, bool]]:
    """Return writes as ``(name, value, persists, is_direct, establishes)``.

    Declaration builtins are writes, but never proof sources: Bash can report a
    successful ``export``/``local``/``readonly`` even when its substitution failed.
    """
    leading: list[tuple[str, str, bool]] = []
    index = 0
    while index < len(segment):
        write = _assignment_write(segment[index])
        if write is None:
            break
        leading.append(write)
        index += 1
    words = segment[index:]
    updates = [
        (name, value, not words, is_direct, True)
        for name, value, is_direct in leading
    ]
    if words and words[0] in {"declare", "export", "local", "readonly", "typeset"}:
        updates.extend(
            (name, value, True, is_direct, False)
            for word in words[1:]
            if (write := _assignment_write(word)) is not None
            for name, value, is_direct in [write]
        )
    return updates


def _literal_shell_variable(value: str) -> str | None:
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^]]+\])?", value)
    return match.group(1) if match else None


def _resolve_nameref(name: str, namerefs: dict[str, str | None]) -> str | None:
    seen: set[str] = set()
    while name in namerefs:
        if name in seen or namerefs[name] is None:
            return None
        seen.add(name)
        name = namerefs[name]
    return name


def _record_nameref_declarations(
    words: list[str], namerefs: dict[str, str | None]
) -> set[str]:
    if not words or words[0] not in {"declare", "local", "typeset"}:
        return set()
    index = 1
    nameref_mode: bool | None = None
    while index < len(words) and words[index].startswith(("-", "+")):
        option = words[index]
        if option == "--":
            index += 1
            break
        if "n" in option[1:]:
            nameref_mode = option.startswith("-")
        index += 1
    if nameref_mode is None:
        return set()

    declared: set[str] = set()
    for operand in words[index:]:
        write = _assignment_write(operand)
        if write is not None:
            name, target, is_direct = write
            if nameref_mode:
                namerefs[name] = (
                    _literal_shell_variable(target) if is_direct else None
                )
            else:
                namerefs.pop(name, None)
            declared.add(name)
            continue
        name = _literal_shell_variable(operand)
        if name is not None:
            if nameref_mode:
                namerefs[name] = None
            else:
                namerefs.pop(name, None)
            declared.add(name)
    return declared


def _stateful_shell_writes(
    segment: list[str], namerefs: dict[str, str | None]
) -> tuple[set[str], bool]:
    """Return variables a shell construct may write and whether its target is unknown.

    Only parent-shell mutation surfaces needed by the accepted grammar are modeled.
    Dynamic targets plus eval/source/arithmetic forms are deliberately unknown so callers
    invalidate all proof rather than guessing.
    """
    words = _command_words(segment)
    declared = _record_nameref_declarations(words, namerefs)
    writes: set[str] = set()
    unknown = False

    def record(target: str) -> None:
        nonlocal unknown
        name = _literal_shell_variable(target)
        if name is None:
            unknown = True
            return
        resolved = _resolve_nameref(name, namerefs)
        if resolved is None:
            unknown = True
        else:
            writes.add(resolved)

    for name, _, _, _, _ in _assignment_updates(segment):
        if name in namerefs and name not in declared:
            resolved = _resolve_nameref(name, namerefs)
            if resolved is None:
                unknown = True
            else:
                writes.add(resolved)
        else:
            writes.add(name)

    if not words:
        return writes, unknown
    if words[0] in {"!", "elif", "if", "until", "while"}:
        condition = words[1:]
        if condition and condition[0] == "!":
            condition = condition[1:]
        condition_writes, condition_unknown = _stateful_shell_writes(
            condition, namerefs
        )
        writes.update(condition_writes)
        return writes, unknown or condition_unknown
    if words[0] in {"builtin", "command"}:
        index = 1
        while index < len(words) and words[index].startswith("-"):
            if words[0] == "command" and set(words[index][1:]) & {"v", "V"}:
                return writes, unknown
            index += 1
        words = words[index:]
        if not words:
            return writes, unknown

    command = words[0]
    if command == "printf":
        index = 1
        while index < len(words):
            option = words[index]
            if option == "--":
                break
            if option == "-v" and index + 1 < len(words):
                record(words[index + 1])
                break
            if option.startswith("-v") and len(option) > 2:
                record(option[2:])
                break
            if not option.startswith("-"):
                break
            index += 1
    elif command == "read":
        index = 1
        read_targeted = False
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            if option == "--":
                index += 1
                break
            flags = option[1:]
            for flag_index, flag in enumerate(flags):
                if flag not in {"a", "d", "i", "n", "N", "p", "t", "u"}:
                    continue
                argument = flags[flag_index + 1 :]
                if not argument:
                    if index + 1 >= len(words):
                        unknown = True
                        break
                    index += 1
                    argument = words[index]
                if flag == "a":
                    record(argument)
                    read_targeted = True
                break
            index += 1
        for target in words[index:]:
            if target.startswith(("<", ">")):
                break
            record(target)
            read_targeted = True
        if not read_targeted:
            record("REPLY")
    elif command in {"for", "select"}:
        if len(words) < 2:
            unknown = True
        elif words[1].startswith("(("):
            unknown = True
        else:
            record(words[1])
            if command == "select":
                record("REPLY")
    elif command == "getopts":
        if len(words) < 3:
            unknown = True
        else:
            record(words[2])
            record("OPTARG")
            record("OPTIND")
    elif command in {"mapfile", "readarray"}:
        index = 1
        target = "MAPFILE"
        options_with_arguments = {"d", "n", "O", "s", "u", "C", "c"}
        while index < len(words):
            option = words[index]
            if option.startswith(("<", ">")):
                break
            if option == "--":
                index += 1
                if index < len(words) and not words[index].startswith(("<", ">")):
                    target = words[index]
                break
            if not option.startswith("-") or option == "-":
                target = option
                break
            for flag_index, flag in enumerate(option[1:]):
                if flag not in options_with_arguments:
                    continue
                if flag == "C":
                    unknown = True
                if not option[flag_index + 2 :]:
                    index += 1
                break
            index += 1
        record(target)
    elif command == "wait":
        index = 1
        while index < len(words):
            option = words[index]
            if option == "--" or not option.startswith("-"):
                break
            flag_index = option.find("p", 1)
            if flag_index >= 0:
                target = option[flag_index + 1 :]
                if not target:
                    if index + 1 >= len(words):
                        unknown = True
                        break
                    target = words[index + 1]
                record(target)
                break
            index += 1
    elif command in {"cd", "popd", "pushd"}:
        record("PWD")
        record("OLDPWD")
        if command != "cd":
            record("DIRSTACK")
    elif command == "[[" and "=~" in words:
        record("BASH_REMATCH")
    elif command == "unset":
        unset_nameref = any(
            option.startswith("-") and "n" in option[1:]
            for option in words[1:]
            if option != "--"
        )
        for target in words[1:]:
            if target == "--" or target.startswith("-"):
                continue
            if unset_nameref:
                name = _literal_shell_variable(target)
                if name is None:
                    unknown = True
                    continue
                namerefs.pop(name, None)
                writes.add(name)
            else:
                record(target)
    elif command in {"declare", "export", "local", "readonly", "typeset"}:
        for operand in words[1:]:
            if "$" in operand and _assignment_write(operand) is None:
                unknown = True
    elif command in {".", "eval", "let", "source", "trap"} or command.startswith(
        "(("
    ):
        unknown = True
    return writes, unknown


def _function_scope_name(words: list[str]) -> str | None:
    if "{" not in words or not words:
        return None
    if words[0] == "function" and len(words) >= 2:
        name = words[1].removesuffix("()")
        return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else None
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\(\)", words[0]):
        return words[0][:-2]
    return None


def _opens_function_scope(words: list[str]) -> bool:
    return _function_scope_name(words) is not None


def _top_level_shell_commands(
    commands: list[tuple[list[str], str | None]],
) -> list[bool]:
    """Mark commands outside branches, loops, functions, and command groups.

    Only the constructs present in the official Dockerfiles/helper are modeled. An
    unbalanced construct makes every command unproven and therefore unusable as gate
    evidence.
    """
    depth = 0
    top_level: list[bool] = []
    for segment, _ in commands:
        words = _command_words(segment)
        first = words[0] if words else ""
        scope_index = 0
        while scope_index < len(words) and words[scope_index] == "!":
            scope_index += 1
        if scope_index < len(words) and words[scope_index] == "time":
            scope_index += 1
            while scope_index < len(words) and words[scope_index] in {"--", "-p"}:
                scope_index += 1
        scope_first = words[scope_index] if scope_index < len(words) else ""
        if scope_first.startswith("(") and scope_first != "(":
            return [False] * len(commands)
        if first in {"fi", "esac", "done", "}", ")"}:
            depth -= 1
            if depth < 0:
                return [False] * len(commands)
        top_level.append(depth == 0)
        if (
            scope_first
            in {"if", "case", "for", "while", "until", "select", "{", "("}
            or _opens_function_scope(words)
        ):
            depth += 1
    return top_level if depth == 0 else [False] * len(commands)


def _runtime_prepare_assignment(value: str) -> bool:
    if not value.startswith("$(") or not value.endswith(")"):
        return False
    nested = _shell_commands(value[2:-1])
    if len(nested) != 1 or nested[0][1] is not None:
        return False
    words = _command_words(nested[0][0])
    if len(words) < 3 or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(words[0]).name):
        return False
    script = Path(words[1]).name.replace("_", "-")
    if script != "prepare-runtime-requirements.py":
        return False
    for index, word in enumerate(words[2:], start=2):
        if word == "--runtime-version" and index + 1 < len(words):
            return words[index + 1] in {"$RUNTIME_VERSION", "${RUNTIME_VERSION}"}
        if word.startswith("--runtime-version="):
            return word.partition("=")[2] in {"$RUNTIME_VERSION", "${RUNTIME_VERSION}"}
    return words[-1] in {"$RUNTIME_VERSION", "${RUNTIME_VERSION}"}


def _pip_acquisition_arguments(words: list[str]) -> list[str] | None:
    if not words:
        return None
    executable = Path(words[0]).name
    if re.fullmatch(r"pip3?(?:\.\d+)?", executable) and len(words) >= 2:
        return words[2:] if words[1] in {"download", "install"} else None
    if (
        re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable)
        and len(words) >= 4
        and words[1:3] == ["-m", "pip"]
    ):
        return words[4:] if words[3] in {"download", "install"} else None
    return None


def _set_errexit(words: list[str], enabled: bool) -> bool:
    words = _command_words(words)
    if not words or words[0] != "set":
        return enabled
    index = 1
    while index < len(words):
        option = words[index]
        if option in {"-o", "+o"} and index + 1 < len(words):
            if words[index + 1] == "errexit":
                enabled = option == "-o"
            index += 2
            continue
        if re.fullmatch(r"-[A-Za-z]+", option) and "e" in option[1:]:
            enabled = True
        elif re.fullmatch(r"\+[A-Za-z]+", option) and "e" in option[1:]:
            enabled = False
        index += 1
    return enabled


def _errexit_enabled_before(
    commands: list[tuple[list[str], str | None]], stop: int
) -> bool:
    enabled = False
    top_level = _top_level_shell_commands(commands)
    for index, (words, operator) in enumerate(commands[:stop]):
        if not top_level[index]:
            continue
        previous = commands[index - 1][1] if index else None
        if previous not in {None, ";"} or operator in {"|", "|&", "&"}:
            continue
        enabled = _set_errexit(words, enabled)
    return enabled


def _nonzero_exit(words: list[str]) -> bool:
    words = _command_words(words)
    return bool(
        len(words) == 2
        and words[0] == "exit"
        and re.fullmatch(r"[0-9]+", words[1])
        and int(words[1]) % 256 != 0
    )


def _or_fallback_exits_nonzero(
    commands: list[tuple[list[str], str | None]], command_index: int
) -> bool:
    """Recognize the one fail-closed OR form used by the packaged helper.

    ``command || true`` and arbitrary fallback programs remain untrusted. The accepted
    form is a direct non-zero ``exit`` or a simple braced sequence whose last command is
    that exit, with no nested control edge that could mask it.
    """
    fallback_index = command_index + 1
    if fallback_index >= len(commands):
        return False
    words, operator = commands[fallback_index]
    direct = _command_words(words)
    if _nonzero_exit(direct):
        return operator not in {"||", "&&", "|", "|&", "&"}
    if not direct or direct[0] != "{":
        return False

    last_command: list[str] | None = direct[1:] or None
    for index in range(fallback_index, len(commands)):
        group_words, group_operator = commands[index]
        words = _command_words(group_words)
        if index == fallback_index:
            words = words[1:]
        closes_group = "}" in words
        if closes_group:
            words = words[: words.index("}")]
        if words:
            last_command = words
        if group_operator in {"||", "&&", "|", "|&", "&"}:
            return False
        if closes_group:
            return last_command is not None and _nonzero_exit(last_command)
    return False


def _command_failure_is_unmasked(
    commands: list[tuple[list[str], str | None]], command_index: int
) -> bool:
    previous = commands[command_index - 1][1] if command_index else None
    if previous not in {None, ";"}:
        return False
    operator = commands[command_index][1]
    if operator is None:
        return True
    if operator == "||":
        return _or_fallback_exits_nonzero(commands, command_index)
    if operator != ";":
        return False
    return _errexit_enabled_before(commands, command_index)


def _run_acquires_pinned_runtime(run: str) -> bool:
    commands = _shell_commands(run)
    top_level = _top_level_shell_commands(commands)
    if any(_opens_function_scope(_command_words(segment)) for segment, _ in commands):
        return False
    prepared: dict[str, int] = {}
    namerefs: dict[str, str | None] = {}
    runtime_project_is_runtime = False
    runtime_version_pristine = True
    for index, (segment, _) in enumerate(commands):
        writes, unknown_write = _stateful_shell_writes(segment, namerefs)
        if unknown_write:
            prepared.clear()
            runtime_project_is_runtime = False
            runtime_version_pristine = False
        for name in writes:
            if name == "RUNTIME_VERSION":
                runtime_version_pristine = False
            prepared.pop(name, None)
            if name == "runtime_project":
                runtime_project_is_runtime = False

        assignment_is_unmasked = bool(
            top_level[index] and _command_failure_is_unmasked(commands, index)
        )
        for name, value, persists, is_direct, establishes in _assignment_updates(
            segment
        ):
            if name == "RUNTIME_VERSION":
                runtime_version_pristine = False
            prepared.pop(name, None)
            if name == "runtime_project":
                runtime_project_is_runtime = bool(
                    persists
                    and is_direct
                    and establishes
                    and name not in namerefs
                    and assignment_is_unmasked
                    and top_level[index]
                    and value.replace("_", "-") == "molecules-workspace-runtime"
                )
            if (
                persists
                and is_direct
                and establishes
                and name not in namerefs
                and assignment_is_unmasked
                and top_level[index]
                and runtime_version_pristine
                and _runtime_prepare_assignment(value)
            ):
                prepared[name] = index

        words = _command_words(segment)
        if not top_level[index]:
            continue

        arguments = _pip_acquisition_arguments(words)
        if arguments is None or not _command_failure_is_unmasked(commands, index):
            continue
        if runtime_version_pristine and any(
            re.fullmatch(
                r"molecules[-_]workspace[-_]runtime==\$\{?RUNTIME_VERSION\}?", argument
            )
            for argument in arguments
        ):
            return True
        for name, assignment_index in prepared.items():
            if assignment_index < index and any(
                argument in {f"${name}", f"${{{name}}}"} for argument in arguments
            ):
                return runtime_version_pristine and runtime_project_is_runtime
    return False


def _run_directly_executes_prebake(run: str) -> bool:
    commands = _shell_commands(run)
    top_level = _top_level_shell_commands(commands)
    for index, (segment, _) in enumerate(commands):
        if not top_level[index] or not _command_failure_is_unmasked(commands, index):
            continue
        words = _command_words(segment)
        if not words:
            continue
        executable = Path(words[0]).name
        if executable == "prebake-mgmt-mcp.sh":
            return True
        if executable not in {"bash", "sh"}:
            continue
        script_index = 1
        while script_index < len(words):
            option = words[script_index]
            if option == "--":
                script_index += 1
                break
            if not option.startswith("-") or option == "-":
                break
            if not re.fullmatch(r"-[eux]+", option):
                script_index = len(words)
                break
            script_index += 1
        if (
            script_index < len(words)
            and words[script_index].endswith("/scripts/prebake-mgmt-mcp.sh")
        ):
            return True
    return False


def _helper_consumes_mcp_contract(helper: str) -> bool:
    expected = {
        "PKG": "$(_read MANAGEMENT_MCP_NPM_PACKAGE)",
        "VER": "$(_read MANAGEMENT_MCP_PINNED_VERSION)",
        "RANGE": "$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)",
    }
    protected = {*expected, "SPEC"}
    bindings: dict[str, str] = {}
    namerefs: dict[str, str | None] = {}
    exact_checked = False
    range_checked = False
    commands = _shell_commands(" ; ".join(_continued_lines(helper)))
    top_level = _top_level_shell_commands(commands)
    function_names = {
        name
        for segment, _ in commands
        if (name := _function_scope_name(_command_words(segment))) is not None
    }
    for index, (segment, _) in enumerate(commands):
        words = _command_words(segment)
        if (
            words
            and words[0] != "_prebake_self_check"
            and words[0] in function_names
            and _function_scope_name(words) is None
        ):
            return False
        writes, unknown_write = _stateful_shell_writes(segment, namerefs)
        if unknown_write:
            return False
        for name in writes & protected:
            bindings.pop(name, None)

        assignment_is_unmasked = bool(
            top_level[index] and _command_failure_is_unmasked(commands, index)
        )
        for name, value, persists, is_direct, establishes in _assignment_updates(
            segment
        ):
            if name not in protected:
                continue
            bindings.pop(name, None)
            if not (
                persists
                and is_direct
                and establishes
                and name not in namerefs
                and assignment_is_unmasked
            ):
                continue
            if name == "SPEC":
                if (
                    value in {"${PKG}@${VER}", "$PKG@$VER"}
                    and bindings.get("PKG") == expected["PKG"]
                    and bindings.get("VER") == expected["VER"]
                ):
                    bindings[name] = "canonical-exact-spec"
            else:
                bindings[name] = value

        if (
            not top_level[index]
            or len(words) < 2
            or words[0] != "_prebake_self_check"
            or not _command_failure_is_unmasked(commands, index)
        ):
            continue
        if words[1] == "${SPEC}":
            if bindings.get("SPEC") != "canonical-exact-spec":
                return False
            exact_checked = True
        elif words[1] == "${PKG}@${RANGE}":
            if (
                bindings.get("PKG") != expected["PKG"]
                or bindings.get("RANGE") != expected["RANGE"]
            ):
                return False
            range_checked = True
    return exact_checked and range_checked


def _template_runtime_pin(repo_root: Path) -> str:
    pin_path = repo_root / ".runtime-version"
    if not pin_path.is_file():
        raise MCPPinLockstepError("missing .runtime-version exact runtime pin")
    pin = pin_path.read_text(encoding="utf-8").strip()
    _exact_semver(pin, ".runtime-version")

    dockerfile = repo_root / "Dockerfile"
    if not dockerfile.is_file():
        raise MCPPinLockstepError("missing Dockerfile for mcp-server-bake capability")
    logical = _continued_lines(dockerfile.read_text(encoding="utf-8"))

    if not any(re.match(r"^ARG\s+RUNTIME_VERSION(?:=|\s|$)", line, re.I) for line in logical):
        raise MCPPinLockstepError("Dockerfile is missing ARG RUNTIME_VERSION")
    try:
        runtime_acquisition = any(
            line.upper().startswith("RUN ") and _run_acquires_pinned_runtime(line[4:])
            for line in logical
        )
        prebake_delegation = any(
            line.upper().startswith("RUN ") and _run_directly_executes_prebake(line[4:])
            for line in logical
        )
    except ValueError as exc:
        raise MCPPinLockstepError(f"Dockerfile has malformed shell syntax: {exc}") from exc
    if not runtime_acquisition:
        raise MCPPinLockstepError(
            "Dockerfile does not bind RUNTIME_VERSION to runtime wheel acquisition"
        )
    if not prebake_delegation:
        raise MCPPinLockstepError(
            "Dockerfile has no executable RUN delegation to prebake-mgmt-mcp.sh"
        )
    return pin


def _runtime_wheel_reference(index: bytes, runtime_version: str) -> tuple[str, str]:
    expected = f"molecules_workspace_runtime-{runtime_version}-py3-none-any.whl"
    parser = _RuntimeWheelLinks()
    try:
        parser.feed(index.decode("utf-8"))
    except UnicodeError as exc:
        raise MCPPinLockstepError("runtime package index is not UTF-8 HTML") from exc
    matches: list[tuple[str, str]] = []
    for href in parser.hrefs:
        absolute = urllib.parse.urljoin(MOLECULE_RUNTIME_INDEX_URL, href)
        parsed = urllib.parse.urlsplit(absolute)
        if urllib.parse.unquote(Path(parsed.path).name) != expected:
            continue
        digest = urllib.parse.parse_qs(parsed.fragment).get("sha256", [])
        if len(digest) != 1 or not re.fullmatch(r"[0-9a-f]{64}", digest[0]):
            raise MCPPinLockstepError(
                f"exact runtime wheel {expected} lacks a valid immutable sha256"
            )
        clean = urllib.parse.urlunsplit(parsed._replace(fragment=""))
        if not _same_origin(clean, MOLECULE_RUNTIME_INDEX_URL):
            raise MCPPinLockstepError(f"runtime wheel URL leaves trusted registry: {clean}")
        matches.append((clean, digest[0]))
    if len(matches) != 1:
        raise MCPPinLockstepError(
            f"expected exactly one immutable runtime wheel for {runtime_version}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _runtime_contract(wheel_bytes: bytes, runtime_version: str) -> dict[str, str]:
    source_path = "molecule_runtime/platform_agent_identity.py"
    helper_path = "molecule_runtime/scripts/prebake-mgmt-mcp.sh"
    try:
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as wheel:
            members = wheel.infolist()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise MCPPinLockstepError(
                    f"runtime wheel has too many members: {len(members)}"
                )
            total_size = 0
            for member in members:
                if member.flag_bits & 0x1:
                    raise MCPPinLockstepError(
                        f"runtime wheel contains encrypted member: {member.filename}"
                    )
                if member.file_size < 0 or member.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
                    raise MCPPinLockstepError(
                        f"runtime wheel member exceeds {_MAX_ARCHIVE_MEMBER_BYTES} "
                        f"uncompressed bytes: {member.filename}"
                    )
                total_size += member.file_size
                if total_size > _MAX_WHEEL_UNCOMPRESSED_BYTES:
                    raise MCPPinLockstepError(
                        "runtime wheel total uncompressed size exceeds "
                        f"{_MAX_WHEEL_UNCOMPRESSED_BYTES} bytes"
                    )
            member_names = [member.filename for member in members]
            names = set(member_names)
            if len(names) != len(member_names):
                raise MCPPinLockstepError("runtime wheel contains duplicate member names")
            if source_path not in names or helper_path not in names:
                raise MCPPinLockstepError(
                    "exact runtime wheel is missing platform_agent_identity.py or "
                    "prebake-mgmt-mcp.sh"
                )
            metadata_paths = [
                name
                for name in names
                if name.endswith(".dist-info/METADATA")
                and Path(name).name == "METADATA"
            ]
            if len(metadata_paths) != 1:
                raise MCPPinLockstepError(
                    f"exact runtime wheel must contain one METADATA file; found {len(metadata_paths)}"
                )
            metadata = BytesParser().parsebytes(wheel.read(metadata_paths[0]))
            if metadata.get("Name", "").lower().replace("_", "-") != "molecules-workspace-runtime":
                raise MCPPinLockstepError("runtime wheel METADATA has the wrong project name")
            if metadata.get("Version") != runtime_version:
                raise MCPPinLockstepError(
                    "runtime wheel METADATA version does not match .runtime-version"
                )
            source = wheel.read(source_path).decode("utf-8")
            helper = wheel.read(helper_path).decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeError, OSError) as exc:
        raise MCPPinLockstepError(f"runtime wheel is malformed: {exc}") from exc

    required = {
        "MANAGEMENT_MCP_NPM_PACKAGE",
        "MANAGEMENT_MCP_PINNED_VERSION",
        "MANAGEMENT_MCP_COMPATIBLE_RANGE",
        "MANAGEMENT_MCP_REGISTRY",
        "MANAGEMENT_MCP_REGISTRY_SCOPE",
    }
    values: dict[str, str] = {}
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise MCPPinLockstepError("runtime platform_agent_identity.py is invalid Python") from exc
    writes: dict[str, list[ast.AST]] = {name: [] for name in required}
    for node in ast.walk(module):
        if (
            isinstance(node, ast.Name)
            and node.id in required
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            writes[node.id].append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in required:
                writes[node.name].append(node)
        elif isinstance(node, ast.arg):
            if node.arg in required:
                writes[node.arg].append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    for name in required:
                        writes[name].append(node)
                    continue
                bound = alias.asname
                if bound is None:
                    bound = (
                        alias.name.partition(".")[0]
                        if isinstance(node, ast.Import)
                        else alias.name
                    )
                if bound in required:
                    writes[bound].append(node)
        elif isinstance(node, ast.ExceptHandler):
            if node.name in required:
                writes[node.name].append(node)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name in required:
                writes[node.name].append(node)
        elif isinstance(node, ast.MatchMapping):
            if node.rest in required:
                writes[node.rest].append(node)
    literal_targets: dict[str, ast.Name] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id in required
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            values[target.id] = node.value.value
            literal_targets[target.id] = target
    invalid = sorted(
        name
        for name in required
        if len(writes[name]) != 1
        or literal_targets.get(name) is not writes[name][0]
    )
    if invalid:
        raise MCPPinLockstepError(
            "runtime wheel requires exactly one top-level literal assignment for MCP "
            "contract constants: "
            + ", ".join(invalid)
        )
    try:
        helper_consumes_contract = _helper_consumes_mcp_contract(helper)
    except ValueError as exc:
        raise MCPPinLockstepError(f"runtime prebake helper is malformed: {exc}") from exc
    if not helper_consumes_contract:
        raise MCPPinLockstepError(
            "runtime prebake helper does not consume and offline-check the exact MCP pin"
        )
    if not _caret_contains(
        values["MANAGEMENT_MCP_COMPATIBLE_RANGE"],
        values["MANAGEMENT_MCP_PINNED_VERSION"],
    ):
        raise MCPPinLockstepError(
            f"runtime MCP pin {values['MANAGEMENT_MCP_PINNED_VERSION']} is outside "
            f"compatible range {values['MANAGEMENT_MCP_COMPATIBLE_RANGE']}"
        )
    package = values["MANAGEMENT_MCP_NPM_PACKAGE"]
    scope = values["MANAGEMENT_MCP_REGISTRY_SCOPE"]
    registry = values["MANAGEMENT_MCP_REGISTRY"]
    if not package.startswith(scope + "/"):
        raise MCPPinLockstepError("runtime MCP package and registry scope disagree")
    parsed_registry = urllib.parse.urlsplit(registry)
    if (
        _https_origin(registry) != _PACKAGE_ORIGIN
        or parsed_registry.path != "/api/packages/molecule-ai/npm/"
        or parsed_registry.query
        or parsed_registry.fragment
    ):
        raise MCPPinLockstepError(f"runtime wheel names an untrusted MCP registry: {registry}")
    return values


def _verify_mcp_tarball(
    tarball: bytes,
    *,
    package: str,
    version: str,
    integrity: str,
    shasum: str,
) -> None:
    if not integrity.startswith("sha512-"):
        raise MCPPinLockstepError("exact MCP artifact lacks sha512 integrity")
    try:
        expected_sha512 = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
    except ValueError as exc:
        raise MCPPinLockstepError("exact MCP artifact has malformed sha512 integrity") from exc
    if not hmac.compare_digest(hashlib.sha512(tarball).digest(), expected_sha512):
        raise MCPPinLockstepError("exact MCP tarball sha512 integrity mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", shasum):
        raise MCPPinLockstepError("exact MCP artifact has malformed sha1 shasum")
    # npm's legacy shasum is an additional metadata consistency check; the
    # security integrity boundary above is SHA-512.
    legacy_sha1 = hashlib.sha1(tarball, usedforsecurity=False).hexdigest()
    if not hmac.compare_digest(legacy_sha1, shasum):
        raise MCPPinLockstepError("exact MCP tarball sha1 shasum mismatch")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(tarball), mode="rb") as compressed:
            uncompressed = compressed.read(_MAX_TAR_UNCOMPRESSED_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise MCPPinLockstepError(f"exact MCP gzip payload is malformed: {exc}") from exc
    if len(uncompressed) > _MAX_TAR_UNCOMPRESSED_BYTES:
        raise MCPPinLockstepError(
            f"MCP gzip payload exceeds {_MAX_TAR_UNCOMPRESSED_BYTES} uncompressed bytes"
        )
    try:
        with tarfile.open(fileobj=io.BytesIO(uncompressed), mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise MCPPinLockstepError(
                    f"MCP tarball has too many members: {len(members)}"
                )
            total_size = 0
            for item in members:
                if item.size < 0 or item.size > _MAX_ARCHIVE_MEMBER_BYTES:
                    raise MCPPinLockstepError(
                        f"MCP tarball member exceeds {_MAX_ARCHIVE_MEMBER_BYTES} "
                        f"uncompressed bytes: {item.name}"
                    )
                total_size += item.size
                if total_size > _MAX_TAR_UNCOMPRESSED_BYTES:
                    raise MCPPinLockstepError(
                        "MCP tarball total uncompressed member size exceeds "
                        f"{_MAX_TAR_UNCOMPRESSED_BYTES} bytes"
                    )
            member = archive.getmember("package/package.json")
            if not member.isfile():
                raise MCPPinLockstepError("MCP tarball package.json is not a bounded file")
            stream = archive.extractfile(member)
            if stream is None:
                raise MCPPinLockstepError("MCP tarball package.json is unreadable")
            manifest = json.loads(stream.read().decode("utf-8"))
    except (tarfile.TarError, KeyError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MCPPinLockstepError(f"exact MCP tarball is malformed: {exc}") from exc
    if manifest.get("name") != package or manifest.get("version") != version:
        raise MCPPinLockstepError("MCP tarball package identity does not match its exact pin")
    binaries = manifest.get("bin")
    if not isinstance(binaries, (str, dict)) or not binaries:
        raise MCPPinLockstepError("MCP tarball has no executable bin entry")


def _verify_exact_mcp_artifact(values: dict[str, str], fetch_bytes) -> None:
    package = values["MANAGEMENT_MCP_NPM_PACKAGE"]
    version = values["MANAGEMENT_MCP_PINNED_VERSION"]
    registry = values["MANAGEMENT_MCP_REGISTRY"]
    packument_url = registry + urllib.parse.quote(package, safe="")
    try:
        packument = json.loads(fetch_bytes(packument_url).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MCPPinLockstepError("MCP registry packument is malformed JSON") from exc
    if not isinstance(packument, dict) or packument.get("name") != package:
        raise MCPPinLockstepError("MCP registry packument has the wrong package identity")
    versions = packument.get("versions")
    exact = versions.get(version) if isinstance(versions, dict) else None
    if not isinstance(exact, dict):
        raise MCPPinLockstepError(
            f"exact MCP package version {version} is missing from the registry"
        )
    if exact.get("name") != package or exact.get("version") != version:
        raise MCPPinLockstepError("exact MCP registry metadata has a mismatched identity")
    dist = exact.get("dist")
    if not isinstance(dist, dict):
        raise MCPPinLockstepError("exact MCP registry metadata lacks dist integrity")
    tarball_url = dist.get("tarball")
    integrity = dist.get("integrity")
    shasum = dist.get("shasum")
    if not all(isinstance(value, str) and value for value in (tarball_url, integrity, shasum)):
        raise MCPPinLockstepError("exact MCP registry metadata lacks immutable dist fields")
    if not _same_origin(tarball_url, registry):
        raise MCPPinLockstepError(f"MCP tarball URL leaves trusted registry: {tarball_url}")
    _verify_mcp_tarball(
        fetch_bytes(tarball_url),
        package=package,
        version=version,
        integrity=integrity,
        shasum=shasum,
    )


def _run_mcp_pin_lockstep(
    repo_root: Path,
    *,
    fetch_bytes=_fetch_bytes,
) -> tuple[bool, str]:
    try:
        runtime_version = _template_runtime_pin(repo_root)
        index = fetch_bytes(MOLECULE_RUNTIME_INDEX_URL)
        wheel_url, wheel_sha = _runtime_wheel_reference(index, runtime_version)
        wheel = fetch_bytes(wheel_url)
        if not hmac.compare_digest(hashlib.sha256(wheel).hexdigest(), wheel_sha):
            raise MCPPinLockstepError("exact runtime wheel sha256 mismatch")
        values = _runtime_contract(wheel, runtime_version)
        _verify_exact_mcp_artifact(values, fetch_bytes)
    except Exception as exc:  # runner boundary: every unexpected condition fails closed
        return False, str(exc) or exc.__class__.__name__
    package = values["MANAGEMENT_MCP_NPM_PACKAGE"]
    pinned = values["MANAGEMENT_MCP_PINNED_VERSION"]
    compatible = values["MANAGEMENT_MCP_COMPATIBLE_RANGE"]
    return True, (
        f"runtime {runtime_version} immutable wheel -> {package}@{pinned} immutable tarball; "
        f"exact pin satisfies launch range {compatible}; template delegates prebake"
    )


# Runner table: bundle -> callable(repo_root)->(ok, detail). Bundles absent here are
# 'planned' — reported but not executed in Phase 1 (execution wired in Phase 2). The
# node and MCP lockstep bundles are executed now because they are self-guarding,
# credential-free, bounded, and fail closed — see their definitions above.
EXECUTABLE_RUNNERS = {
    "secret-scan": _run_secret_scan,
    "node-install-lint-typecheck-build": _run_node_package,
    "mcp-pin-lockstep": _run_mcp_pin_lockstep,
}


def run_bundles(plan: dict, repo_root: Path) -> tuple[bool, list[str]]:
    """Execute the effective bundles that have a wired runner; report the rest as
    'planned'. Returns (aggregate_ok, per-bundle report lines)."""
    ok = True
    lines: list[str] = []
    for bundle in plan["bundles_effective"]:
        runner = EXECUTABLE_RUNNERS.get(bundle)
        if runner is None:
            lines.append(f"  planned  {bundle} (execution wired in Phase 2)")
            continue
        passed, detail = runner(repo_root)
        lines.append(f"  {'PASS' if passed else 'FAIL':7} {bundle} — {detail}")
        ok = ok and passed
    return ok, lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="meta-ci capability→bundle router (Phase 1, advisory)")
    ap.add_argument("--repo-root", default=".", help="path to the repo whose repo-meta.yaml to route")
    ap.add_argument("--plan-json", action="store_true", help="print the derived plan as JSON and exit 0")
    ap.add_argument("--plan-only", action="store_true", help="derive + print the plan; skip bundle runners")
    args = ap.parse_args(argv)

    print(SENTINEL)
    repo_root = Path(args.repo_root).resolve()
    manifest_path = repo_root / "repo-meta.yaml"
    if not manifest_path.exists():
        print(f"::error::no repo-meta.yaml at {manifest_path}", file=sys.stderr)
        return 2

    try:
        manifest = _load_yaml(manifest_path)
    except MetaCIError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    errors, warnings = validate_manifest(manifest)
    for w in warnings:
        print(f"::warning::repo-meta: {w}")
    if errors:
        for e in errors:
            print(f"::error::repo-meta INVALID: {e}")
        print("meta-ci: repo-meta.yaml is INVALID — cannot route CI bundles.")
        return 1

    plan = derive_bundles(manifest)

    if args.plan_json:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    print(f"meta-ci plan for layer={plan['layer']!r} capabilities={plan['capabilities']}")
    for n in plan["waiver_notices"]:
        print(f"  ::notice:: {n}")
    if plan["bundles_waived"]:
        print(f"  waived (live): {plan['bundles_waived']}")
    print(f"  effective bundles: {plan['bundles_effective']}")

    if args.plan_only:
        return 0

    ok, lines = run_bundles(plan, repo_root)
    print("meta-ci bundle results:")
    for ln in lines:
        print(ln)
    if not ok:
        print("::error::meta-ci: one or more EXECUTED bundles failed.")
        return 1
    print("meta-ci: PASS (manifest valid; all executed bundles green).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
