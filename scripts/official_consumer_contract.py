#!/usr/bin/env python3
"""Fail-closed static contract for official final-image MCP CI wiring.

The meta-CI archive job supplies two blobs from an exact consumer commit. This
checker treats both blobs as data: it never imports or executes consumer code.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import stat
import sys


FINAL_IMAGE_VERIFIER_REF = "".join(("11b8598e5c0b3f0b1031733a8d5f6bc", "238f146a4"))
SENTINEL = "official-consumer-contract:sentinel:executed"
_MAX_CONTRACT_BYTES = 512 * 1024
_WORKFLOW_PATH = Path(".gitea/workflows/ci.yml")
_TEST_PATH = Path("tests/test_ci_runtime_image_pin.py")
_REF_ASSIGNMENT_RE = re.compile(
    r"(?m)^\s*MOLECULE_CI_REF:\s*([0-9a-f]{40})\s*(?:#.*)?$"
)
_REQUIRED_TESTS = {
    "claude-code": (
        "test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image",
    ),
    "codex": ("test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image",),
    "openclaw": (
        "test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image",
    ),
    "hermes": (
        "test_t4_fetches_exact_molecule_ci_and_generates_attestation",
        "test_t4_runs_hardened_final_image_mcp_e2e_before_privileged_probe",
    ),
}
_WORKFLOW_FRAGMENTS = (
    "https://git.moleculesai.app/molecule-ai/molecule-ci.git",
    "GIT_ASKPASS=/bin/false",
    "GIT_TERMINAL_PROMPT=0",
    "credential.helper=",
    "http.userAgent=curl/8.4.0",
    "fetch",
    "--no-tags",
    "--depth 1",
    'origin "$MOLECULE_CI_REF"',
    "rev-parse HEAD",
    "mcp_pin_lockstep.py",
    "--repo-root . --json",
    "mcp_built_image_e2e.py",
    "load_attestation",
    "docker build",
    '-t "$T4_TAG"',
    "docker create --interactive --name",
    "--network none",
    "--user 1000:1000 --workdir /tmp",
    "--cap-drop ALL --security-opt no-new-privileges",
    "--pids-limit 128 --memory 768m --cpus 1",
    "--tmpfs /tmp:size=64m",
    '--entrypoint python3 "$T4_TAG"',
    "/mcp_built_image_e2e.py",
    "docker cp",
    "docker start --attach --interactive",
    "grep -qxF 'mcp-built-image-e2e:sentinel:executed'",
    "KEEP_T4_IMAGE=1",
)
_TEST_FRAGMENTS = (
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


class OfficialConsumerContractError(Exception):
    """A static official-consumer CI contract violation."""


def _decode_contract(payload: bytes, label: str) -> str:
    if len(payload) > _MAX_CONTRACT_BYTES:
        raise OfficialConsumerContractError(f"{label} exceeds the size limit")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OfficialConsumerContractError(f"{label} is not UTF-8") from exc


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _require_fragments(text: str, fragments: tuple[str, ...], label: str) -> None:
    normalized = _normalized(text)
    missing = [fragment for fragment in fragments if fragment not in normalized]
    if missing:
        raise OfficialConsumerContractError(
            f"{label} contract is missing required final-image MCP assertions"
        )


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and isinstance(node.func.value, ast.Constant)
        and node.func.value.value == ""
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], (ast.List, ast.Tuple))
    ):
        return None
    parts = [_static_string(element) for element in node.args[0].elts]
    if any(part is None for part in parts):
        return None
    return "".join(part for part in parts if part is not None)


def _test_ref(module: ast.Module) -> str | None:
    assignments: list[ast.AST] = []
    for statement in module.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if any(
                isinstance(target, ast.Name) and target.id == "MOLECULE_CI_REF"
                for target in targets
            ):
                assignments.append(statement.value)
    if len(assignments) != 1:
        return None
    return _static_string(assignments[0])


def _validate_workflow(workflow: str) -> None:
    refs = _REF_ASSIGNMENT_RE.findall(workflow)
    if len(refs) != 1:
        raise OfficialConsumerContractError(
            "workflow must contain exactly one MOLECULE_CI_REF assignment"
        )
    if refs[0] != FINAL_IMAGE_VERIFIER_REF:
        raise OfficialConsumerContractError(
            "workflow does not pin the reviewed molecule-ci verifier"
        )
    _require_fragments(workflow, _WORKFLOW_FRAGMENTS, "workflow")


def _validate_test_contract(consumer: str, test_source: str) -> None:
    try:
        module = ast.parse(test_source, filename=str(_TEST_PATH))
    except SyntaxError as exc:
        raise OfficialConsumerContractError(
            "test contract is not valid Python"
        ) from exc

    if _test_ref(module) != FINAL_IMAGE_VERIFIER_REF:
        raise OfficialConsumerContractError(
            "test contract does not pin the reviewed molecule-ci verifier"
        )

    functions = {
        node.name: node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required_names = _REQUIRED_TESTS[consumer]
    missing_names = [name for name in required_names if name not in functions]
    if missing_names:
        raise OfficialConsumerContractError(
            "test contract is missing a required regression test"
        )

    required_functions = [functions[name] for name in required_names]
    if any(
        not any(isinstance(node, ast.Assert) for node in ast.walk(function))
        for function in required_functions
    ):
        raise OfficialConsumerContractError(
            "required regression test contains no assertions"
        )

    required_nodes = [
        node for function in required_functions for node in ast.walk(function)
    ]
    workflow_loads = [
        node
        for node in required_nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "safe_load"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "yaml"
        and any(
            isinstance(child, ast.Name) and child.id == "CI_WORKFLOW"
            for child in ast.walk(node)
        )
    ]
    if not workflow_loads:
        raise OfficialConsumerContractError(
            "required regression test does not statically read the CI workflow"
        )

    assertions = [node for node in required_nodes if isinstance(node, ast.Assert)]
    if not any(
        isinstance(assertion.test, ast.Compare)
        and any(isinstance(operator, ast.Eq) for operator in assertion.test.ops)
        and any(
            isinstance(node, ast.Name) and node.id == "MOLECULE_CI_REF"
            for node in ast.walk(assertion.test)
        )
        for assertion in assertions
    ):
        raise OfficialConsumerContractError(
            "required regression test does not compare the immutable verifier ref"
        )
    if not any(
        isinstance(node, ast.Name) and node.id == "FORK_RUN"
        for function in required_functions
        for node in ast.walk(function)
    ):
        raise OfficialConsumerContractError(
            "required regression test does not assert the non-fork guard"
        )
    if not any(
        isinstance(assertion.test, ast.Compare)
        and isinstance(assertion.test.left, ast.Constant)
        and assertion.test.left.value == "--volume"
        and any(isinstance(operator, ast.NotIn) for operator in assertion.test.ops)
        for assertion in assertions
    ):
        raise OfficialConsumerContractError(
            "required regression test does not reject verifier bind mounts"
        )

    literals = " ".join(
        node.value
        for function in required_functions
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    _require_fragments(literals, _TEST_FRAGMENTS, "test")


def validate_contract(
    consumer: str, workflow_payload: bytes, test_payload: bytes
) -> None:
    """Validate one official consumer's workflow and regression test as data."""

    if consumer not in _REQUIRED_TESTS:
        raise OfficialConsumerContractError("unsupported official consumer")
    workflow = _decode_contract(workflow_payload, "workflow")
    test_source = _decode_contract(test_payload, "test contract")
    _validate_workflow(workflow)
    _validate_test_contract(consumer, test_source)


def _read_regular_file(repo_root: Path, relative_path: Path) -> bytes:
    path = repo_root / relative_path
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise OfficialConsumerContractError(
            f"required contract file is missing: {relative_path}"
        ) from exc
    if not stat.S_ISREG(mode):
        raise OfficialConsumerContractError(
            f"required contract file is not regular: {relative_path}"
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise OfficialConsumerContractError(
            f"required contract file is unreadable: {relative_path}"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="validate static final-image MCP wiring in an official consumer"
    )
    parser.add_argument("--consumer", required=True, choices=tuple(_REQUIRED_TESTS))
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        workflow_payload = _read_regular_file(args.repo_root, _WORKFLOW_PATH)
        test_payload = _read_regular_file(args.repo_root, _TEST_PATH)
        validate_contract(args.consumer, workflow_payload, test_payload)
    except OfficialConsumerContractError as exc:
        print(
            f"official consumer contract failed for {args.consumer}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"{SENTINEL} {args.consumer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
