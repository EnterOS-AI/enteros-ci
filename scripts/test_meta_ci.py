"""Tests for meta-ci.py — the capability→bundle router (Phase 1, advisory).

Pins the derivation contract: UNION of layer + capability bundles, dedupe, universal
secret-scan baseline, live-waiver suppression + expiry re-attach, unknown-capability
WARN (never error), strict manifest validation, and the aggregate exit codes. Each
positive assertion is paired with a NEGATIVE control so a vacuous test can't pass
(cf. feedback_negative_control_every_test).

meta-ci.py is hyphenated and has a __main__ guard, so its pure functions are loaded via
importlib and its CLI is exercised as a subprocess (the exact entrypoint CI invokes).
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent
META_CI_PATH = _SCRIPTS / "meta-ci.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("meta_ci", META_CI_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


meta = _load_module()


# --- map/vocab sync with the schema + SDK validator ------------------------
def test_known_capabilities_match_vendored_schema():
    schema = json.loads((_SCRIPTS.parent / "schemas" / "repo-meta.schema.json").read_text())
    schema_known = set(schema["$defs"]["knownCapability"]["enum"])
    assert meta.KNOWN_CAPABILITIES == schema_known
    schema_layers = set(schema["$defs"]["layer"]["enum"])
    assert meta.LAYERS == schema_layers
    # node-package is a KNOWN capability (added RFC #57 Phase 2) that attaches a bundle.
    assert "node-package" in meta.KNOWN_CAPABILITIES
    assert "node-package" in schema_known
    # NEGATIVE control: a bogus capability is NOT in the known set.
    assert "totally-made-up" not in meta.KNOWN_CAPABILITIES


def test_capability_pattern_matches_schema():
    schema = json.loads((_SCRIPTS.parent / "schemas" / "repo-meta.schema.json").read_text())
    assert meta.CAPABILITY_RE.pattern == schema["$defs"]["capability"]["pattern"]


# --- derivation: union + dedupe --------------------------------------------
def test_service_derivation():
    m = {"schema_version": 1, "layer": "service", "capabilities": ["go-service", "docker-image"]}
    plan = meta.derive_bundles(m)
    assert set(plan["bundles_effective"]) == {
        "go-build-vet-lint-test", "docker-build-smoke", "secret-scan",
    }


def test_runtime_template_union_dedupes_adapter():
    # layer runtime-template already brings adapter-conformance; the `adapter` capability
    # brings it too — the UNION must dedupe to a single entry.
    m = {"schema_version": 1, "layer": "runtime-template",
         "capabilities": ["adapter", "mcp-server-bake", "docker-image"]}
    plan = meta.derive_bundles(m)
    eff = plan["bundles_effective"]
    assert eff.count("adapter-conformance") == 1  # deduped, not doubled
    assert set(eff) == {
        "adapter-conformance", "docker-build-smoke", "t4-assert",
        "mcp-pin-lockstep", "secret-scan",
    }
    # NEGATIVE control: a plugin-only bundle must NOT leak into a runtime-template plan.
    assert "plugin-manifest-validate" not in eff


def test_plugin_derivation():
    m = {"schema_version": 1, "layer": "plugin", "capabilities": ["skills", "settings-fragment"]}
    plan = meta.derive_bundles(m)
    assert set(plan["bundles_effective"]) == {
        "plugin-manifest-validate", "skill-lint", "settings-fragment-validate", "secret-scan",
    }


def test_node_package_derivation():
    m = {"schema_version": 1, "layer": "service", "capabilities": ["node-package"]}
    plan = meta.derive_bundles(m)
    assert set(plan["bundles_effective"]) == {
        "node-install-lint-typecheck-build", "go-build-vet-lint-test", "secret-scan",
    }  # layer:service brings the go baseline; the cap adds the node bundle.


def test_node_package_cap_plans_node_bundle_not_go():
    # NEGATIVE control for the whole point of this change: a repo that declares
    # node-package as its ONLY capability under a neutral layer must plan the NODE
    # bundle, NOT the go one it used to mis-fit onto. `contract` layer's baseline
    # is contracts-codegen-drift + secret-scan (no go), isolating the capability.
    m = {"schema_version": 1, "layer": "contract", "capabilities": ["node-package"]}
    plan = meta.derive_bundles(m)
    assert "node-install-lint-typecheck-build" in plan["bundles_effective"]
    assert "go-build-vet-lint-test" not in plan["bundles_effective"]
    assert "node-package" not in plan["unknown_capabilities"]  # it is KNOWN now


def test_universal_secret_scan_even_with_no_capabilities():
    m = {"schema_version": 1, "layer": "plugin", "capabilities": []}
    plan = meta.derive_bundles(m)
    assert "secret-scan" in plan["bundles_effective"]


# --- waivers ----------------------------------------------------------------
def test_live_waiver_suppresses_bundle():
    future = (_dt.date.today() + _dt.timedelta(days=30)).isoformat()
    m = {"schema_version": 1, "layer": "runtime-template", "capabilities": ["mcp-server-bake"],
         "waivers": [{"bundle": "mcp-pin-lockstep", "until": future, "reason": "blocked on molecule-core#1234"}]}
    plan = meta.derive_bundles(m)
    assert "mcp-pin-lockstep" not in plan["bundles_effective"]
    assert "mcp-pin-lockstep" in plan["bundles_waived"]


def test_expired_waiver_reattaches_bundle():
    past = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    m = {"schema_version": 1, "layer": "runtime-template", "capabilities": ["mcp-server-bake"],
         "waivers": [{"bundle": "mcp-pin-lockstep", "until": past, "reason": "expired molecule-core#1234"}]}
    plan = meta.derive_bundles(m)
    # NEGATIVE control on the waiver mechanism: an EXPIRED waiver must NOT suppress.
    assert "mcp-pin-lockstep" in plan["bundles_effective"]
    assert any("EXPIRED" in n for n in plan["waiver_notices"])


# --- validation: strict errors + unknown-cap warns -------------------------
def test_unknown_capability_warns_not_errors():
    m = {"schema_version": 1, "layer": "plugin", "capabilities": ["x-fuzz", "made-up-thing"]}
    errors, warnings = meta.validate_manifest(m)
    assert errors == []                      # well-formed unknowns are NOT errors
    assert len(warnings) == 2
    plan = meta.derive_bundles(m)
    assert set(plan["unknown_capabilities"]) == {"x-fuzz", "made-up-thing"}
    # NEGATIVE control: an unknown capability attaches NO bundle beyond the baseline.
    assert set(plan["bundles_effective"]) == {"plugin-manifest-validate", "secret-scan"}


def test_typo_capability_is_a_hard_error():
    m = {"schema_version": 1, "layer": "plugin", "capabilities": ["go_service"]}  # underscore
    errors, _ = meta.validate_manifest(m)
    assert any("kebab-case" in e for e in errors)


def test_bad_layer_is_error():
    m = {"schema_version": 1, "layer": "not-a-layer", "capabilities": []}
    errors, _ = meta.validate_manifest(m)
    assert any("layer=" in e for e in errors)


def test_missing_schema_version_is_error():
    m = {"layer": "plugin"}
    errors, _ = meta.validate_manifest(m)
    assert any("schema_version" in e for e in errors)


def test_additional_property_rejected_by_schema():
    # strict additionalProperties:false — a stray top-level key must red.
    m = {"schema_version": 1, "layer": "plugin", "capabilities": [], "bogus": 1}
    errors, _ = meta.validate_manifest(m)
    assert any("bogus" in e or "additional" in e.lower() for e in errors)


def test_valid_manifest_has_no_errors():
    m = {"schema_version": 1, "layer": "service", "capabilities": ["go-service"]}
    errors, warnings = meta.validate_manifest(m)
    assert errors == [] and warnings == []


# --- node-package bundle runner --------------------------------------------
def _write_pkg(tmp_path, scripts):
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "scripts": scripts}))


def test_node_steps_none_without_package_json(tmp_path):
    assert meta.node_bundle_steps(tmp_path) is None


def test_node_steps_pnpm_precedence_over_npm(tmp_path):
    # A repo with BOTH pnpm-lock.yaml and package-lock.json (a real case:
    # molecule-app) must resolve deterministically to pnpm (precedence pnpm > npm).
    _write_pkg(tmp_path, {"build": "x", "lint": "x", "typecheck": "x"})
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "package-lock.json").write_text("{}")
    steps = meta.node_bundle_steps(tmp_path)
    assert steps[0] == ("install", ["pnpm", "install", "--frozen-lockfile"])
    # declared scripts run in canonical order lint -> typecheck -> build, via pnpm.
    assert [lbl for lbl, _ in steps] == ["install", "lint", "typecheck", "build"]
    assert steps[3] == ("build", ["pnpm", "run", "build"])
    # NEGATIVE control: npm was NOT chosen despite package-lock.json being present.
    assert steps[0][1][0] != "npm"


def test_node_steps_npm_ci_with_package_lock(tmp_path):
    _write_pkg(tmp_path, {"build": "x"})
    (tmp_path / "package-lock.json").write_text("{}")
    steps = meta.node_bundle_steps(tmp_path)
    assert steps[0] == ("install", ["npm", "ci"])


def test_node_steps_yarn_frozen(tmp_path):
    _write_pkg(tmp_path, {"lint": "x"})
    (tmp_path / "yarn.lock").write_text("")
    steps = meta.node_bundle_steps(tmp_path)
    assert steps[0] == ("install", ["yarn", "install", "--frozen-lockfile"])


def test_node_steps_no_lockfile_falls_back_to_plain_install(tmp_path):
    # A package.json with no lockfile can't be frozen-installed (npm ci needs one):
    # degrade to a non-frozen `npm install`, don't fail (real case: tenant-proxy).
    _write_pkg(tmp_path, {"test": "x"})
    steps = meta.node_bundle_steps(tmp_path)
    assert steps == [("install", ["npm", "install", "--no-audit", "--no-fund"])]


def test_node_steps_skip_absent_scripts(tmp_path):
    # Only DECLARED scripts run; a repo with just `build` (real case: mcp-server)
    # runs build and skips lint/typecheck — never invents a script it lacks.
    _write_pkg(tmp_path, {"build": "x", "start": "x", "test": "x"})
    (tmp_path / "package-lock.json").write_text("{}")
    labels = [lbl for lbl, _ in meta.node_bundle_steps(tmp_path)]
    assert labels == ["install", "build"]
    # NEGATIVE control: lint/typecheck are NOT run (not declared).
    assert "lint" not in labels and "typecheck" not in labels


def test_run_node_package_noop_without_package_json(tmp_path):
    ok, detail = meta._run_node_package(tmp_path)
    assert ok and "no package.json" in detail  # self-guards to a clean PASS


def test_run_node_package_fails_closed_when_manager_absent(tmp_path, monkeypatch):
    # DEFECT (code-review CONFIRMED): a repo that DECLARES node-package but whose
    # package manager is missing from the runner used to return (True, "skipped …")
    # — a silent FALSE-GREEN. The lint/typecheck/build never ran, yet the leg went
    # green in the "every executed runner green" aggregate. The old buggy code was:
    #     if shutil.which(manager) is None:
    #         return True, f"skipped ({manager} not installed on runner)"
    # It must now FAIL CLOSED (runner mis-provisioned; fail, don't skip).
    _write_pkg(tmp_path, {"build": "x"})
    (tmp_path / "pnpm-lock.yaml").write_text("")
    monkeypatch.setattr(meta.shutil, "which", lambda _cmd: None)
    ok, detail = meta._run_node_package(tmp_path)
    assert not ok  # NEGATIVE control: old code returned ok=True here (false-green)
    # actionable message must name the missing manager AND the repo
    assert "pnpm" in detail
    assert tmp_path.name in detail


def test_absent_manager_reds_the_aggregate(tmp_path, monkeypatch):
    # End-to-end at the aggregate seam: run_bundles ANDs each executed leg. A repo
    # whose declared node manager is absent must drive aggregate_ok False — proving
    # the false-green can no longer contribute a passing leg.
    _write_pkg(tmp_path, {"lint": "x"})
    (tmp_path / "yarn.lock").write_text("")
    monkeypatch.setattr(meta.shutil, "which", lambda _cmd: None)
    plan = {"bundles_effective": ["node-install-lint-typecheck-build"]}
    aggregate_ok, lines = meta.run_bundles(plan, tmp_path)
    assert aggregate_ok is False
    assert any("FAIL" in ln for ln in lines)


class _FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


def test_node_steps_run_with_a_bounded_timeout(tmp_path, monkeypatch):
    # DEFECT (code-review CONFIRMED): the node lint/typecheck/build subprocess ran
    # with NO timeout= — a watch/hanging build blocks meta-ci indefinitely. Every
    # step must now be invoked with a bounded timeout= kwarg.
    _write_pkg(tmp_path, {"build": "x"})
    (tmp_path / "package-lock.json").write_text("{}")
    monkeypatch.setattr(meta.shutil, "which", lambda _cmd: "/usr/bin/" + _cmd)
    seen_timeouts: list = []

    def _fake_run(argv, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        return _FakeProc()

    monkeypatch.setattr(meta.subprocess, "run", _fake_run)
    ok, _ = meta._run_node_package(tmp_path)
    assert ok
    assert seen_timeouts, "no subprocess step was invoked"
    # NEGATIVE control: every step carried a positive, bounded timeout (not None).
    assert all(isinstance(t, (int, float)) and t > 0 for t in seen_timeouts)


def test_node_step_timeout_fails_not_hangs(tmp_path, monkeypatch):
    # A hanging build must surface as a clear FAILURE, never block the job.
    _write_pkg(tmp_path, {"build": "x"})
    (tmp_path / "package-lock.json").write_text("{}")
    monkeypatch.setattr(meta.shutil, "which", lambda _cmd: "/usr/bin/" + _cmd)

    def _timeout_run(argv, **kwargs):
        raise meta.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(meta.subprocess, "run", _timeout_run)
    ok, detail = meta._run_node_package(tmp_path)
    assert not ok  # NEGATIVE control: a hang used to block forever, never returning
    assert "timed out" in detail.lower()


# --- CLI end-to-end (the exact entrypoint CI runs) -------------------------
def _run_cli(repo_root: Path, *extra):
    return subprocess.run(
        [sys.executable, str(META_CI_PATH), "--repo-root", str(repo_root), *extra],
        capture_output=True, text=True,
    )


def test_cli_valid_repo_meta_passes(tmp_path):
    (tmp_path / "repo-meta.yaml").write_text(
        "schema_version: 1\nlayer: plugin\ncapabilities: [skills]\n"
    )
    (tmp_path / "README.md").write_text("ok")  # secret-scan needs a real dir; README is harmless
    proc = _run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert meta.SENTINEL in proc.stdout            # sentinel proves the script executed
    assert "effective bundles" in proc.stdout


def test_cli_invalid_repo_meta_fails(tmp_path):
    (tmp_path / "repo-meta.yaml").write_text("schema_version: 1\nlayer: bogus-layer\n")
    proc = _run_cli(tmp_path)
    assert proc.returncode == 1
    assert "INVALID" in proc.stdout


def test_cli_missing_repo_meta_is_env_error(tmp_path):
    proc = _run_cli(tmp_path)
    assert proc.returncode == 2
    assert "no repo-meta.yaml" in proc.stderr


def test_cli_plan_json(tmp_path):
    (tmp_path / "repo-meta.yaml").write_text(
        "schema_version: 1\nlayer: runtime-template\ncapabilities: [adapter, docker-image]\n"
    )
    proc = _run_cli(tmp_path, "--plan-json")
    assert proc.returncode == 0
    payload = proc.stdout[proc.stdout.index("{"):]
    plan = json.loads(payload)
    assert "adapter-conformance" in plan["bundles_effective"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
