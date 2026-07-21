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
jobs:
  t4-conformance:
    env:
      MOLECULE_CI_REF: {verifier_ref}
    steps:
      - run: |
          GIT_ASKPASS=/bin/false GIT_TERMINAL_PROMPT=0 git init "$CI_ROOT"
          git -C "$CI_ROOT" remote add origin \
            https://git.moleculesai.app/molecule-ai/molecule-ci.git
          git -c credential.helper= -c http.userAgent=curl/8.4.0 \\
            -C "$CI_ROOT" fetch --no-tags --depth 1 origin "$MOLECULE_CI_REF"
          git -C "$CI_ROOT" checkout -q --detach FETCH_HEAD
          test "$(git -C "$CI_ROOT" rev-parse HEAD)" = "$MOLECULE_CI_REF"
          python3 "$CI_ROOT/scripts/mcp_pin_lockstep.py" --repo-root . --json
          from mcp_built_image_e2e import load_attestation
          docker build -t "$T4_TAG" .
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


def test_workflow_must_pin_the_exact_immutable_verifier_ref() -> None:
    wrong_ref = "a" * 40

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="workflow does not pin the reviewed molecule-ci verifier",
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
        match="overwrites MOLECULE_CI_REF within one env scope",
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
        match="reassigns MOLECULE_CI_REF inside a run script",
    ):
        contract.validate_contract("codex", workflow, _test_contract("codex"))


def test_final_image_fetch_and_attestation_must_share_one_pinned_run_block() -> None:
    workflow = _workflow().replace(
        b'          python3 "$CI_ROOT/scripts/mcp_pin_lockstep.py" '
        b"--repo-root . --json\n",
        b"          echo verifier-separated\n"
        b"      - run: |\n"
        b'          python3 "$CI_ROOT/scripts/mcp_pin_lockstep.py" '
        b"--repo-root . --json\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="final-image fetch does not use the reviewed MOLECULE_CI_REF",
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
        contract.OfficialConsumerContractError, match="workflow contract is missing"
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
        match="required regression test is not guaranteed to execute",
    ):
        contract.validate_contract("codex", _workflow(), test_contract)


def test_required_regression_test_cannot_be_rebound_after_definition() -> None:
    test_name = "test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image"
    test_contract = _test_contract("codex") + f"\n{test_name} = lambda: None\n".encode()

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="required regression test is not guaranteed to execute",
    ):
        contract.validate_contract("codex", _workflow(), test_contract)


@pytest.mark.parametrize(
    "mutation",
    (
        ".__test__ = False",
        ".pytestmark = __import__('pytest').mark.skip",
    ),
)
def test_required_regression_test_cannot_disable_pytest_collection(
    mutation: str,
) -> None:
    test_name = "test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image"
    test_contract = _test_contract("codex") + f"\n{test_name}{mutation}\n".encode()

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="required regression test is not guaranteed to execute",
    ):
        contract.validate_contract("codex", _workflow(), test_contract)


def test_required_regression_test_cannot_skip_before_assertions() -> None:
    test_contract = _test_contract("codex").replace(
        b"    workflow = yaml.safe_load(CI_WORKFLOW.read_text())\n",
        b'    __import__("pytest").skip("disabled")\n'
        b"    workflow = yaml.safe_load(CI_WORKFLOW.read_text())\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="required regression test is not guaranteed to execute",
    ):
        contract.validate_contract("codex", _workflow(), test_contract)


def test_workflow_helper_cannot_skip_the_required_regression_test() -> None:
    helper = b"""\
def _load_workflow():
    __import__("pytest").skip("disabled")
    return yaml.safe_load(CI_WORKFLOW.read_text())

"""
    test_contract = (
        _test_contract("codex")
        .replace(
            b"yaml.safe_load(CI_WORKFLOW.read_text())",
            b"_load_workflow()",
            1,
        )
        .replace(
            b"def test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image():",
            helper
            + b"def test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image():",
            1,
        )
    )

    with pytest.raises(contract.OfficialConsumerContractError):
        contract.validate_contract("codex", _workflow(), test_contract)


def test_required_regression_test_must_assert_the_hardening_contract() -> None:
    test_contract = _test_contract("hermes").replace(
        b"--pids-limit 128 --memory 768m --cpus 1", b"removed", 1
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="test contract is missing",
    ):
        contract.validate_contract("hermes", _workflow(), test_contract)


def test_dead_fragment_tuple_and_truthiness_assertion_fail_closed() -> None:
    test_contract = _test_contract("codex").replace(
        b"    for fragment in required_fragments:\n        assert fragment in build\n",
        b"    assert required_fragments\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="test contract is missing required final-image MCP assertions",
    ):
        contract.validate_contract("codex", _workflow(), test_contract)


def test_fragment_assertions_must_target_parsed_workflow_data() -> None:
    test_contract = _test_contract("codex").replace(
        b"    for fragment in required_fragments:\n        assert fragment in build\n",
        b'    decoy = " ".join(required_fragments)\n'
        b"    for fragment in required_fragments:\n"
        b"        assert fragment in decoy\n",
        1,
    )

    with pytest.raises(
        contract.OfficialConsumerContractError,
        match="test contract is missing required final-image MCP assertions",
    ):
        contract.validate_contract("codex", _workflow(), test_contract)


def test_post_derivation_overwrite_with_decoy_fails_closed() -> None:
    test_contract = _test_contract("codex").replace(
        b"    assert workflow_ref == MOLECULE_CI_REF\n",
        b'    build = " ".join(required_fragments) + " " + FORK_RUN\n'
        b"    assert workflow_ref == MOLECULE_CI_REF\n",
        1,
    )

    with pytest.raises(contract.OfficialConsumerContractError):
        contract.validate_contract("codex", _workflow(), test_contract)


def test_assertions_under_if_false_fail_closed() -> None:
    enforced = (
        b"    assert workflow_ref == MOLECULE_CI_REF\n"
        b"    assert FORK_RUN in build\n"
        b"    for fragment in required_fragments:\n"
        b"        assert fragment in build\n"
        b'    assert "--volume" not in build\n'
    )
    unreachable = (
        b"    if False:\n"
        b"        assert workflow_ref == MOLECULE_CI_REF\n"
        b"        assert FORK_RUN in build\n"
        b"        for fragment in required_fragments:\n"
        b"            assert fragment in build\n"
        b'        assert "--volume" not in build\n'
    )
    test_contract = _test_contract("codex").replace(enforced, unreachable, 1)

    with pytest.raises(contract.OfficialConsumerContractError):
        contract.validate_contract("codex", _workflow(), test_contract)


def test_reassigned_fragment_sequence_terminates_and_fails_closed(
    tmp_path: Path,
) -> None:
    test_contract = _test_contract("codex").replace(
        b"    assert workflow_ref == MOLECULE_CI_REF\n",
        b'    required_fragments = ("replacement",)\n'
        b"    assert workflow_ref == MOLECULE_CI_REF\n",
        1,
    )
    repo = tmp_path / "consumer"
    workflow = repo / ".gitea" / "workflows" / "ci.yml"
    tests = repo / "tests"
    workflow.parent.mkdir(parents=True)
    tests.mkdir()
    workflow.write_bytes(_workflow())
    tests.joinpath("test_ci_runtime_image_pin.py").write_bytes(test_contract)

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
        timeout=5,
    )

    assert result.returncode == 1
    assert (
        "test contract is missing required final-image MCP assertions" in result.stderr
    )
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("original", "replacement", "message"),
    (
        (
            b"yaml.safe_load(CI_WORKFLOW.read_text())",
            b"{}",
            "does not statically read the CI workflow",
        ),
        (
            b"assert workflow_ref == MOLECULE_CI_REF",
            b"assert MOLECULE_CI_REF",
            "does not compare the immutable verifier ref",
        ),
        (
            b"assert FORK_RUN in build",
            b"assert FORK_RUN",
            "does not assert the non-fork guard",
        ),
        (
            b'assert "--volume" not in build',
            b'assert "--volume"',
            "does not reject verifier bind mounts",
        ),
    ),
)
def test_decoy_literals_do_not_satisfy_the_regression_contract(
    original: bytes, replacement: bytes, message: str
) -> None:
    test_contract = _test_contract("codex").replace(original, replacement, 1)

    with pytest.raises(contract.OfficialConsumerContractError, match=message):
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
