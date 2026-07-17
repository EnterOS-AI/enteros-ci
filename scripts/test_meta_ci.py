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
