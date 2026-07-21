"""Tests for the static official runtime-template CI contract gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).with_name("official_consumer_contract.py")
VERIFIER_REF = "".join(("11b8598e5c0b3f0b1031733a8d5f6bc", "238f146a4"))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "official_consumer_contract_tested", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contract = _load_module()


def _workflow(*, verifier_ref: str = VERIFIER_REF) -> bytes:
    return f"""\
on: [push, pull_request]

jobs:
  validate-static:
    runs-on: ubuntu-latest
    steps:
      - run: echo static
  t4-conformance:
    runs-on: docker-host
    needs: validate-static
    if: github.event.pull_request.head.repo.fork != true
    env:
      MOLECULE_CI_REF: {verifier_ref}
    steps:
      - name: Prove management MCP in the final image
        run: |
          set -euo pipefail
          T4_TAG="t4-conformance-test:${{GITHUB_RUN_ID:-local}}-${{GITHUB_RUN_ATTEMPT:-1}}"
          CI_ROOT="$RUNNER_TEMP/molecule-ci-${{GITHUB_RUN_ID:-local}}-${{GITHUB_RUN_ATTEMPT:-1}}"
          MCP_ATTESTATION="$RUNNER_TEMP/mcp-attestation-${{GITHUB_RUN_ID:-local}}-${{GITHUB_RUN_ATTEMPT:-1}}.json"
          MCP_VERIFY_CONTAINER="mcp-verify-${{GITHUB_RUN_ID:-local}}-${{GITHUB_RUN_ATTEMPT:-1}}"
          git init "$CI_ROOT"
          git -C "$CI_ROOT" remote add origin \
            https://git.moleculesai.app/molecule-ai/molecule-ci.git
          GIT_ASKPASS=/bin/false GIT_TERMINAL_PROMPT=0 \\
            git -c credential.helper= -c http.userAgent=curl/8.4.0 \\
            -C "$CI_ROOT" fetch --no-tags --depth 1 origin "$MOLECULE_CI_REF"
          git -C "$CI_ROOT" checkout -q --detach FETCH_HEAD
          test "$(git -C "$CI_ROOT" rev-parse HEAD)" = "$MOLECULE_CI_REF"
          python3 "$CI_ROOT/scripts/mcp_pin_lockstep.py" --repo-root . --json \
            > "$MCP_ATTESTATION"
          EXPECTED_RUNTIME_VERSION="$(python3 - <<'PY'
          from mcp_built_image_e2e import load_attestation
          load_attestation(stream)
          PY
          )"
          docker build --build-arg RUNTIME_VERSION="$EXPECTED_RUNTIME_VERSION" \\
            -t "$T4_TAG" .
          docker create --interactive --name "$MCP_VERIFY_CONTAINER" \\
            --network none --user 1000:1000 --workdir /tmp \\
            --cap-drop ALL --security-opt no-new-privileges \\
            --pids-limit 128 --memory 768m --cpus 1 \\
            --tmpfs /tmp:size=64m --entrypoint python3 "$T4_TAG" \\
            /mcp_built_image_e2e.py
          docker cp "$CI_ROOT/scripts/mcp_built_image_e2e.py" \\
            "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py"
          docker start --attach --interactive "$MCP_VERIFY_CONTAINER" \\
            < "$MCP_ATTESTATION"
          grep -qxF 'mcp-built-image-e2e:sentinel:executed' "$MCP_E2E_LOG"
          KEEP_T4_IMAGE=1
  validate:
    runs-on: ubuntu-latest
    needs: [t4-conformance]
    if: always()
    steps:
      - run: test "${{{{ needs.t4-conformance.result }}}}" = success
""".encode()


def _with_workflow_env(workflow: bytes, verifier_ref: str = VERIFIER_REF) -> bytes:
    return f"env:\n  MOLECULE_CI_REF: {verifier_ref}\n".encode() + workflow


def _test_contract(consumer: str, *, verifier_ref: str = VERIFIER_REF) -> bytes:
    prefix, suffix = verifier_ref[:32], verifier_ref[32:]
    common_body = """\
    workflow = yaml.safe_load(CI_WORKFLOW.read_text())
    workflow_ref = workflow["jobs"]["t4-conformance"]["env"]["MOLECULE_CI_REF"]
    build = workflow["jobs"]["t4-conformance"]
    required_fragments = (
        "GIT_ASKPASS=/bin/false",
        "credential.helper=",
        "http.userAgent=curl/8.4.0",
        "--no-tags",
        "--depth 1",
        'origin "$MOLECULE_CI_REF"',
        "mcp_pin_lockstep.py",
        "mcp_built_image_e2e.py",
        "--network none",
        "--user 1000:1000 --workdir /tmp",
        "--cap-drop ALL --security-opt no-new-privileges",
        "--pids-limit 128 --memory 768m --cpus 1",
        "--tmpfs /tmp:size=64m",
        "mcp-built-image-e2e:sentinel:executed",
    )
    assert workflow_ref == MOLECULE_CI_REF
    assert FORK_RUN in build
    for fragment in required_fragments:
        assert fragment in build
    assert "--volume" not in build
"""
    if consumer == "hermes":
        functions = f"""\
def test_t4_fetches_exact_molecule_ci_and_generates_attestation():
    assert MOLECULE_CI_REF
    assert "mcp_pin_lockstep.py"

def test_t4_runs_hardened_final_image_mcp_e2e_before_privileged_probe():
{common_body}
"""
    else:
        functions = f"""\
def test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image():
{common_body}
"""
    return (
        "import yaml\n"
        "from pathlib import Path\n\n"
        "CI_WORKFLOW = Path('.gitea/workflows/ci.yml')\n"
        "FORK_RUN = 'github.event.pull_request.head.repo.fork != true'\n"
        "MOLECULE_CI_REF = ''.join((\""
        + prefix
        + '", "'
        + suffix
        + '"))\n\n'
        + functions
    ).encode()


@pytest.mark.parametrize("consumer", ("claude-code", "codex", "openclaw", "hermes"))
def test_valid_official_contract_is_accepted(consumer: str) -> None:
    contract.validate_contract(consumer, _workflow(), _test_contract(consumer))


def test_duplicate_yaml_mapping_keys_fail_closed() -> None:
    workflow = _workflow().replace(
        b"    runs-on: docker-host\n",
        b"    runs-on: ubuntu-latest\n    runs-on: docker-host\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="duplicate YAML mapping key"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "replacement",
    (b"on: [push]", b"on: [pull_request]"),
)
def test_proof_workflow_must_run_on_push_and_pull_request(
    replacement: bytes,
) -> None:
    workflow = _workflow().replace(b"on: [push, pull_request]", replacement, 1)

    with pytest.raises(
        contract.OfficialConsumerContractError, match="push and pull_request"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_proof_job_must_run_on_a_docker_host() -> None:
    workflow = _workflow().replace(
        b"    runs-on: docker-host\n", b"    runs-on: ubuntu-latest\n", 1
    )

    with pytest.raises(contract.OfficialConsumerContractError, match="docker-host"):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_t4_proof_job_name_is_semantically_required() -> None:
    workflow = _workflow().replace(b"  t4-conformance:\n", b"  decoy-conformance:\n", 1)

    with pytest.raises(
        contract.OfficialConsumerContractError, match="t4-conformance proof job"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_t4_proof_must_reach_a_downstream_required_aggregate() -> None:
    workflow = _workflow().replace(
        b"    needs: [t4-conformance]\n",
        b"    needs: [validate-static]\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="downstream aggregate"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_t4_dependency_graph_cannot_hide_the_proof_in_a_cycle() -> None:
    workflow = _workflow().replace(
        b"    needs: validate-static\n",
        b"    needs: [validate-static, validate]\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="dependency cycle"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_always_aggregate_must_enforce_the_t4_result() -> None:
    workflow = _workflow().replace(
        b'      - run: test "${{ needs.t4-conformance.result }}" = success\n',
        b"      - run: echo aggregate-without-enforcement\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="does not enforce"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_t4_runner_labels_cannot_make_the_proof_unschedulable() -> None:
    workflow = _workflow().replace(
        b"    runs-on: docker-host\n",
        b"    runs-on: [docker-host, never-scheduled]\n",
        1,
    )

    with pytest.raises(contract.OfficialConsumerContractError, match="runner labels"):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "replacement",
    (
        "github.event.pull_request.head.repo.fork == true",
        "github.event_name == 'push'",
        "github.event.pull_request.head.repo.fork != true || true",
    ),
)
def test_proof_job_accepts_only_exact_non_fork_guards(replacement: str) -> None:
    workflow = _workflow().replace(
        b"github.event.pull_request.head.repo.fork != true",
        replacement.encode(),
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="unsupported fork guard"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_unguarded_proof_job_fails_closed() -> None:
    workflow = _workflow().replace(
        b"    if: github.event.pull_request.head.repo.fork != true\n", b"", 1
    )

    with pytest.raises(contract.OfficialConsumerContractError, match="non-fork guard"):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    ("needle", "replacement"),
    (
        (
            b"    runs-on: docker-host\n",
            b"    runs-on: docker-host\n    continue-on-error: true\n",
        ),
        (
            b"      - name: Prove management MCP in the final image\n",
            b"      - name: Prove management MCP in the final image\n"
            b"        continue-on-error: true\n",
        ),
        (
            b"  validate:\n    runs-on: ubuntu-latest\n",
            b"  validate:\n    runs-on: ubuntu-latest\n    continue-on-error: true\n",
        ),
    ),
)
def test_proof_and_dependency_chain_cannot_mask_errors(
    needle: bytes, replacement: bytes
) -> None:
    workflow = _workflow().replace(needle, replacement, 1)

    with pytest.raises(
        contract.OfficialConsumerContractError, match="continue-on-error"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    ("needle", "replacement"),
    (
        (
            b"jobs:\n",
            b"defaults:\n  run:\n    shell: bash {0} || true\njobs:\n",
        ),
        (
            b"    env:\n      MOLECULE_CI_REF:",
            b"    defaults:\n      run:\n        shell: bash {0} || true\n"
            b"    env:\n      MOLECULE_CI_REF:",
        ),
        (
            b"      - name: Prove management MCP in the final image\n",
            b"      - name: Prove management MCP in the final image\n"
            b"        shell: bash {0} || true\n",
        ),
    ),
)
def test_custom_shell_wrappers_cannot_mask_the_proof(
    needle: bytes, replacement: bytes
) -> None:
    workflow = _workflow().replace(needle, replacement, 1)

    with pytest.raises(contract.OfficialConsumerContractError, match="custom shell"):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_required_commands_in_comments_do_not_count_as_execution() -> None:
    command = (
        b'          docker cp "$CI_ROOT/scripts/mcp_built_image_e2e.py" \\\n'
        b'            "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py"\n'
    )
    workflow = _workflow().replace(
        command,
        b"          # docker cp verifier decoy only\n"
        b"          # docker cp $CI_ROOT/scripts/mcp_built_image_e2e.py "
        b"$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py\n"
        b"          echo verifier-not-copied\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="ordered executable proof",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    (
        (b"          cat <<'DECOY'\n", b"          DECOY\n"),
        (b"          unused_copy() {\n", b"          }\n"),
        (b"          if false; then\n", b"          fi\n"),
    ),
)
def test_required_commands_in_inert_shell_regions_do_not_count(
    prefix: bytes, suffix: bytes
) -> None:
    command = (
        b'          docker cp "$CI_ROOT/scripts/mcp_built_image_e2e.py" \\\n'
        b'            "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py"\n'
    )
    workflow = _workflow().replace(command, prefix + command + suffix, 1)

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="ordered executable proof",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_required_command_in_multiline_inert_control_flow_does_not_count() -> None:
    command = (
        b'          docker cp "$CI_ROOT/scripts/mcp_built_image_e2e.py" \\\n'
        b'            "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py"\n'
    )
    workflow = _workflow().replace(
        command,
        b"          if false\n          then\n" + command + b"          fi\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="ordered executable proof",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_every_split_proof_stage_must_run_under_the_non_fork_guard() -> None:
    command = (
        b'          docker cp "$CI_ROOT/scripts/mcp_built_image_e2e.py" \\\n'
        b'            "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py"\n'
    )
    replacement = (
        b"      - name: Fork-only verifier copy\n"
        b"        if: github.event.pull_request.head.repo.fork == true\n"
        b"        run: |\n"
        b"          set -euo pipefail\n"
        + command
        + b"      - name: Continue final-image proof\n"
        b"        run: |\n"
        b"          set -euo pipefail\n"
    )
    workflow = _workflow().replace(command, replacement, 1)

    with pytest.raises(contract.OfficialConsumerContractError, match="non-fork guard"):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_final_image_proof_commands_must_be_ordered() -> None:
    copy_block = (
        b'          docker cp "$CI_ROOT/scripts/mcp_built_image_e2e.py" \\\n'
        b'            "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py"\n'
    )
    start_block = (
        b'          docker start --attach --interactive "$MCP_VERIFY_CONTAINER" \\\n'
        b'            < "$MCP_ATTESTATION"\n'
    )
    workflow = _workflow().replace(
        copy_block + start_block, start_block + copy_block, 1
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="ordered executable proof"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_exact_ref_fetch_cannot_be_masked_inside_an_if_condition() -> None:
    fetch = (
        b"          GIT_ASKPASS=/bin/false GIT_TERMINAL_PROMPT=0 \\\n"
        b"            git -c credential.helper= -c http.userAgent=curl/8.4.0 \\\n"
        b'            -C "$CI_ROOT" fetch --no-tags --depth 1 origin "$MOLECULE_CI_REF"\n'
    )
    masked = (
        b"          if GIT_ASKPASS=/bin/false GIT_TERMINAL_PROMPT=0 \\\n"
        b"            git -c credential.helper= -c http.userAgent=curl/8.4.0 \\\n"
        b'            -C "$CI_ROOT" fetch --no-tags --depth 1 origin '
        b'"$MOLECULE_CI_REF"; true; then\n'
        b"            :\n"
        b"          fi\n"
    )
    workflow = _workflow().replace(fetch, masked, 1)

    with pytest.raises(
        contract.OfficialConsumerContractError, match="masks|required command"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "replacement",
    (
        b"          if False:\n              load_attestation(stream)\n",
        b"          def decoy():\n              load_attestation(stream)\n",
    ),
)
def test_attestation_loader_call_must_be_reachable(replacement: bytes) -> None:
    workflow = _workflow().replace(
        b"          load_attestation(stream)\n", replacement, 1
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="hardened attestation load"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "mount",
    (
        b"--volume /tmp/verifier:/mcp_built_image_e2e.py:ro",
        b"-v /tmp/verifier:/mcp_built_image_e2e.py:ro",
        b"--mount type=bind,source=/tmp/verifier,target=/mcp_built_image_e2e.py",
    ),
)
def test_final_image_verifier_rejects_every_bind_mount_form(mount: bytes) -> None:
    workflow = _workflow().replace(
        b"            --network none --user 1000:1000 --workdir /tmp \\\n",
        b"            --network none " + mount + b" \\\n"
        b"            --user 1000:1000 --workdir /tmp \\\n",
        1,
    )

    with pytest.raises(contract.OfficialConsumerContractError, match="bind mount"):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "override",
    (
        b"--network host",
        b"--user 0:0",
        b"--cap-add ALL",
        b"--privileged",
        b"--security-opt seccomp=unconfined",
        b"--entrypoint /bin/sh",
        b"--env PYTHONPATH=/host",
        b"--volumes-from privileged-container",
    ),
)
def test_final_image_verifier_rejects_unsafe_or_ambiguous_overrides(
    override: bytes,
) -> None:
    workflow = _workflow().replace(
        b"            /mcp_built_image_e2e.py\n",
        b"            " + override + b" \\\n            /mcp_built_image_e2e.py\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="unsafe or ambiguous|bind mount",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_t4_tag_cannot_be_reassigned_between_build_and_verifier() -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b'          T4_TAG="different:image"\n          docker create --interactive',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="same final image"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_final_image_build_rejects_a_second_tag_override() -> None:
    workflow = _workflow().replace(
        b'            -t "$T4_TAG" .\n',
        b'            -t "$T4_TAG" --tag different:image .\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="same final image"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_final_image_build_rejects_an_alternate_dockerfile() -> None:
    workflow = _workflow().replace(
        b'            -t "$T4_TAG" .\n',
        b'            -t "$T4_TAG" --file /tmp/forged.Dockerfile .\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="unsafe or ambiguous"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "replacement",
    (
        b'docker tag forged:image "$T4_TAG"',
        b'docker build -t "$T4_TAG" /tmp/forged-context',
    ),
)
def test_final_image_tag_cannot_be_replaced_after_the_reviewed_build(
    replacement: bytes,
) -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b"          " + replacement + b"\n          docker create --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="same final image"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_verifier_copy_cannot_chain_an_unreviewed_overwrite() -> None:
    workflow = _workflow().replace(
        b'            "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py"\n',
        b'            "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py"; '
        b'docker cp /tmp/forged.py "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="invalid verifier copy"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_proof_cannot_shadow_required_executables_with_shell_functions() -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n",
        b"          set -euo pipefail\n"
        b"          docker() {\n"
        b"            command true\n"
        b"          }\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="shadows a required executable"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_lockstep_checker_must_come_from_the_reviewed_fetch() -> None:
    workflow = _workflow().replace(
        b'python3 "$CI_ROOT/scripts/mcp_pin_lockstep.py"',
        b"python3 ./scripts/mcp_pin_lockstep.py",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="unreviewed lockstep checker"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_attestation_path_cannot_drift_between_generation_and_start() -> None:
    workflow = _workflow().replace(
        b"          docker start --attach",
        b'          MCP_ATTESTATION="/tmp/forged.json"\n'
        b"          docker start --attach",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="different attestation|ambiguous MCP_ATTESTATION",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_verifier_source_root_cannot_drift_after_the_reviewed_fetch() -> None:
    workflow = _workflow().replace(
        b"          docker cp ",
        b'          CI_ROOT="/tmp/unreviewed"\n          docker cp ',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="unreviewed verifier|ambiguous CI_ROOT",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_verifier_must_run_the_same_final_image_that_was_built() -> None:
    workflow = _workflow().replace(
        b'            --tmpfs /tmp:size=64m --entrypoint python3 "$T4_TAG" \\\n',
        b'            --tmpfs /tmp:size=64m --entrypoint python3 "$OTHER_TAG" \\\n'
        b'          # --entrypoint python3 "$T4_TAG"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="same final image"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "mutation",
    (
        "MOLECULE_CI_REF[0]=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        'names=(MOLECULE_CI_REF); printf -v "${names[0]}" %s bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        "declare -n target=MOLECULE_CI_''REF; target=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "name=MOLECULE_CI_''REF; eval \"$name=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\"",
    ),
)
def test_shell_cannot_mutate_the_immutable_ref_directly_or_indirectly(
    mutation: str,
) -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n",
        b"          set -euo pipefail\n          " + mutation.encode() + b"\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="mutate MOLECULE_CI_REF"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_ref_mutation_text_in_comments_is_inert() -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n",
        b"          set -euo pipefail\n"
        b"          # MOLECULE_CI_REF[0]=comment-only\n"
        b"          # eval 'MOLECULE_CI_REF=comment-only'\n",
        1,
    )

    contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "collection_bypass",
    (
        "import pytest as p\nmarker = p.mark.skip\nglobals()['pytestmark'] = marker\n",
        "def disable_test(fn):\n    return __import__('pytest').mark.skip(fn)\n"
        "globals()['test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image'] = "
        "disable_test(test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image)\n",
    ),
)
def test_pytest_collection_bypass_cannot_mask_a_semantically_invalid_workflow(
    collection_bypass: str,
) -> None:
    workflow = _workflow().replace(b"on: [push, pull_request]", b"on: [push]", 1)
    test_contract = _test_contract("codex") + collection_bypass.encode()

    with pytest.raises(
        contract.OfficialConsumerContractError, match="push and pull_request"
    ):
        contract.validate_contract("codex", workflow, test_contract)


def test_workflow_must_pin_the_exact_immutable_verifier_ref() -> None:
    wrong_ref = "a" * 40

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="dynamic or divergent MOLECULE_CI_REF assignment",
    ):
        contract.validate_contract(
            "codex", _workflow(verifier_ref=wrong_ref), _test_contract("codex")
        )


def test_matching_workflow_ref_assignments_across_scopes_are_accepted() -> None:
    contract.validate_contract(
        "codex",
        _with_workflow_env(_workflow()),
        _test_contract("codex"),
    )


@pytest.mark.parametrize("replacement", ("a" * 40, "${{ github.sha }}"))
def test_divergent_or_dynamic_scoped_workflow_ref_fails_closed(
    replacement: str,
) -> None:
    workflow = _with_workflow_env(
        _workflow(verifier_ref=replacement),
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="dynamic or divergent MOLECULE_CI_REF assignment",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_duplicate_workflow_ref_in_one_env_scope_fails_closed() -> None:
    assignment = f"      MOLECULE_CI_REF: {VERIFIER_REF}\n".encode()
    workflow = _workflow().replace(assignment, assignment * 2, 1)

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="duplicate YAML mapping key",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_shell_reassignment_of_workflow_ref_fails_closed() -> None:
    wrong_ref = "b" * 40
    workflow = _workflow().replace(
        b"          GIT_ASKPASS=/bin/false",
        (
            f"          MOLECULE_CI_REF={wrong_ref}\n          GIT_ASKPASS=/bin/false"
        ).encode(),
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="mutate MOLECULE_CI_REF",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "missing",
    (
        "GIT_ASKPASS=/bin/false",
        "credential.helper=",
        "--network none",
        "--security-opt no-new-privileges",
        "mcp-built-image-e2e:sentinel:executed",
        "KEEP_T4_IMAGE=1",
    ),
)
def test_missing_workflow_safety_contract_fails_closed(missing: str) -> None:
    workflow = _workflow().replace(missing.encode(), b"removed", 1)

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="ordered executable proof|unsafe or ambiguous|exact-ref fetch",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_test_module_must_pin_the_same_immutable_verifier_ref() -> None:
    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="test contract does not pin the reviewed molecule-ci verifier",
    ):
        contract.validate_contract(
            "openclaw",
            _workflow(),
            _test_contract("openclaw", verifier_ref="b" * 40),
        )


def test_required_regression_test_function_must_exist() -> None:
    test_contract = _test_contract("claude-code").replace(
        b"test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image",
        b"test_unrelated",
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="required regression test",
    ):
        contract.validate_contract("claude-code", _workflow(), test_contract)


def test_required_regression_test_must_be_synchronous() -> None:
    test_contract = _test_contract("codex").replace(
        b"def test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image():",
        b"async def test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image():",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="plain synchronous function",
    ):
        contract.validate_contract("codex", _workflow(), test_contract)


def test_unknown_consumer_fails_closed() -> None:
    with pytest.raises(
        contract.OfficialConsumerContractError, match="unsupported official consumer"
    ):
        contract.validate_contract("unknown", _workflow(), _test_contract("codex"))


@pytest.mark.parametrize(
    ("workflow", "test_source", "message"),
    (
        (b"x" * (512 * 1024 + 1), _test_contract("codex"), "exceeds the size limit"),
        (_workflow(), b"\xffcredential=must-not-log", "is not UTF-8"),
    ),
)
def test_contract_inputs_are_bounded_without_echoing_contents(
    workflow: bytes, test_source: bytes, message: str
) -> None:
    with pytest.raises(contract.OfficialConsumerContractError, match=message) as error:
        contract.validate_contract("codex", workflow, test_source)

    assert "credential=must-not-log" not in str(error.value)


def test_cli_validates_only_the_two_static_contract_files(tmp_path: Path) -> None:
    repo = tmp_path / "consumer"
    workflow = repo / ".gitea" / "workflows" / "ci.yml"
    tests = repo / "tests"
    workflow.parent.mkdir(parents=True)
    tests.mkdir()
    workflow.write_bytes(_workflow())
    tests.joinpath("test_ci_runtime_image_pin.py").write_bytes(_test_contract("codex"))

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--consumer",
            "codex",
            "--repo-root",
            str(repo),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "official-consumer-contract:sentinel:executed codex\n"
    assert result.stderr == ""


def test_cli_rejects_a_symlinked_contract_blob(tmp_path: Path) -> None:
    repo = tmp_path / "consumer"
    workflow = repo / ".gitea" / "workflows" / "ci.yml"
    tests = repo / "tests"
    workflow.parent.mkdir(parents=True)
    tests.mkdir()
    outside = tmp_path / "outside.yml"
    outside.write_bytes(_workflow())
    workflow.symlink_to(outside)
    tests.joinpath("test_ci_runtime_image_pin.py").write_bytes(_test_contract("codex"))

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--consumer",
            "codex",
            "--repo-root",
            str(repo),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "required contract file is not regular" in result.stderr
    assert result.stdout == ""
