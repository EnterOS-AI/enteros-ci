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
import datetime as _dt
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent


def _load_mcp_pin_lockstep():
    path = _SCRIPT_DIR / "mcp_pin_lockstep.py"
    spec = importlib.util.spec_from_file_location("mcp_pin_lockstep", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load static MCP artifact checker: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mcp_pin_lockstep = _load_mcp_pin_lockstep()

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
# mcp-server-bake artifact runner.
#
# The standalone checker is deliberately data-only: it reads .runtime-version,
# trusted registry metadata, and bounded archive members. It never executes the
# consumer checkout, Dockerfile, wheel module, or packaged helper. Runtime release
# tests own helper semantics and template Tier-4 owns the final built image.
# ---------------------------------------------------------------------------
def _run_mcp_pin_lockstep(repo_root: Path) -> tuple[bool, str]:
    return mcp_pin_lockstep.run(repo_root)


# Runner table: bundle -> callable(repo_root)->(ok, detail). Bundles absent here are
# 'planned' — reported but not executed in Phase 1 (execution wired in Phase 2). The
# node and MCP artifact bundles are executed now because they are bounded and
# fail closed. The MCP checker is isolated in scripts/mcp_pin_lockstep.py.
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
