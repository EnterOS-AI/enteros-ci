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


def _function_nodes(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    stack = list(reversed(function.body))
    while stack:
        node = stack.pop()
        nodes.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return nodes


def _is_workflow_load(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "safe_load"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "yaml"
        and any(
            isinstance(child, ast.Name) and child.id == "CI_WORKFLOW"
            for child in ast.walk(node)
        )
    )


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        return {
            name
            for element in target.elts
            for name in _target_names(element)
        }
    return set()


def _is_workflow_derived(
    expression: ast.AST,
    derived_names: set[str],
    derived_functions: set[str],
) -> bool:
    for node in ast.walk(expression):
        if _is_workflow_load(node):
            return True
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in derived_names
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in derived_functions
        ):
            return True
    return False


def _workflow_derived_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    derived_functions: set[str],
) -> set[str]:
    assignments: list[tuple[list[ast.AST], ast.AST]] = []
    for node in _function_nodes(function):
        if isinstance(node, ast.Assign):
            assignments.append((node.targets, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append(([node.target], node.value))

    derived_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for targets, value in assignments:
            if not _is_workflow_derived(value, derived_names, derived_functions):
                continue
            assigned = {
                name for target in targets for name in _target_names(target)
            }
            if not assigned.issubset(derived_names):
                derived_names.update(assigned)
                changed = True
    return derived_names


def _workflow_returning_functions(
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> set[str]:
    derived_functions: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, function in functions.items():
            derived_names = _workflow_derived_names(function, derived_functions)
            if any(
                isinstance(node, ast.Return)
                and node.value is not None
                and _is_workflow_derived(
                    node.value, derived_names, derived_functions
                )
                for node in _function_nodes(function)
            ) and name not in derived_functions:
                derived_functions.add(name)
                changed = True
    return derived_functions


def _comparison_parts(
    comparison: ast.Compare,
) -> list[tuple[ast.AST, ast.cmpop, ast.AST]]:
    operands = [comparison.left, *comparison.comparators]
    return [
        (operands[index], operator, operands[index + 1])
        for index, operator in enumerate(comparison.ops)
    ]


def _contains_name(expression: ast.AST, name: str) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id == name
        for node in ast.walk(expression)
    )


def _asserts_name_against_workflow(
    assertion: ast.Assert,
    name: str,
    operator_types: tuple[type[ast.cmpop], ...],
    derived_names: set[str],
    derived_functions: set[str],
) -> bool:
    if not isinstance(assertion.test, ast.Compare):
        return False
    for left, operator, right in _comparison_parts(assertion.test):
        if not isinstance(operator, operator_types):
            continue
        if _contains_name(left, name) and _is_workflow_derived(
            right, derived_names, derived_functions
        ):
            return True
        if _contains_name(right, name) and _is_workflow_derived(
            left, derived_names, derived_functions
        ):
            return True
    return False


def _static_string_sequence(
    expression: ast.AST, bindings: dict[str, tuple[str, ...]]
) -> tuple[str, ...] | None:
    if isinstance(expression, ast.Name):
        return bindings.get(expression.id)
    if not isinstance(expression, (ast.List, ast.Set, ast.Tuple)):
        return None
    values = tuple(_static_string(element) for element in expression.elts)
    if any(value is None for value in values):
        return None
    return tuple(value for value in values if value is not None)


def _asserted_workflow_fragments(
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef],
    derived_functions: set[str],
) -> str:
    asserted: list[str] = []
    for function in functions:
        nodes = _function_nodes(function)
        derived_names = _workflow_derived_names(function, derived_functions)
        bindings: dict[str, tuple[str, ...]] = {}
        changed = True
        while changed:
            changed = False
            for node in nodes:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if value is None:
                    continue
                sequence = _static_string_sequence(value, bindings)
                if sequence is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in _target_names(target):
                        if bindings.get(name) != sequence:
                            bindings[name] = sequence
                            changed = True

        for node in nodes:
            if isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare):
                for left, operator, right in _comparison_parts(node.test):
                    value = _static_string(left)
                    if (
                        isinstance(operator, ast.In)
                        and value is not None
                        and _is_workflow_derived(
                            right, derived_names, derived_functions
                        )
                    ):
                        asserted.append(value)
            if not isinstance(node, (ast.For, ast.AsyncFor)):
                continue
            target_names = _target_names(node.target)
            if len(target_names) != 1:
                continue
            target_name = next(iter(target_names))
            sequence = _static_string_sequence(node.iter, bindings)
            if sequence is None:
                continue
            loop_assertions = [
                child for child in ast.walk(node) if isinstance(child, ast.Assert)
            ]
            if any(
                isinstance(assertion.test, ast.Compare)
                and any(
                    isinstance(operator, ast.In)
                    and isinstance(left, ast.Name)
                    and left.id == target_name
                    and _is_workflow_derived(
                        right, derived_names, derived_functions
                    )
                    for left, operator, right in _comparison_parts(assertion.test)
                )
                for assertion in loop_assertions
            ):
                asserted.extend(sequence)
    return " ".join(asserted)


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
    derived_functions = _workflow_returning_functions(functions)
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
        node for function in required_functions for node in _function_nodes(function)
    ]
    workflow_loads = [
        node
        for node in required_nodes
        if _is_workflow_load(node)
    ]
    if not workflow_loads:
        raise OfficialConsumerContractError(
            "required regression test does not statically read the CI workflow"
        )

    derived_names = {
        function.name: _workflow_derived_names(function, derived_functions)
        for function in required_functions
    }
    if not any(
        _asserts_name_against_workflow(
            assertion,
            "MOLECULE_CI_REF",
            (ast.Eq,),
            derived_names[function.name],
            derived_functions,
        )
        for function in required_functions
        for assertion in _function_nodes(function)
        if isinstance(assertion, ast.Assert)
    ):
        raise OfficialConsumerContractError(
            "required regression test does not compare the immutable verifier ref"
        )
    if not any(
        _asserts_name_against_workflow(
            assertion,
            "FORK_RUN",
            (ast.Eq, ast.In),
            derived_names[function.name],
            derived_functions,
        )
        for function in required_functions
        for assertion in _function_nodes(function)
        if isinstance(assertion, ast.Assert)
    ):
        raise OfficialConsumerContractError(
            "required regression test does not assert the non-fork guard"
        )
    if not any(
        isinstance(assertion.test, ast.Compare)
        and any(
            isinstance(operator, ast.NotIn)
            and _static_string(left) == "--volume"
            and _is_workflow_derived(
                right, derived_names[function.name], derived_functions
            )
            for left, operator, right in _comparison_parts(assertion.test)
        )
        for function in required_functions
        for assertion in _function_nodes(function)
        if isinstance(assertion, ast.Assert)
    ):
        raise OfficialConsumerContractError(
            "required regression test does not reject verifier bind mounts"
        )

    _require_fragments(
        _asserted_workflow_fragments(required_functions, derived_functions),
        _TEST_FRAGMENTS,
        "test",
    )


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
