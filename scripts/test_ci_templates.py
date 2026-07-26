"""Regression tests for canonical consumer workflow templates."""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_TEMPLATE = REPO_ROOT / "templates" / "ci-workspace-template.yml"
MINIMAL_TEMPLATE = REPO_ROOT / "templates" / "ci-minimal.yml"
DIFF_SECRET_TEMPLATE = REPO_ROOT / "templates" / "ci-secret-scan.yml"
CONFORMANCE_TEMPLATE = REPO_ROOT / "templates" / "ci-conformance-gate.yml"
SOP_GATE_TEMPLATE = REPO_ROOT / "templates" / "ci-sop-checklist-gate.yml"
PINNED_MOLECULE_CI_REF = (
    "45fbe831b983a4bb48bdeb" + "19499e9eb5e8fef3dd"
)
CONSUMER_TEMPLATES = tuple(sorted((REPO_ROOT / "templates").glob("ci-*.yml")))
SCRIPT_FETCH_TEMPLATES = tuple(
    path for path in CONSUMER_TEMPLATES if path != CONFORMANCE_TEMPLATE
)
SECRET_SCANNING_WORKFLOWS = (
    WORKSPACE_TEMPLATE,
    REPO_ROOT / "templates" / "ci-plugin.yml",
    REPO_ROOT / "templates" / "ci-org-template.yml",
)


def _all_run_steps(path: Path) -> list[str]:
    workflow = yaml.safe_load(path.read_text())
    return [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]


def _canonical_script_references(path: Path) -> set[str]:
    return {
        match
        for command in _all_run_steps(path)
        for match in re.findall(
            r'molecule-ci-ssot/((?:\.molecule-ci/)?scripts/[^\s"]+\.py)',
            command,
        )
    }


def test_workspace_template_only_invokes_scripts_present_in_fetched_ci_repo() -> None:
    references = _canonical_script_references(WORKSPACE_TEMPLATE)
    assert references
    for reference in references:
        source_path = REPO_ROOT / reference
        assert source_path.is_file(), f"template references missing {source_path}"


@pytest.mark.parametrize("path", CONSUMER_TEMPLATES)
def test_consumer_templates_only_invoke_scripts_present_in_cloned_ci_repo(
    path: Path,
) -> None:
    references = _canonical_script_references(path)
    for reference in references:
        source_path = REPO_ROOT / reference
        assert source_path.is_file(), f"{path.name} references missing {source_path}"


@pytest.mark.parametrize("path", CONSUMER_TEMPLATES)
def test_consumer_templates_never_use_remote_workflow_call(path: Path) -> None:
    assert not re.search(
        r"uses:\s+\S+/\.gitea/workflows/\S+@", path.read_text()
    ), f"{path.name} uses unsupported cross-repository workflow_call"


@pytest.mark.parametrize("path", CONSUMER_TEMPLATES)
def test_inline_ssot_templates_pin_and_verify_an_immutable_ref(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text())
    job = next(iter(workflow["jobs"].values()))
    ref = job["env"]["MOLECULE_CI_REF"]
    commands = "\n".join(_all_run_steps(path))
    assert re.fullmatch(r"[0-9a-f]{40}", ref)
    assert ref == PINNED_MOLECULE_CI_REF
    assert "git clone" not in commands
    assert 'fetch -q --depth 1 origin "$MOLECULE_CI_REF"' in commands
    assert 'rev-parse HEAD)" = "$MOLECULE_CI_REF"' in commands


def test_inline_ssot_templates_assert_execution_sentinels() -> None:
    assert "minimal-validate:sentinel:executed" in MINIMAL_TEMPLATE.read_text()
    assert "secret-scan:sentinel:executed" in DIFF_SECRET_TEMPLATE.read_text()
    assert "sop-checklist:sentinel:executed" in SOP_GATE_TEMPLATE.read_text()


def test_minimal_template_has_a_credential_free_hash_locked_bootstrap() -> None:
    workflow = yaml.safe_load(MINIMAL_TEMPLATE.read_text())
    steps = workflow["jobs"]["minimal-validate"]["steps"]
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith(
        "actions/checkout@"
    ))
    setup_python = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    commands = "\n".join(_all_run_steps(MINIMAL_TEMPLATE))

    assert checkout["uses"] == (
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
    )
    assert checkout["with"] == {"persist-credentials": False}
    assert setup_python["uses"] == (
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405"
    )
    assert setup_python["with"] == {"python-version": "3.11.15"}
    assert "GIT_CONFIG_GLOBAL=/dev/null" in commands
    assert "GIT_CONFIG_NOSYSTEM=1" in commands
    assert "GIT_TERMINAL_PROMPT=0" in commands
    assert "GIT_ASKPASS=/bin/false" in commands
    assert "http.userAgent=curl/8.4.0" in commands
    assert "PyYAML==6.0.3" in commands
    assert (
        "--hash=sha256:"
        "b8bb0864c5a28024fac8a632c443c87c5aa6f215c0b126c449ae1a150412f31d"
        in commands
    )
    assert "--require-hashes" in commands
    assert "--only-binary=:all:" in commands
    assert "pip install --break-system-packages pyyaml -q" not in commands


@pytest.mark.parametrize(
    ("path", "job_name"),
    (
        (MINIMAL_TEMPLATE, "minimal-validate"),
        (SOP_GATE_TEMPLATE, "gate"),
    ),
)
def test_hash_locked_bootstrap_keeps_setup_python_runtime_linkable(
    path: Path,
    job_name: str,
) -> None:
    """A cold setup-python install must survive the otherwise empty env."""
    workflow = yaml.safe_load(path.read_text())
    install_step = next(
        step
        for step in workflow["jobs"][job_name]["steps"]
        if str(step.get("name", "")).startswith("Install exact hash-locked")
    )
    command = install_step["run"]

    assert command.count("CDPATH='' cd -P --") == 2
    assert "${RUNNER_TOOL_CACHE:?runner did not export RUNNER_TOOL_CACHE}" in command
    assert "${pythonLocation:?setup-python did not export pythonLocation}" in command
    assert '"$TOOL_CACHE_ROOT"/Python/3.11.15/*' in command
    assert 'PYTHON_BIN="$PYTHON_ROOT/bin/python3"' in command
    assert 'PYTHON_LIB="$PYTHON_ROOT/lib"' in command
    assert 'test -x "$PYTHON_BIN"' in command
    assert 'test -d "$PYTHON_LIB"' in command
    isolated = command.split("env -i", 1)[1]
    assert 'PATH="$PYTHON_ROOT/bin:/usr/bin:/bin"' in isolated
    assert 'LD_LIBRARY_PATH="$PYTHON_LIB"' in isolated
    assert '"$PYTHON_BIN" -m pip install' in isolated
    assert "\n            python3 -m pip install" not in isolated


@pytest.mark.parametrize(
    ("path", "job_name"),
    (
        (MINIMAL_TEMPLATE, "minimal-validate"),
        (SOP_GATE_TEMPLATE, "gate"),
    ),
)
def test_hash_locked_bootstrap_runs_in_a_cold_scrubbed_runtime(
    path: Path,
    job_name: str,
    tmp_path: Path,
) -> None:
    """Execute the shipped block against a probe that needs its private lib."""
    workflow = yaml.safe_load(path.read_text())
    command = next(
        step["run"]
        for step in workflow["jobs"][job_name]["steps"]
        if str(step.get("name", "")).startswith("Install exact hash-locked")
    )
    tool_cache = tmp_path / "tool-cache"
    python_root = tool_cache / "Python" / "3.11.15" / "x64"
    python_bin = python_root / "bin"
    python_lib = python_root / "lib"
    runner_temp = tmp_path / "runner-temp"
    probe = tmp_path / "probe"
    python_bin.mkdir(parents=True)
    python_lib.mkdir()
    runner_temp.mkdir()

    expected_path = f"{python_bin}:/usr/bin:/bin"
    fake_python = python_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f'test "${{LD_LIBRARY_PATH:-}}" = {shlex.quote(str(python_lib))}\n'
        f'test "$PATH" = {shlex.quote(expected_path)}\n'
        f'test "$HOME" = {shlex.quote(str(runner_temp))}\n'
        'test -z "${UNTRUSTED_MARKER+x}"\n'
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(probe))}\n"
    )
    fake_python.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", command],
        env={
            "HOME": str(tmp_path / "untrusted-home"),
            "PATH": "/untrusted/bin:/usr/bin:/bin",
            "RUNNER_TEMP": str(runner_temp),
            "RUNNER_TOOL_CACHE": str(tool_cache),
            "UNTRUSTED_MARKER": "must-not-cross-env-i",
            "pythonLocation": str(python_root),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert probe.read_text().splitlines() == [
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--break-system-packages",
        "--no-deps",
        "--only-binary=:all:",
        "--require-hashes",
        "-r",
        str(
            runner_temp
            / f"molecule-ci-{'minimal' if path == MINIMAL_TEMPLATE else 'sop'}.lock"
        ),
    ]


@pytest.mark.parametrize("escape_kind", ("outside", "traversal", "symlink"))
@pytest.mark.parametrize(
    ("path", "job_name"),
    (
        (MINIMAL_TEMPLATE, "minimal-validate"),
        (SOP_GATE_TEMPLATE, "gate"),
    ),
)
def test_hash_locked_bootstrap_rejects_runtime_path_escape(
    path: Path,
    job_name: str,
    escape_kind: str,
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load(path.read_text())
    command = next(
        step["run"]
        for step in workflow["jobs"][job_name]["steps"]
        if str(step.get("name", "")).startswith("Install exact hash-locked")
    )
    tool_cache = tmp_path / "tool-cache"
    outside_root = tmp_path / "outside"
    (outside_root / "bin").mkdir(parents=True)
    (outside_root / "lib").mkdir()
    probe = tmp_path / "invoked"
    fake_python = outside_root / "bin" / "python3"
    fake_python.write_text(
        f"#!/bin/sh\n: > {shlex.quote(str(probe))}\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    expected_parent = tool_cache / "Python" / "3.11.15"
    expected_parent.mkdir(parents=True)
    if escape_kind == "outside":
        python_location = outside_root
    elif escape_kind == "traversal":
        python_location = expected_parent / ".." / ".." / ".." / "outside"
    else:
        python_location = expected_parent / "x64"
        python_location.symlink_to(outside_root, target_is_directory=True)

    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    result = subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", command],
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/bin:/bin",
            "RUNNER_TEMP": str(runner_temp),
            "RUNNER_TOOL_CACHE": str(tool_cache),
            "pythonLocation": str(python_location),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert not probe.exists()


def test_sop_template_contains_secrets_to_one_hardened_gate_step() -> None:
    workflow = yaml.safe_load(SOP_GATE_TEMPLATE.read_text())
    steps = workflow["jobs"]["gate"]["steps"]
    checkout = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    setup_python = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    commands = "\n".join(_all_run_steps(SOP_GATE_TEMPLATE))

    assert checkout["uses"] == (
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
    )
    assert checkout["with"] == {
        "ref": "${{ github.event.repository.default_branch }}",
        "persist-credentials": False,
    }
    assert setup_python["uses"] == (
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405"
    )
    assert setup_python["with"] == {"python-version": "3.11.15"}
    for required in (
        "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_TERMINAL_PROMPT=0",
        "GIT_ASKPASS=/bin/false",
        "http.userAgent=curl/8.4.0",
        "PyYAML==6.0.3",
        "--require-hashes",
        "--only-binary=:all:",
        "--data-binary @-",
        "-H @-",
        "-A curl/8.4.0",
        "--connect-timeout",
        "--max-time",
        "--proto '=https'",
        'GITEA_TOKEN="$resolved_token"',
        "python3 -I",
    ):
        assert required in commands
    for forbidden in (
        'clientSecret\\":\\"$INFISICAL_CI_CLIENT_SECRET',
        '-H "Authorization: Bearer $TOK"',
        "::add-mask::",
        "GITHUB_ENV",
        "len=${#VALUE}",
        "pip install --break-system-packages pyyaml -q",
        "export GITEA_TOKEN",
    ):
        assert forbidden not in commands

    secret_steps = [
        step
        for step in steps
        if any(
            name in step.get("env", {})
            for name in (
                "INFISICAL_CI_CLIENT_SECRET",
                "SOP_CHECKLIST_GATE_TOKEN",
                "SOP_TIER_CHECK_TOKEN",
                "RFC_324_TEAM_READ_TOKEN",
                "GITEA_TOKEN",
                "GITHUB_TOKEN",
            )
        )
    ]
    assert len(secret_steps) == 1
    assert "Run sop-checklist-gate" in secret_steps[0]["name"]
    gate_run = secret_steps[0]["run"]
    assert (
        gate_run.index(
            "unset INFISICAL_CI_CLIENT_ID INFISICAL_CI_CLIENT_SECRET"
        )
        < gate_run.index("TOK=$(")
        < gate_run.index('GITEA_TOKEN="$resolved_token"')
    )
    assert gate_run.count('GITEA_TOKEN="$resolved_token"') == 1
    assert not {
        "SECRET_SOP_TIER_CHECK_TOKEN",
        "RFC_324_TEAM_READ_TOKEN",
        "REPO_GITEA_TOKEN",
        "GH_TOKEN",
    } & set(secret_steps[0]["env"])


@pytest.mark.parametrize("path", SCRIPT_FETCH_TEMPLATES)
def test_script_templates_fetch_outside_the_consumer_checkout(path: Path) -> None:
    commands = "\n".join(_all_run_steps(path))
    assert "$RUNNER_TEMP/molecule-ci-ssot" in commands
    assert 'mkdir "$CI_ROOT"' in commands
    assert 'mkdir -p "$CI_ROOT"' not in commands
    assert 'rm -rf "$CI_ROOT"' not in commands
    assert "git init -q .molecule-ci" not in commands
    assert "git init -q .molecule-ci-canonical" not in commands


def test_local_action_template_uses_a_guarded_dedicated_checkout() -> None:
    content = CONFORMANCE_TEMPLATE.read_text()
    commands = "\n".join(_all_run_steps(CONFORMANCE_TEMPLATE))
    assert "mkdir .molecule-ci-ssot" in commands
    assert "mkdir -p .molecule-ci-ssot" not in commands
    assert "rm -rf .molecule-ci-ssot" not in commands
    assert "git init -q .molecule-ci-ssot" in commands
    assert "uses: ./.molecule-ci-ssot/.gitea/actions/conformance-gate" in content


@pytest.mark.parametrize(
    "path",
    (WORKSPACE_TEMPLATE,),
)
def test_workspace_runtime_install_uses_source_pinned_installer(path: Path) -> None:
    commands = _all_run_steps(path)
    installers = [
        command for command in commands
        if "install_workspace_dependencies.py" in command
    ]
    assert len(installers) == 1
    assert all(
        not (
            "--extra-index-url" in command
            and "molecules-workspace-runtime" in command
        )
        for command in commands
    )


@pytest.mark.parametrize("path", SECRET_SCANNING_WORKFLOWS)
def test_workflow_secret_scans_use_redacting_canonical_script(path: Path) -> None:
    content = path.read_text()
    commands = _all_run_steps(path)

    assert "match.group(0)" not in content
    scanners = [command for command in commands if "check-secrets.py" in command]
    assert len(scanners) == 1
