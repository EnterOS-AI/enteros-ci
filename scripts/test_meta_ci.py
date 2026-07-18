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
import base64
import hashlib
import http.client
import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
import urllib.error
import zipfile
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
_OFFICIAL_CONSUMER_RECORDS = json.loads(
    (_SCRIPTS / "fixtures" / "meta-ci" / "official-consumers.json").read_text()
)
_OFFICIAL_CONSUMERS = {
    consumer["name"]: consumer
    for consumer in _OFFICIAL_CONSUMER_RECORDS
}


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


def test_mcp_server_bake_selects_an_executable_lockstep_bundle():
    manifest = {
        "schema_version": 1,
        "layer": "runtime-template",
        "capabilities": ["mcp-server-bake"],
    }

    plan = meta.derive_bundles(manifest)

    assert "mcp-pin-lockstep" in plan["bundles_effective"]
    assert meta.EXECUTABLE_RUNNERS["mcp-pin-lockstep"] is meta._run_mcp_pin_lockstep


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


# --- mcp-pin-lockstep bundle runner ----------------------------------------
def _runtime_wheel(
    version="0.4.25",
    pinned="1.8.3",
    compatible="^1.8.0",
    helper=None,
    source_extra=None,
):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as wheel:
        source_lines = [
            'MANAGEMENT_MCP_NPM_PACKAGE = "@molecule-ai/mcp-server"',
            f'MANAGEMENT_MCP_PINNED_VERSION = "{pinned}"',
            f'MANAGEMENT_MCP_COMPATIBLE_RANGE = "{compatible}"',
            'MANAGEMENT_MCP_REGISTRY = "https://git.moleculesai.app/api/packages/molecule-ai/npm/"',
            'MANAGEMENT_MCP_REGISTRY_SCOPE = "@molecule-ai"',
        ]
        if source_extra:
            source_lines.append(source_extra)
        wheel.writestr(
            "molecule_runtime/platform_agent_identity.py",
            "\n".join(source_lines),
        )
        wheel.writestr(
            "molecule_runtime/scripts/prebake-mgmt-mcp.sh",
            helper
            or "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -eu",
                    "PKG=\"$(_read MANAGEMENT_MCP_NPM_PACKAGE)\"",
                    "VER=\"$(_read MANAGEMENT_MCP_PINNED_VERSION)\"",
                    "RANGE=\"$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)\"",
                    'SPEC="${PKG}@${VER}"',
                    '_prebake_self_check "${SPEC}"',
                    '_prebake_self_check "${PKG}@${RANGE}"',
                ]
            ),
        )
        wheel.writestr(
            f"molecules_workspace_runtime-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: molecules-workspace-runtime\nVersion: {version}\n",
        )
    return stream.getvalue()


def _mcp_tarball(version="1.8.3"):
    stream = io.BytesIO()
    payload = json.dumps(
        {
            "name": "@molecule-ai/mcp-server",
            "version": version,
            "bin": {"molecule-mcp": "./dist/index.js"},
        }
    ).encode()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def _mcp_lockstep_fixture(tmp_path, *, runtime_version="0.4.25", pinned="1.8.3"):
    (tmp_path / ".runtime-version").write_text(runtime_version + "\n")
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.11-slim\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN set -e; runtime_project=\"molecules-workspace-runtime\"; \\\n"
        "    runtime_requirement=\"$(python3 /tmp/prepare-runtime-requirements.py "
        "--runtime-version \"${RUNTIME_VERSION}\")\"; \\\n"
        "    pip download --dest /tmp/molecule-runtime \"${runtime_requirement}\"\n"
        "RUN bash \"$(python3 -c 'import molecule_runtime')/scripts/prebake-mgmt-mcp.sh\"\n"
    )

    wheel = _runtime_wheel(version=runtime_version, pinned=pinned)
    wheel_sha = hashlib.sha256(wheel).hexdigest()
    wheel_name = f"molecules_workspace_runtime-{runtime_version}-py3-none-any.whl"
    wheel_url = (
        "https://git.moleculesai.app/api/packages/molecule-ai/pypi/files/"
        f"molecules-workspace-runtime/{runtime_version}/{wheel_name}"
    )
    index_url = meta.MOLECULE_RUNTIME_INDEX_URL
    index = f'<a href="{wheel_url}#sha256={wheel_sha}">{wheel_name}</a>'.encode()

    tarball = _mcp_tarball(pinned)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    tarball_url = (
        "https://git.moleculesai.app/api/packages/molecule-ai/npm/"
        f"%40molecule-ai%2Fmcp-server/-/{pinned}/mcp-server-{pinned}.tgz"
    )
    packument_url = (
        "https://git.moleculesai.app/api/packages/molecule-ai/npm/"
        "%40molecule-ai%2Fmcp-server"
    )
    packument = json.dumps(
        {
            "name": "@molecule-ai/mcp-server",
            "versions": {
                pinned: {
                    "name": "@molecule-ai/mcp-server",
                    "version": pinned,
                    "dist": {
                        "integrity": integrity,
                        "shasum": hashlib.sha1(tarball).hexdigest(),
                        "tarball": tarball_url,
                    },
                }
            },
        }
    ).encode()
    responses = {
        index_url: index,
        wheel_url: wheel,
        packument_url: packument,
        tarball_url: tarball,
    }

    def fetch(url):
        if url not in responses:
            raise AssertionError(f"unexpected URL: {url}")
        return responses[url]

    return responses, fetch


def test_mcp_pin_lockstep_verifies_exact_immutable_runtime_and_mcp_artifacts(tmp_path):
    _, fetch = _mcp_lockstep_fixture(tmp_path)

    ok, detail = meta._run_mcp_pin_lockstep(tmp_path, fetch_bytes=fetch)

    assert ok, detail
    assert "runtime 0.4.25" in detail
    assert "@molecule-ai/mcp-server@1.8.3" in detail


def test_dockerfile_rejects_nonexecuting_shell_for_lockstep_proof(tmp_path):
    _mcp_lockstep_fixture(tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "FROM python:3.11-slim\n",
            'FROM python:3.11-slim\nSHELL ["/bin/true"]\n',
        )
    )

    with pytest.raises(meta.MCPPinLockstepError, match="SHELL"):
        meta._template_runtime_pin(tmp_path)


def test_dockerfile_rejects_lockstep_proof_absent_from_final_stage(tmp_path):
    _mcp_lockstep_fixture(tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "FROM python:3.11-slim\n", "FROM python:3.11-slim AS proof\n"
        )
        + "FROM scratch AS final\n"
    )

    with pytest.raises(meta.MCPPinLockstepError, match="final Dockerfile stage"):
        meta._template_runtime_pin(tmp_path)


def test_dockerfile_rejects_heredoc_instruction_spoofing(tmp_path):
    _mcp_lockstep_fixture(tmp_path)
    (tmp_path / "Dockerfile").write_text(
        "# syntax=docker/dockerfile:1\n"
        "FROM python:3.11-slim\n"
        'SHELL ["/bin/true"]\n'
        "RUN <<'PROOF'\n"
        "FROM scratch AS fake\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN set -e; runtime_project=molecules-workspace-runtime; "
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version ${RUNTIME_VERSION})"; pip download "$runtime_requirement"\n'
        "RUN bash /opt/molecule/scripts/prebake-mgmt-mcp.sh\n"
        "PROOF\n"
    )

    with pytest.raises(meta.MCPPinLockstepError, match="heredoc"):
        meta._template_runtime_pin(tmp_path)


def test_dockerfile_rejects_nondefault_escape_instruction_spoofing(tmp_path):
    _mcp_lockstep_fixture(tmp_path)
    (tmp_path / "Dockerfile").write_text(
        "# escape=`\n"
        "FROM python:3.11-slim\n"
        'SHELL ["/bin/true"]\n'
        "RUN echo ignored `\n"
        "FROM scratch AS fake\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN set -e; runtime_project=molecules-workspace-runtime; "
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version ${RUNTIME_VERSION})"; pip download "$runtime_requirement"\n'
        "RUN bash /opt/molecule/scripts/prebake-mgmt-mcp.sh\n"
    )

    with pytest.raises(meta.MCPPinLockstepError, match="escape"):
        meta._template_runtime_pin(tmp_path)


def test_dockerfile_rejects_runtime_env_override(tmp_path):
    _mcp_lockstep_fixture(tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "ARG RUNTIME_VERSION=\n",
            "ARG RUNTIME_VERSION=\nENV RUNTIME_VERSION=9.9.9\n",
        )
    )

    with pytest.raises(meta.MCPPinLockstepError, match="ENV RUNTIME_VERSION"):
        meta._template_runtime_pin(tmp_path)


def test_dockerfile_requires_runtime_arg_before_acquisition(tmp_path):
    _mcp_lockstep_fixture(tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    contents = dockerfile.read_text().replace("ARG RUNTIME_VERSION=\n", "")
    dockerfile.write_text(
        contents.replace(
            "RUN bash ",
            "ARG RUNTIME_VERSION=\nRUN bash ",
        )
    )

    with pytest.raises(meta.MCPPinLockstepError, match="precede"):
        meta._template_runtime_pin(tmp_path)


def test_dockerfile_rejects_runtime_arg_redefinition(tmp_path):
    _mcp_lockstep_fixture(tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "RUN bash ",
            "ARG RUNTIME_VERSION=9.9.9\nRUN bash ",
        )
    )

    with pytest.raises(meta.MCPPinLockstepError, match="exactly one"):
        meta._template_runtime_pin(tmp_path)


@pytest.mark.parametrize(
    ("filename", "contents", "message"),
    [
        (".runtime-version", None, ".runtime-version"),
        (".runtime-version", "not-a-version\n", "exact stable semver"),
        (
            "Dockerfile",
            "FROM scratch\n"
            "ARG RUNTIME_VERSION=\n"
            "RUN set -e; runtime_project=\"molecules-workspace-runtime\"; \\\n"
            "    runtime_requirement=\"$(python3 /tmp/prepare-runtime-requirements.py "
            "--runtime-version \"${RUNTIME_VERSION}\")\"; \\\n"
            "    pip download --dest /tmp/molecule-runtime \"${runtime_requirement}\"\n",
            "prebake-mgmt-mcp.sh",
        ),
    ],
)
def test_mcp_pin_lockstep_fails_closed_on_missing_or_malformed_template_metadata(
    tmp_path, filename, contents, message
):
    _, fetch = _mcp_lockstep_fixture(tmp_path)
    path = tmp_path / filename
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents)

    ok, detail = meta._run_mcp_pin_lockstep(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert message in detail


def test_mcp_pin_lockstep_fails_closed_on_runtime_wheel_hash_mismatch(tmp_path):
    responses, fetch = _mcp_lockstep_fixture(tmp_path)
    wheel_url = next(url for url in responses if url.endswith(".whl"))
    responses[wheel_url] += b"tampered"

    ok, detail = meta._run_mcp_pin_lockstep(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "sha256" in detail.lower()


def test_mcp_pin_lockstep_fails_closed_when_exact_mcp_package_is_missing(tmp_path):
    responses, fetch = _mcp_lockstep_fixture(tmp_path)
    packument_url = next(
        url for url in responses if url.endswith("%40molecule-ai%2Fmcp-server")
    )
    responses[packument_url] = json.dumps(
        {"name": "@molecule-ai/mcp-server", "versions": {}}
    ).encode()

    ok, detail = meta._run_mcp_pin_lockstep(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "exact MCP package version 1.8.3" in detail


def test_mcp_pin_lockstep_fails_closed_when_registry_is_unavailable(tmp_path):
    _mcp_lockstep_fixture(tmp_path)

    def unavailable(_url):
        raise OSError("registry unavailable")

    ok, detail = meta._run_mcp_pin_lockstep(tmp_path, fetch_bytes=unavailable)

    assert not ok
    assert "registry unavailable" in detail


class _FakeHTTPResponse:
    def __init__(self, url, payload=b"ok"):
        self._url = url
        self._payload = payload
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self._url

    def read(self, limit):
        return self._payload[:limit]


def test_package_fetch_retries_only_transient_http_failures(monkeypatch):
    url = meta.MOLECULE_RUNTIME_INDEX_URL
    outcomes = [
        urllib.error.HTTPError(url, 429, "rate limited", {}, None),
        urllib.error.HTTPError(url, 503, "unavailable", {}, None),
        _FakeHTTPResponse(url, b"eventual success"),
    ]
    requests = []
    sleeps = []

    def urlopen(request, *, timeout):
        requests.append((request, timeout))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(meta, "_open_package_url", urlopen)
    monkeypatch.setattr(meta.time, "sleep", sleeps.append)

    assert meta._fetch_bytes(url) == b"eventual success"
    assert len(requests) == 3
    assert all(timeout == meta._HTTP_ATTEMPT_TIMEOUT_SECONDS for _, timeout in requests)
    assert all(request.get_header("User-agent") == "curl/8.4.0" for request, _ in requests)
    assert sleeps == [meta._HTTP_RETRY_DELAY_SECONDS, meta._HTTP_RETRY_DELAY_SECONDS * 2]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_package_fetch_does_not_retry_auth_or_other_client_errors(monkeypatch, status):
    url = meta.MOLECULE_RUNTIME_INDEX_URL
    calls = 0

    def urlopen(_request, *, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(url, status, "client error", {}, None)

    monkeypatch.setattr(meta, "_open_package_url", urlopen)

    with pytest.raises(meta.MCPPinLockstepError, match=f"HTTP {status}"):
        meta._fetch_bytes(url)
    assert calls == 1


def test_package_fetch_fails_closed_after_bounded_transport_retries(monkeypatch):
    url = meta.MOLECULE_RUNTIME_INDEX_URL
    calls = 0

    def urlopen(_request, *, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(meta, "_open_package_url", urlopen)
    monkeypatch.setattr(meta.time, "sleep", lambda _delay: None)

    with pytest.raises(meta.MCPPinLockstepError, match="after 3 attempts"):
        meta._fetch_bytes(url)
    assert calls == meta._HTTP_MAX_ATTEMPTS == 3


def test_package_fetch_retries_truncated_http_response(monkeypatch):
    url = meta.MOLECULE_RUNTIME_INDEX_URL
    calls = 0

    def urlopen(_request, *, timeout):
        nonlocal calls
        calls += 1
        raise http.client.IncompleteRead(b"partial", 100)

    monkeypatch.setattr(meta, "_open_package_url", urlopen)
    monkeypatch.setattr(meta.time, "sleep", lambda _delay: None)

    with pytest.raises(meta.MCPPinLockstepError, match="after 3 attempts"):
        meta._fetch_bytes(url)
    assert calls == meta._HTTP_MAX_ATTEMPTS == 3


@pytest.mark.parametrize(
    "url",
    [
        "https://git.moleculesai.app:8443/api/packages/molecule-ai/pypi/simple/",
        "https://reviewer@git.moleculesai.app/api/packages/molecule-ai/pypi/simple/",
    ],
)
def test_package_fetch_rejects_noncanonical_origin_before_open(monkeypatch, url):
    calls = 0

    def urlopen(_request, *, timeout):
        nonlocal calls
        calls += 1
        return _FakeHTTPResponse(url)

    monkeypatch.setattr(meta, "_open_package_url", urlopen)

    with pytest.raises(meta.MCPPinLockstepError, match="untrusted package URL"):
        meta._fetch_bytes(url)
    assert calls == 0


def test_package_redirect_handler_rejects_off_origin_before_follow():
    request = urllib.request.Request(meta.MOLECULE_RUNTIME_INDEX_URL)
    handler = meta._SameOriginRedirectHandler()

    with pytest.raises(meta.MCPPinLockstepError, match="redirected off origin"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://packages.invalid/runtime.whl",
        )


def test_runtime_wheel_rejects_oversized_member_before_decompression(monkeypatch):
    wheel = _runtime_wheel()
    monkeypatch.setattr(meta, "_MAX_ARCHIVE_MEMBER_BYTES", 64)

    with pytest.raises(meta.MCPPinLockstepError, match="member exceeds"):
        meta._runtime_contract(wheel, "0.4.25")


def test_runtime_wheel_rejects_excess_total_uncompressed_size(monkeypatch):
    wheel = _runtime_wheel()
    monkeypatch.setattr(meta, "_MAX_WHEEL_UNCOMPRESSED_BYTES", 128)

    with pytest.raises(meta.MCPPinLockstepError, match="total uncompressed"):
        meta._runtime_contract(wheel, "0.4.25")


def test_mcp_tarball_rejects_gzip_bomb_before_tar_parse(monkeypatch):
    import gzip

    payload = gzip.compress(b"A" * 257)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode()
    monkeypatch.setattr(meta, "_MAX_TAR_UNCOMPRESSED_BYTES", 256)

    with pytest.raises(meta.MCPPinLockstepError, match="gzip payload exceeds"):
        meta._verify_mcp_tarball(
            payload,
            package="@molecule-ai/mcp-server",
            version="1.8.3",
            integrity=integrity,
            shasum=hashlib.sha1(payload).hexdigest(),
        )


def test_mcp_tarball_rejects_oversized_member(monkeypatch):
    tarball = _mcp_tarball()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    monkeypatch.setattr(meta, "_MAX_ARCHIVE_MEMBER_BYTES", 64)

    with pytest.raises(meta.MCPPinLockstepError, match="member exceeds"):
        meta._verify_mcp_tarball(
            tarball,
            package="@molecule-ai/mcp-server",
            version="1.8.3",
            integrity=integrity,
            shasum=hashlib.sha1(tarball).hexdigest(),
        )


def test_dockerfile_rejects_runtime_pin_without_effective_wheel_acquisition(tmp_path):
    (tmp_path / ".runtime-version").write_text("0.4.25\n")
    (tmp_path / "Dockerfile").write_text(
        "FROM scratch\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN echo \"${RUNTIME_VERSION}\"\n"
        "RUN bash /opt/molecule/prebake-mgmt-mcp.sh\n"
    )

    with pytest.raises(meta.MCPPinLockstepError, match="runtime wheel acquisition"):
        meta._template_runtime_pin(tmp_path)


def test_dockerfile_rejects_echo_only_prebake_marker(tmp_path):
    (tmp_path / ".runtime-version").write_text("0.4.25\n")
    (tmp_path / "Dockerfile").write_text(
        "FROM scratch\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN set -e; runtime_project=\"molecules-workspace-runtime\"; \\\n"
        "    runtime_requirement=\"$(python3 /tmp/prepare-runtime-requirements.py "
        "--runtime-version \"${RUNTIME_VERSION}\")\"; \\\n"
        "    pip download --dest /tmp/molecule-runtime \"${runtime_requirement}\"\n"
        "RUN echo prebake-mgmt-mcp.sh\n"
    )

    with pytest.raises(meta.MCPPinLockstepError, match="executable RUN delegation"):
        meta._template_runtime_pin(tmp_path)


def test_dockerfile_accepts_positional_runtime_prepare_contract(tmp_path):
    (tmp_path / ".runtime-version").write_text("0.4.25\n")
    (tmp_path / "Dockerfile").write_text(
        "FROM scratch\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN set -e; runtime_project=\"molecules-workspace-runtime\"; \\\n"
        "    runtime_requirement=\"$(python3 /tmp/prepare-runtime-requirements.py "
        "requirements.txt /tmp/public.txt \"${RUNTIME_VERSION}\")\"; \\\n"
        "    pip download --dest /tmp/molecule-runtime \"${runtime_requirement}\"\n"
        "RUN bash /opt/molecule/scripts/prebake-mgmt-mcp.sh\n"
    )

    assert meta._template_runtime_pin(tmp_path) == "0.4.25"


def test_dockerfile_rejects_optional_runtime_acquisition(tmp_path):
    (tmp_path / ".runtime-version").write_text("0.4.25\n")
    (tmp_path / "Dockerfile").write_text(
        "FROM scratch\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN pip download \"molecules-workspace-runtime==${RUNTIME_VERSION}\" || true\n"
        "RUN bash /opt/molecule/scripts/prebake-mgmt-mcp.sh\n"
    )

    with pytest.raises(meta.MCPPinLockstepError, match="runtime wheel acquisition"):
        meta._template_runtime_pin(tmp_path)


@pytest.mark.parametrize(
    ("consumer_name", "runtime_run"),
    [
        (
            "claude-code",
            '''set -eu;
            runtime_project="molecules-workspace-runtime";
            rm -rf /tmp/molecule-runtime;
            rm -f /tmp/template-requirements.txt;
            mkdir -p /tmp/molecule-runtime;
            runtime_requirement="$(python3 /tmp/prepare_runtime_requirements.py
              requirements.txt /tmp/template-requirements.txt
              --runtime-version "${RUNTIME_VERSION}")";
            if [ "${runtime_requirement#${runtime_project}}" = "${runtime_requirement}" ]; then
              echo "ERROR: runtime requirement was not canonicalized" >&2;
              exit 1;
            fi;
            pip download --isolated --only-binary=:all: --no-deps
              --index-url "$MOLECULE_RUNTIME_INDEX"
              --dest /tmp/molecule-runtime "${runtime_requirement}";
            set -- /tmp/molecule-runtime/*.whl;
            if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
              echo "ERROR: private runtime acquisition did not produce exactly one wheel" >&2;
              exit 1;
            fi;
            pip install --isolated --no-cache-dir /tmp/molecule-runtime/*.whl
              -r /tmp/template-requirements.txt;
            rm -rf /tmp/molecule-runtime /tmp/template-requirements.txt''',
        ),
        (
            "codex",
            '''set -eux;
            runtime_project="molecules-workspace-runtime";
            runtime_requirement="$(python3 /tmp/prepare_runtime_requirements.py
              requirements.txt /tmp/template-requirements.txt
              --runtime-version "${RUNTIME_VERSION}")";
            case "${runtime_requirement}" in "${runtime_project}"*) ;; *) exit 1 ;; esac;
            rm -rf /tmp/molecule-runtime;
            mkdir -p /tmp/molecule-runtime;
            pip download --isolated --no-cache-dir --only-binary=:all: --no-deps
              --index-url "${MOLECULE_RUNTIME_INDEX}"
              --dest /tmp/molecule-runtime "${runtime_requirement}";
            test "$(find /tmp/molecule-runtime -maxdepth 1 -type f -name '*.whl' | wc -l)" -eq 1;
            pip install --isolated --no-cache-dir /tmp/molecule-runtime/*.whl
              -r /tmp/template-requirements.txt;
            rm -rf /tmp/molecule-runtime /tmp/template-requirements.txt
              /tmp/prepare_runtime_requirements.py;
            python3 -c "import molecule_runtime.preflight as pf; s=getattr(pf,'SUPPORTED_RUNTIMES',None); s.add('codex') if isinstance(s,set) else None; print('preflight SUPPORTED_RUNTIMES shim:', 'patched' if isinstance(s,set) else 'n/a (adapter-module discovery is authoritative)')" || true''',
        ),
        (
            "openclaw",
            '''set -eux;
            runtime_project="molecules-workspace-runtime";
            rm -rf /tmp/molecule-runtime;
            mkdir -p /tmp/molecule-runtime;
            runtime_requirement="$(python3 /usr/local/bin/prepare-runtime-requirements.py
              requirements.txt /tmp/template-requirements.txt "${RUNTIME_VERSION}")";
            case "${runtime_requirement}" in "${runtime_project}"*) ;; *) exit 1 ;; esac;
            pip download --isolated --only-binary=:all: --no-deps
              --index-url "$MOLECULE_RUNTIME_INDEX"
              --dest /tmp/molecule-runtime "${runtime_requirement}";
            runtime_wheel_count="$(find /tmp/molecule-runtime -maxdepth 1 -type f -name 'molecules_workspace_runtime-*.whl' | wc -l)";
            test "${runtime_wheel_count}" -eq 1;
            runtime_wheel="$(find /tmp/molecule-runtime -maxdepth 1 -type f -name 'molecules_workspace_runtime-*.whl')";
            pip install --isolated --no-cache-dir
              "${runtime_wheel}" -r /tmp/template-requirements.txt;
            rm -rf /tmp/molecule-runtime /tmp/template-requirements.txt''',
        ),
        (
            "hermes",
            '''set -eu;
            runtime_project="molecules-workspace-runtime";
            runtime_requirement="$(python3 /usr/local/bin/prepare-runtime-requirements.py
              --requirements requirements.txt
              --output /tmp/template-requirements.txt
              --runtime-version "$RUNTIME_VERSION")";
            case "$runtime_requirement" in "$runtime_project"*) ;; *) exit 1 ;; esac;
            rm -rf /tmp/molecule-runtime;
            mkdir /tmp/molecule-runtime;
            pip download --isolated --only-binary=:all: --no-deps
              --index-url "$MOLECULE_RUNTIME_INDEX"
              --dest /tmp/molecule-runtime "$runtime_requirement";
            wheel_count="$(find /tmp/molecule-runtime -maxdepth 1 -type f -name '*.whl' | wc -l)";
            test "$wheel_count" -eq 1;
            runtime_wheel="$(find /tmp/molecule-runtime -maxdepth 1 -type f -name 'molecules_workspace_runtime-*.whl')";
            test -n "$runtime_wheel";
            pip install --isolated --no-cache-dir "$runtime_wheel"
              -r /tmp/template-requirements.txt;
            rm -rf /tmp/molecule-runtime /tmp/template-requirements.txt''',
        ),
    ],
)
def test_clean_current_consumer_runtime_acquisition_is_recognized(
    consumer_name, runtime_run
):
    consumer = _OFFICIAL_CONSUMERS[consumer_name]
    # Each RUN was extracted from a clean git archive at the recorded immutable ref.
    # The live self-test downloads those same archives and exercises the full runner.
    assert meta._run_acquires_pinned_runtime(runtime_run), consumer["commit"]


def test_official_consumer_archive_manifest_is_exact_and_immutable():
    assert len(_OFFICIAL_CONSUMER_RECORDS) == len(_OFFICIAL_CONSUMERS) == 4
    assert set(_OFFICIAL_CONSUMERS) == {"claude-code", "codex", "openclaw", "hermes"}
    assert all(
        set(item) == {"name", "repository", "commit"}
        and item["repository"] == f"molecule-ai-workspace-template-{name}"
        and re.fullmatch(r"[0-9a-f]{40}", item["commit"])
        for name, item in _OFFICIAL_CONSUMERS.items()
    )


def test_runtime_acquisition_rejects_pipeline_masking():
    run = 'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}" | true'

    assert not meta._run_acquires_pinned_runtime(run)


def test_runtime_acquisition_accepts_explicit_fail_closed_fallback():
    run = (
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}" '
        '|| { echo acquisition failed >&2; exit 1; }'
    )

    assert meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize("status", [256, 512])
@pytest.mark.parametrize("braced", [False, True])
def test_runtime_acquisition_rejects_fallback_exit_status_that_normalizes_to_zero(
    status, braced
):
    fallback = f"{{ exit {status}; }}" if braced else f"exit {status}"
    run = (
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}" '
        f"|| {fallback}"
    )

    assert not meta._run_acquires_pinned_runtime(run)


def test_runtime_acquisition_accepts_fallback_with_effective_nonzero_exit():
    run = (
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}" '
        "|| { exit 257; }"
    )

    assert meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize("positional_set", ["set -- -e", "set positional -e"])
def test_runtime_acquisition_rejects_positional_set_decoy_errexit(positional_set):
    run = (
        f"set +e; {positional_set}; "
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"; true'
    )

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize("positional_set", ["set -- -e", "set positional -e"])
def test_runtime_helper_rejects_positional_set_decoy_errexit(positional_set):
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -e",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            'SPEC="${PKG}@${VER}"',
            "set +e",
            positional_set,
            '_prebake_self_check "${SPEC}"; true',
            '_prebake_self_check "${PKG}@${RANGE}"; true',
        ]
    )

    assert not meta._helper_consumes_mcp_contract(helper)


@pytest.mark.parametrize(
    "invalid_set",
    ["set -eZ", "set -e +Z", "set -e -O extglob +e"],
)
def test_runtime_acquisition_rejects_invalid_set_decoy_errexit(invalid_set):
    run = (
        f"set +e; {invalid_set}; "
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"; true'
    )

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize(
    "invalid_set",
    ["set -eZ", "set -e +Z", "set -e -O extglob +e"],
)
def test_runtime_helper_rejects_invalid_set_decoy_errexit(invalid_set):
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -e",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            'SPEC="${PKG}@${VER}"',
            "set +e",
            invalid_set,
            '_prebake_self_check "${SPEC}"; true',
            '_prebake_self_check "${PKG}@${RANGE}"; true',
        ]
    )

    assert not meta._helper_consumes_mcp_contract(helper)


@pytest.mark.parametrize(
    "noexec_set",
    [
        "set -en",
        "set -e -o noexec",
        "time set -en",
        "command set -en",
        "builtin set -en",
        "! set -en",
    ],
)
def test_runtime_acquisition_rejects_noexec_shell(noexec_set):
    run = (
        f"{noexec_set}; "
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize(
    "noexec_set",
    [
        "set -en",
        "set -e -o noexec",
        "time set -en",
        "command set -en",
        "builtin set -en",
        "! set -en",
    ],
)
def test_prebake_delegation_rejects_noexec_shell(noexec_set):
    run = f"{noexec_set}; bash /opt/molecule/scripts/prebake-mgmt-mcp.sh"

    assert not meta._run_directly_executes_prebake(run)


@pytest.mark.parametrize(
    "noexec_set",
    [
        "set -en",
        "set -e -o noexec",
        "time set -en",
        "command set -en",
        "builtin set -en",
        "! set -en",
    ],
)
def test_runtime_helper_rejects_noexec_shell(noexec_set):
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            noexec_set,
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            'SPEC="${PKG}@${VER}"',
            '_prebake_self_check "${SPEC}"',
            '_prebake_self_check "${PKG}@${RANGE}"',
        ]
    )

    assert not meta._helper_consumes_mcp_contract(helper)


@pytest.mark.parametrize(
    "terminal", ["exit 0", "exit 256", "exec true", "{ exit 0; }"]
)
def test_runtime_acquisition_rejects_prior_green_terminal_control(terminal):
    run = (
        f"{terminal}; "
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize(
    "terminal", ["exit 0", "exit 256", "exec true", "{ exit 0; }"]
)
def test_prebake_delegation_rejects_prior_green_terminal_control(terminal):
    run = f"{terminal}; bash /opt/molecule/scripts/prebake-mgmt-mcp.sh"

    assert not meta._run_directly_executes_prebake(run)


@pytest.mark.parametrize(
    "terminal", ["exit 0", "exit 256", "exec true", "{ exit 0; }"]
)
def test_runtime_helper_rejects_prior_green_terminal_control(terminal):
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -e",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            'SPEC="${PKG}@${VER}"',
            terminal,
            '_prebake_self_check "${SPEC}"',
            '_prebake_self_check "${PKG}@${RANGE}"',
        ]
    )

    assert not meta._helper_consumes_mcp_contract(helper)


@pytest.mark.parametrize(
    "invocation",
    [
        "bash -n /opt/molecule/scripts/prebake-mgmt-mcp.sh",
        "bash --noexec /opt/molecule/scripts/prebake-mgmt-mcp.sh",
        "sh -n /opt/molecule/scripts/prebake-mgmt-mcp.sh",
    ],
)
def test_prebake_delegation_rejects_shell_noexec_modes(invocation):
    assert not meta._run_directly_executes_prebake(invocation)


@pytest.mark.parametrize(
    "invocation",
    [
        "bash -s /opt/molecule/scripts/prebake-mgmt-mcp.sh",
        "sh -s /opt/molecule/scripts/prebake-mgmt-mcp.sh",
        "bash --help /opt/molecule/scripts/prebake-mgmt-mcp.sh",
        "bash --version /opt/molecule/scripts/prebake-mgmt-mcp.sh",
    ],
)
def test_prebake_delegation_rejects_nonexecuting_shell_modes(invocation):
    assert not meta._run_directly_executes_prebake(invocation)


def test_prebake_delegation_accepts_execution_shell_options():
    run = "bash -eux /opt/molecule/scripts/prebake-mgmt-mcp.sh"

    assert meta._run_directly_executes_prebake(run)


def test_prebake_delegation_ignores_unrelated_optional_command():
    run = "compatibility_probe || true; bash /opt/molecule/scripts/prebake-mgmt-mcp.sh"

    assert meta._run_directly_executes_prebake(run)


@pytest.mark.parametrize("mask", [" || true", " | true"])
def test_prebake_delegation_rejects_masked_execution(mask):
    run = f"bash /opt/molecule/scripts/prebake-mgmt-mcp.sh{mask}"

    assert not meta._run_directly_executes_prebake(run)


def test_runtime_acquisition_rejects_command_inside_dead_if_branch():
    run = (
        "set -e; if false; then :; "
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"; fi'
    )

    assert not meta._run_acquires_pinned_runtime(run)


def test_runtime_acquisition_rejects_command_inside_never_called_function():
    run = (
        "set -e; acquire_runtime() { :; "
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"; }; true'
    )

    assert not meta._run_acquires_pinned_runtime(run)


def test_prebake_delegation_rejects_command_inside_never_called_function():
    run = (
        "set -e; bake_runtime() { :; "
        "bash /opt/molecule/scripts/prebake-mgmt-mcp.sh; }; true"
    )

    assert not meta._run_directly_executes_prebake(run)


def test_prebake_delegation_rejects_called_function_control_before_execution():
    run = (
        "disable_execution() { set -n; }; disable_execution; "
        "bash /opt/molecule/scripts/prebake-mgmt-mcp.sh"
    )

    assert not meta._run_directly_executes_prebake(run)


@pytest.mark.parametrize(
    "dynamic_control",
    ["eval 'exit 0'", "source /tmp/unverifiable-prebake-control.sh"],
)
def test_prebake_delegation_rejects_dynamic_control_before_execution(
    dynamic_control,
):
    run = f"{dynamic_control}; bash /opt/molecule/scripts/prebake-mgmt-mcp.sh"

    assert not meta._run_directly_executes_prebake(run)


def test_runtime_acquisition_rejects_attached_posix_subshell():
    run = (
        "(true; set -e; runtime_project=molecules-workspace-runtime; "
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version ${RUNTIME_VERSION})"; '
        'pip download "$runtime_requirement" )'
    )

    assert not meta._run_acquires_pinned_runtime(run)


def test_prebake_delegation_rejects_attached_posix_subshell():
    run = "(true; set -e; bash /opt/molecule/scripts/prebake-mgmt-mcp.sh )"

    assert not meta._run_directly_executes_prebake(run)


def test_runtime_helper_rejects_attached_posix_subshell():
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "(true; set -e",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            'SPEC="${PKG}@${VER}"',
            '_prebake_self_check "${SPEC}"',
            '_prebake_self_check "${PKG}@${RANGE}"',
            "true )",
        ]
    )

    assert not meta._helper_consumes_mcp_contract(helper)


def test_attached_posix_subshell_rejection_preserves_top_level_controls():
    acquisition = 'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"'
    delegation = "bash /opt/molecule/scripts/prebake-mgmt-mcp.sh"
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -e",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            'SPEC="${PKG}@${VER}"',
            '_prebake_self_check "${SPEC}"',
            '_prebake_self_check "${PKG}@${RANGE}"',
        ]
    )

    assert meta._run_acquires_pinned_runtime(acquisition)
    assert meta._run_directly_executes_prebake(delegation)
    assert meta._helper_consumes_mcp_contract(helper)


@pytest.mark.parametrize("mask", [" || true", " | true"])
def test_runtime_helper_rejects_masked_exact_and_range_self_checks(mask):
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -eu",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            'SPEC="${PKG}@${VER}"',
            f'_prebake_self_check "${{SPEC}}"{mask}',
            f'_prebake_self_check "${{PKG}}@${{RANGE}}"{mask}',
        ]
    )

    assert not meta._helper_consumes_mcp_contract(helper)


def test_runtime_helper_accepts_fail_closed_self_check_fallbacks():
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -eu",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            'SPEC="${PKG}@${VER}"',
            '_prebake_self_check "${SPEC}" || { echo exact failed; exit 1; }',
            '_prebake_self_check "${PKG}@${RANGE}" || { echo range failed; exit 1; }',
        ]
    )

    assert meta._helper_consumes_mcp_contract(helper)


def test_runtime_helper_rejects_self_checks_inside_never_called_function():
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -eu",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            'SPEC="${PKG}@${VER}"',
            "verify_only() {",
            '  _prebake_self_check "${SPEC}"',
            '  _prebake_self_check "${PKG}@${RANGE}"',
            "}",
        ]
    )

    assert not meta._helper_consumes_mcp_contract(helper)


@pytest.mark.parametrize(
    "mutation",
    [
        'export VER="9.9.9"',
        'readonly VER="9.9.9"',
        'local VER="9.9.9"',
        'declare -x VER="9.9.9"',
        'typeset -x VER="9.9.9"',
        "unset VER",
        "VER+=.0",
        'VER="9.9.9" true',
    ],
)
def test_runtime_helper_rejects_rebound_exact_version_before_spec(mutation):
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -eu",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            mutation,
            'SPEC="${PKG}@${VER}"',
            '_prebake_self_check "${SPEC}"',
            '_prebake_self_check "${PKG}@${RANGE}"',
        ]
    )

    assert not meta._helper_consumes_mcp_contract(helper)


def test_runtime_helper_accepts_exporting_existing_canonical_bindings():
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -eu",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            "export PKG VER RANGE",
            'SPEC="${PKG}@${VER}"',
            "export SPEC",
            '_prebake_self_check "${SPEC}"',
            '_prebake_self_check "${PKG}@${RANGE}"',
        ]
    )

    assert meta._helper_consumes_mcp_contract(helper)


@pytest.mark.parametrize(
    "mutation",
    [
        "printf -v VER %s 9.9.9",
        "time printf -v VER %s 9.9.9",
        "time -p read -r VER <<< 9.9.9",
        "read -r VER <<< 9.9.9",
        "read -ra VER <<< 9.9.9",
        "read -aVER",
        "while read -r VER; do :; done <<< 9.9.9",
        "mapfile -t VER < input",
        "wait -p VER 123",
        "for VER in 9.9.9; do :; done",
        "select VER in 9.9.9; do break; done <<< 1",
        "declare -n REPLY=VER; read <<< 9.9.9",
        "declare -n OPTIND=VER; set -- -x; getopts x option",
        "declare -n PWD=VER; cd /tmp",
        "declare -n version_alias=VER; version_alias=9.9.9",
        "typeset -n version_alias=VER; printf -v version_alias %s 9.9.9",
        'target=VER; local -n version_alias="$target"; version_alias=9.9.9',
    ],
)
def test_runtime_helper_rejects_implicit_or_indirect_version_write(mutation):
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -eu",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            mutation,
            'SPEC="${PKG}@${VER}"',
            '_prebake_self_check "${SPEC}"',
            '_prebake_self_check "${PKG}@${RANGE}"',
        ]
    )

    assert not meta._helper_consumes_mcp_contract(helper)


@pytest.mark.parametrize(
    "mutation",
    [
        "printf -v harmless %s value",
        "read -r harmless <<< value",
        "read -aharmless <<< value",
        "mapfile -t harmless < input",
        "wait -p harmless 123",
        "for harmless in value; do :; done",
        "declare -n harmless_alias=harmless; harmless_alias=value",
        "declare -n harmless_alias=VER; unset -n harmless_alias; harmless_alias=value",
        "declare -n harmless_alias=VER; declare +n harmless_alias; harmless_alias=value",
    ],
)
def test_runtime_helper_allows_implicit_write_to_unprotected_variable(mutation):
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -eu",
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            mutation,
            'SPEC="${PKG}@${VER}"',
            '_prebake_self_check "${SPEC}"',
            '_prebake_self_check "${PKG}@${RANGE}"',
        ]
    )

    assert meta._helper_consumes_mcp_contract(helper)


@pytest.mark.parametrize(
    "mutation",
    [
        "eval ':'",
        "source /tmp/mutate-helper.sh",
        "trap 'VER=9.9.9' DEBUG",
        "mutate_pin() { VER=9.9.9; }; mutate_pin",
    ],
)
def test_runtime_helper_rejects_unverifiable_control_before_bindings(mutation):
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -eu",
            mutation,
            'PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"',
            'VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"',
            'RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"',
            'SPEC="${PKG}@${VER}"',
            '_prebake_self_check "${SPEC}"',
            '_prebake_self_check "${PKG}@${RANGE}"',
        ]
    )

    assert not meta._helper_consumes_mcp_contract(helper)


@pytest.mark.parametrize("mutation", ["overwrite", "unset"])
def test_runtime_acquisition_rejects_invalidated_prepared_requirement(mutation):
    change = (
        'runtime_requirement="requests==2.32.0";'
        if mutation == "overwrite"
        else "unset runtime_requirement;"
    )
    run = (
        'set -e; runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")"; '
        f"{change} "
        'pip download "$runtime_requirement"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize("builtin", ["export", "readonly", "declare", "typeset"])
def test_runtime_acquisition_rejects_assignment_builtin_overwrite(builtin):
    run = (
        'set -e; runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")"; '
        f'{builtin} runtime_requirement="requests==2.32.0"; '
        'pip download "$runtime_requirement"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize("builtin", ["export", "readonly", "declare", "typeset", "local"])
@pytest.mark.parametrize("binding", ["runtime_project", "runtime_requirement"])
def test_runtime_acquisition_rejects_declaration_assignment_as_canonical_binding(
    builtin, binding
):
    runtime_project = (
        f'{builtin} runtime_project="molecules-workspace-runtime";'
        if binding == "runtime_project"
        else 'runtime_project="molecules-workspace-runtime";'
    )
    runtime_requirement = (
        f'{builtin} runtime_requirement="$(python3 '
        '/tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")";'
        if binding == "runtime_requirement"
        else 'runtime_requirement="$(python3 '
        '/tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")";'
    )
    run = (
        f"set -e; {runtime_project} {runtime_requirement} "
        'pip download "$runtime_requirement"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


def test_runtime_acquisition_accepts_exporting_existing_canonical_bindings():
    run = (
        'set -e; runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")"; '
        'export runtime_project runtime_requirement; '
        'pip download "$runtime_requirement"'
    )

    assert meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize(
    "run",
    [
        'set -e; RUNTIME_VERSION="9.9.9"; '
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"',
        'RUNTIME_VERSION="9.9.9" '
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"',
        'export RUNTIME_VERSION="9.9.9"; '
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"',
    ],
)
def test_runtime_acquisition_rejects_shadowed_runtime_version(run):
    assert not meta._run_acquires_pinned_runtime(run)


def test_runtime_acquisition_rejects_overwritten_runtime_project_identity():
    run = (
        'set -e; runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")"; '
        'runtime_project="requests"; '
        'pip download "$runtime_requirement"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


def test_runtime_acquisition_rejects_shadowed_runtime_project_despite_decoy_identity():
    run = (
        'set -e; decoy="molecules-workspace-runtime"; '
        'runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")"; '
        'runtime_project="requests"; '
        'pip download "$runtime_requirement"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize(
    "transient_assignment",
    [
        'runtime_project="molecules-workspace-runtime" true; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")";',
        'runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")" true;',
    ],
)
def test_runtime_acquisition_rejects_nonreaching_environment_assignment(
    transient_assignment,
):
    run = f'set -e; {transient_assignment} pip download "$runtime_requirement"'

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize("assignment_edge", ["| true", "& wait", "&& true"])
def test_runtime_acquisition_rejects_masked_or_nonpersistent_prepare_assignment(
    assignment_edge,
):
    run = (
        'set -e; runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="requests==2.32.0"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")" '
        f"{assignment_edge}; "
        'pip download "$runtime_requirement"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


def test_runtime_acquisition_rejects_prepare_output_from_masked_failure():
    # The prepare command can print a wrong project and then fail. The assignment keeps
    # that output, while ``|| true`` masks its nonzero status before pip consumes it.
    run = (
        'set -e; runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="requests==2.32.0"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")" || true; '
        'pip download "$runtime_requirement"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize(
    "run",
    [
        'set -e; RUNTIME_VERSION+=0; '
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"',
        'set -e; export RUNTIME_VERSION+=0; '
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"',
        'set -e; runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")"; '
        'runtime_project+="-shadow"; pip download "$runtime_requirement"',
        'set -e; runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")"; '
        'runtime_requirement+="-shadow"; pip download "$runtime_requirement"',
    ],
)
def test_runtime_acquisition_rejects_augmented_protected_assignment(run):
    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize(
    "mutation",
    [
        "printf -v RUNTIME_VERSION %s 9.9.9",
        "time printf -v RUNTIME_VERSION %s 9.9.9",
        "time -p read -r RUNTIME_VERSION <<< 9.9.9",
        "read -r RUNTIME_VERSION <<< 9.9.9",
        "read -ra RUNTIME_VERSION <<< 9.9.9",
        "read -aRUNTIME_VERSION",
        "if read -r RUNTIME_VERSION <<< 9.9.9; then :; fi",
        "mapfile -t RUNTIME_VERSION < input",
        "wait -p RUNTIME_VERSION 123",
        "for RUNTIME_VERSION in 9.9.9; do :; done",
        "select RUNTIME_VERSION in 9.9.9; do break; done <<< 1",
        "getopts x RUNTIME_VERSION",
        "declare -n REPLY=RUNTIME_VERSION; read <<< 9.9.9",
        "declare -n OPTARG=RUNTIME_VERSION; set -- -x 9.9.9; "
        "getopts x: option",
        "declare -n BASH_REMATCH=RUNTIME_VERSION; [[ 9.9.9 =~ (.*) ]]",
        "declare -n pin_alias=RUNTIME_VERSION; pin_alias=9.9.9",
        "typeset -n pin_alias=RUNTIME_VERSION; printf -v pin_alias %s 9.9.9",
        'target=RUNTIME_VERSION; local -n pin_alias="$target"; pin_alias=9.9.9',
        'target=RUNTIME_VERSION; printf -v "$target" %s 9.9.9',
        "trap 'RUNTIME_VERSION=9.9.9' DEBUG",
    ],
)
def test_runtime_acquisition_rejects_implicit_or_indirect_runtime_version_write(
    mutation,
):
    run = (
        f"{mutation}; "
        'pip download "molecules-workspace-runtime==${RUNTIME_VERSION}"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize(
    "mutation",
    [
        "printf -v runtime_requirement %s requests==2.32.0",
        "time printf -v runtime_requirement %s requests==2.32.0",
        "time -p read -r runtime_requirement <<< requests==2.32.0",
        "read -r runtime_requirement <<< requests==2.32.0",
        "read -a runtime_requirement <<< requests==2.32.0",
        "read -aruntime_requirement",
        "until read -r runtime_requirement; do break; done <<< requests==2.32.0",
        "readarray -t runtime_requirement < input",
        "wait -p runtime_requirement 123",
        "for runtime_requirement in requests==2.32.0; do :; done",
        "select runtime_requirement in requests==2.32.0; do break; done <<< 1",
        "declare -n OPTARG=runtime_requirement; set -- -x requests==2.32.0; "
        "getopts x: option",
        "declare -n OLDPWD=runtime_requirement; cd /tmp",
        "declare -n req_alias=runtime_requirement; req_alias=requests==2.32.0",
        "typeset -n req_alias=runtime_requirement; "
        "printf -v req_alias %s requests==2.32.0",
    ],
)
def test_runtime_acquisition_rejects_implicit_or_indirect_prepared_write(mutation):
    run = (
        'set -e; runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")"; '
        f"{mutation}; "
        'pip download "$runtime_requirement"'
    )

    assert not meta._run_acquires_pinned_runtime(run)


@pytest.mark.parametrize(
    "mutation",
    [
        "printf -v harmless %s value",
        "read -r harmless <<< value",
        "read -aharmless <<< value",
        "mapfile -t harmless < input",
        "wait -p harmless 123",
        "for harmless in value; do :; done",
        "declare -n harmless_alias=harmless; harmless_alias=value",
        "declare -n harmless_alias=RUNTIME_VERSION; "
        "unset -n harmless_alias; harmless_alias=value",
        "declare -n harmless_alias=RUNTIME_VERSION; "
        "declare +n harmless_alias; harmless_alias=value",
    ],
)
def test_runtime_acquisition_allows_implicit_write_to_unprotected_variable(mutation):
    run = (
        f"set -e; {mutation}; "
        'runtime_project="molecules-workspace-runtime"; '
        'runtime_requirement="$(python3 /tmp/prepare-runtime-requirements.py '
        '--runtime-version "${RUNTIME_VERSION}")"; '
        'pip download "$runtime_requirement"'
    )

    assert meta._run_acquires_pinned_runtime(run)


def test_runtime_wheel_rejects_helper_markers_in_comments_and_echoes():
    helper = "\n".join(
        [
            "#!/usr/bin/env bash",
            "# _read MANAGEMENT_MCP_PINNED_VERSION",
            "echo 'SPEC=\"${PKG}@${VER}\"'",
            "echo '_prebake_self_check \"${SPEC}\"'",
        ]
    )

    with pytest.raises(meta.MCPPinLockstepError, match="does not consume"):
        meta._runtime_contract(_runtime_wheel(helper=helper), "0.4.25")


@pytest.mark.parametrize(
    "source_extra",
    [
        'MANAGEMENT_MCP_PINNED_VERSION = "".join(["9", ".9.9"])',
        'MANAGEMENT_MCP_PINNED_VERSION = "1.8.3"',
        'MANAGEMENT_MCP_PINNED_VERSION += ".0"',
        'if True:\n    MANAGEMENT_MCP_PINNED_VERSION = "9.9.9"',
        "del MANAGEMENT_MCP_PINNED_VERSION",
        'MANAGEMENT_MCP_PINNED_VERSION: str = "9.9.9"',
        '(MANAGEMENT_MCP_PINNED_VERSION := "9.9.9")',
        "import json as MANAGEMENT_MCP_PINNED_VERSION",
        "def MANAGEMENT_MCP_PINNED_VERSION():\n    return '9.9.9'",
        "class MANAGEMENT_MCP_PINNED_VERSION:\n    pass",
        "async def MANAGEMENT_MCP_PINNED_VERSION():\n    pass",
        "def shadow(MANAGEMENT_MCP_PINNED_VERSION):\n    pass",
        "lambda MANAGEMENT_MCP_PINNED_VERSION: None",
        "from json import loads as MANAGEMENT_MCP_PINNED_VERSION",
        "from json import *",
        "try:\n    raise RuntimeError\nexcept RuntimeError as MANAGEMENT_MCP_PINNED_VERSION:\n    pass",
        "match '9.9.9':\n    case MANAGEMENT_MCP_PINNED_VERSION:\n        pass",
        "match []:\n    case [*MANAGEMENT_MCP_PINNED_VERSION]:\n        pass",
        "match {}:\n    case {**MANAGEMENT_MCP_PINNED_VERSION}:\n        pass",
    ],
)
def test_runtime_wheel_rejects_any_additional_write_to_contract_constant(source_extra):
    with pytest.raises(meta.MCPPinLockstepError, match="literal assignment"):
        meta._runtime_contract(_runtime_wheel(source_extra=source_extra), "0.4.25")


def test_mcp_pin_lockstep_fails_closed_when_pin_is_outside_compatible_range(tmp_path):
    responses, fetch = _mcp_lockstep_fixture(tmp_path)
    wheel_url = next(url for url in responses if url.endswith(".whl"))
    wheel = _runtime_wheel(pinned="2.0.0", compatible="^1.8.0")
    responses[wheel_url] = wheel
    wheel_sha = hashlib.sha256(wheel).hexdigest()
    responses[meta.MOLECULE_RUNTIME_INDEX_URL] = (
        responses[meta.MOLECULE_RUNTIME_INDEX_URL]
        .decode()
        .replace(
            responses[meta.MOLECULE_RUNTIME_INDEX_URL]
            .decode()
            .split("#sha256=")[1]
            .split('"')[0],
            wheel_sha,
        )
        .encode()
    )

    ok, detail = meta._run_mcp_pin_lockstep(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "outside compatible range" in detail


def test_cli_mcp_bundle_fails_closed_before_network_when_runtime_metadata_missing(tmp_path):
    (tmp_path / "repo-meta.yaml").write_text(
        "schema_version: 1\n"
        "layer: runtime-template\n"
        "capabilities: [mcp-server-bake]\n"
    )

    proc = _run_cli(tmp_path)

    assert proc.returncode == 1
    assert "FAIL    mcp-pin-lockstep" in proc.stdout
    assert ".runtime-version" in proc.stdout


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
