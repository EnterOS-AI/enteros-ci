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
    (today: ``secret-scan``), and REPORTS the rest as ``planned`` (execution wired
    in Phase 2 — this file deliberately does not fork heavy docker-build / t4 /
    codegen bundles yet).

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
So the reusable/inline workflow runs THIS one script in a single job and posts a single
``meta-ci / required`` status — the same single-context pattern proven by
``_reusable-minimal-validate.yml``. A ``--sentinel`` line is printed so a hollow no-op
invocation (internal#1000) cannot be counted green.

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
import json
import re
import subprocess
import sys
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


# Runner table: bundle -> callable(repo_root)->(ok, detail). Bundles absent here are
# 'planned' — reported but not executed in Phase 1 (execution wired in Phase 2).
EXECUTABLE_RUNNERS = {
    "secret-scan": _run_secret_scan,
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
