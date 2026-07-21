#!/usr/bin/env python3
"""Fail-closed static contract for official final-image MCP CI wiring.

The meta-CI archive job supplies two blobs from an exact consumer commit. This
checker treats both blobs as data: it never imports or executes consumer code.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
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
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "safe_load"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "yaml"
        and len(node.args) == 1
        and not node.keywords
    ):
        return False
    source = node.args[0]
    return (
        isinstance(source, ast.Call)
        and isinstance(source.func, ast.Attribute)
        and source.func.attr == "read_text"
        and isinstance(source.func.value, ast.Name)
        and source.func.value.id == "CI_WORKFLOW"
        and not source.args
        and not source.keywords
    )


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        return {name for element in target.elts for name in _target_names(element)}
    return set()


def _is_workflow_derived(
    expression: ast.AST,
    derived_names: set[str],
    derived_functions: set[str],
    local_derived: frozenset[str] = frozenset(),
) -> bool:
    """Prove that an expression preserves parsed workflow data.

    This intentionally supports only the selection and collection shapes used
    by the official tests. Unknown transformations fail closed rather than
    letting a literal decoy inherit provenance from an unrelated child node.
    """

    if _is_workflow_load(expression):
        return True
    if isinstance(expression, ast.Name):
        return expression.id in derived_names or expression.id in local_derived
    if isinstance(expression, ast.Subscript):
        if (
            isinstance(expression.value, (ast.List, ast.Tuple))
            and isinstance(expression.slice, ast.Constant)
            and isinstance(expression.slice.value, int)
        ):
            elements = expression.value.elts
            index = expression.slice.value
            if -len(elements) <= index < len(elements):
                return _is_workflow_derived(
                    elements[index], derived_names, derived_functions, local_derived
                )
            return False
        return _is_workflow_derived(
            expression.value, derived_names, derived_functions, local_derived
        )
    if isinstance(expression, ast.Attribute):
        return _is_workflow_derived(
            expression.value, derived_names, derived_functions, local_derived
        )
    if isinstance(expression, (ast.List, ast.Set, ast.Tuple)):
        return bool(expression.elts) and all(
            _is_workflow_derived(
                element, derived_names, derived_functions, local_derived
            )
            for element in expression.elts
        )
    if isinstance(expression, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        scope = set(local_derived)
        has_derived_source = False
        for generator in expression.generators:
            if _is_workflow_derived(
                generator.iter, derived_names, derived_functions, frozenset(scope)
            ):
                scope.update(_target_names(generator.target))
                has_derived_source = True
        return has_derived_source and _is_workflow_derived(
            expression.elt, derived_names, derived_functions, frozenset(scope)
        )
    if isinstance(expression, ast.Call):
        if isinstance(expression.func, ast.Name):
            if expression.func.id in derived_functions:
                return True
            if expression.func.id in {
                "enumerate",
                "list",
                "next",
                "set",
                "sorted",
                "str",
                "tuple",
            }:
                if (
                    not expression.args
                    or expression.keywords
                    or (expression.func.id != "enumerate" and len(expression.args) != 1)
                    or len(expression.args) > 2
                ):
                    return False
                return _is_workflow_derived(
                    expression.args[0],
                    derived_names,
                    derived_functions,
                    local_derived,
                )
            return False
        if isinstance(expression.func, ast.Attribute):
            receiver_is_derived = _is_workflow_derived(
                expression.func.value,
                derived_names,
                derived_functions,
                local_derived,
            )
            if expression.func.attr == "get":
                if (
                    not receiver_is_derived
                    or expression.keywords
                    or len(expression.args) not in (1, 2)
                ):
                    return False
                if len(expression.args) == 2:
                    default = expression.args[1]
                    if not (
                        isinstance(default, ast.Constant)
                        and default.value in (None, "")
                    ):
                        return False
                return True
            if expression.func.attr in {"copy", "items", "keys", "values"}:
                return (
                    receiver_is_derived
                    and not expression.args
                    and not expression.keywords
                )
        return False
    if isinstance(expression, ast.UnaryOp):
        return _is_workflow_derived(
            expression.operand, derived_names, derived_functions, local_derived
        )
    if isinstance(expression, ast.BoolOp):
        return bool(expression.values) and all(
            _is_workflow_derived(value, derived_names, derived_functions, local_derived)
            for value in expression.values
        )
    if isinstance(expression, ast.IfExp):
        return all(
            _is_workflow_derived(part, derived_names, derived_functions, local_derived)
            for part in (expression.body, expression.orelse)
        )
    return False


def _update_assignment_state(
    targets: list[ast.AST],
    value: ast.AST,
    derived_names: set[str],
    string_bindings: dict[str, tuple[str, ...]],
    derived_functions: set[str],
) -> None:
    assigned = {name for target in targets for name in _target_names(target)}
    value_is_derived = _is_workflow_derived(value, derived_names, derived_functions)
    sequence = _static_string_sequence(value, string_bindings)
    for name in assigned:
        if value_is_derived:
            derived_names.add(name)
        else:
            derived_names.discard(name)
        if sequence is None:
            string_bindings.pop(name, None)
        else:
            string_bindings[name] = sequence


def _function_returns_workflow(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    derived_functions: set[str],
) -> bool:
    returns = [
        node for node in _function_nodes(function) if isinstance(node, ast.Return)
    ]
    if (
        len(returns) != 1
        or not function.body
        or returns[0] is not function.body[-1]
        or returns[0].value is None
    ):
        return False

    derived_names: set[str] = set()
    string_bindings: dict[str, tuple[str, ...]] = {}
    for statement in function.body[:-1]:
        if isinstance(statement, ast.Assign):
            _update_assignment_state(
                statement.targets,
                statement.value,
                derived_names,
                string_bindings,
                derived_functions,
            )
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            _update_assignment_state(
                [statement.target],
                statement.value,
                derived_names,
                string_bindings,
                derived_functions,
            )
        elif not isinstance(statement, (ast.Assert, ast.Expr, ast.Pass)):
            for name in _stored_names(statement):
                derived_names.discard(name)
                string_bindings.pop(name, None)

    return _is_workflow_derived(returns[0].value, derived_names, derived_functions)


def _workflow_returning_functions(
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> set[str]:
    derived_functions: set[str] = set()
    # Function provenance is monotonic and can grow at most once per function.
    for _ in range(len(functions) + 1):
        previous = set(derived_functions)
        for name, function in functions.items():
            if _function_returns_workflow(function, derived_functions):
                derived_functions.add(name)
        if derived_functions == previous:
            break
    return derived_functions


def _comparison_parts(
    comparison: ast.Compare,
) -> list[tuple[ast.AST, ast.cmpop, ast.AST]]:
    operands = [comparison.left, *comparison.comparators]
    return [
        (operands[index], operator, operands[index + 1])
        for index, operator in enumerate(comparison.ops)
    ]


def _is_name(expression: ast.AST, name: str) -> bool:
    return isinstance(expression, ast.Name) and expression.id == name


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
        if _is_name(left, name) and _is_workflow_derived(
            right, derived_names, derived_functions
        ):
            return True
        if _is_name(right, name) and _is_workflow_derived(
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


def _stored_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    }


def _has_workflow_source(expression: ast.AST, derived_functions: set[str]) -> bool:
    return any(
        _is_workflow_load(node)
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in derived_functions
        )
        for node in ast.walk(expression)
    )


@dataclass
class _FunctionEvidence:
    saw_workflow_load: bool = False
    asserted_ref: bool = False
    asserted_fork: bool = False
    asserted_no_volume: bool = False
    asserted_fragments: list[str] = field(default_factory=list)


def _record_assertion(
    evidence: _FunctionEvidence,
    assertion: ast.Assert,
    derived_names: set[str],
    derived_functions: set[str],
) -> None:
    if not isinstance(assertion.test, ast.Compare):
        return
    if _asserts_name_against_workflow(
        assertion,
        "MOLECULE_CI_REF",
        (ast.Eq,),
        derived_names,
        derived_functions,
    ):
        evidence.asserted_ref = True
    if _asserts_name_against_workflow(
        assertion,
        "FORK_RUN",
        (ast.Eq, ast.In),
        derived_names,
        derived_functions,
    ):
        evidence.asserted_fork = True
    for left, operator, right in _comparison_parts(assertion.test):
        if (
            isinstance(operator, ast.NotIn)
            and _static_string(left) == "--volume"
            and _is_workflow_derived(right, derived_names, derived_functions)
        ):
            evidence.asserted_no_volume = True
        value = _static_string(left)
        if (
            isinstance(operator, ast.In)
            and value is not None
            and _is_workflow_derived(right, derived_names, derived_functions)
        ):
            evidence.asserted_fragments.append(value)


def _record_static_loop_assertions(
    evidence: _FunctionEvidence,
    loop: ast.For | ast.AsyncFor,
    string_bindings: dict[str, tuple[str, ...]],
    derived_names: set[str],
    derived_functions: set[str],
) -> None:
    target_names = _target_names(loop.target)
    sequence = _static_string_sequence(loop.iter, string_bindings)
    if (
        len(target_names) != 1
        or not sequence
        or loop.orelse
        or not loop.body
        or not all(isinstance(statement, ast.Assert) for statement in loop.body)
    ):
        return
    target_name = next(iter(target_names))
    for assertion in loop.body:
        _record_assertion(evidence, assertion, derived_names, derived_functions)
        if not isinstance(assertion.test, ast.Compare):
            continue
        if any(
            isinstance(operator, ast.In)
            and _is_name(left, target_name)
            and _is_workflow_derived(right, derived_names, derived_functions)
            for left, operator, right in _comparison_parts(assertion.test)
        ):
            evidence.asserted_fragments.extend(sequence)


def _function_evidence(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    derived_functions: set[str],
) -> _FunctionEvidence:
    """Collect guaranteed evidence in source order from one regression test.

    Only direct top-level assertions and non-empty static loops whose bodies
    contain assertions exclusively are guaranteed. Conditional/nested evidence
    is ignored, and any assignment replaces the prior provenance for its name.
    """

    evidence = _FunctionEvidence()
    derived_names: set[str] = set()
    string_bindings: dict[str, tuple[str, ...]] = {}

    for statement in function.body:
        if isinstance(statement, ast.Assign):
            evidence.saw_workflow_load |= _has_workflow_source(
                statement.value, derived_functions
            )
            _update_assignment_state(
                statement.targets,
                statement.value,
                derived_names,
                string_bindings,
                derived_functions,
            )
            continue
        if isinstance(statement, ast.AnnAssign) and statement.value is not None:
            evidence.saw_workflow_load |= _has_workflow_source(
                statement.value, derived_functions
            )
            _update_assignment_state(
                [statement.target],
                statement.value,
                derived_names,
                string_bindings,
                derived_functions,
            )
            continue
        if isinstance(statement, ast.Assert):
            _record_assertion(evidence, statement, derived_names, derived_functions)
            continue
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            _record_static_loop_assertions(
                evidence,
                statement,
                string_bindings,
                derived_names,
                derived_functions,
            )
        if isinstance(statement, (ast.Return, ast.Raise)):
            break
        for name in _stored_names(statement):
            derived_names.discard(name)
            string_bindings.pop(name, None)

    return evidence


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
    if any(function.decorator_list for function in required_functions):
        raise OfficialConsumerContractError(
            "required regression test is not guaranteed to execute"
        )
    if any(
        not any(isinstance(node, ast.Assert) for node in ast.walk(function))
        for function in required_functions
    ):
        raise OfficialConsumerContractError(
            "required regression test contains no assertions"
        )

    evidence = [
        _function_evidence(function, derived_functions)
        for function in required_functions
    ]
    if not any(item.saw_workflow_load for item in evidence):
        raise OfficialConsumerContractError(
            "required regression test does not statically read the CI workflow"
        )

    if not any(item.asserted_ref for item in evidence):
        raise OfficialConsumerContractError(
            "required regression test does not compare the immutable verifier ref"
        )
    if not any(item.asserted_fork for item in evidence):
        raise OfficialConsumerContractError(
            "required regression test does not assert the non-fork guard"
        )
    if not any(item.asserted_no_volume for item in evidence):
        raise OfficialConsumerContractError(
            "required regression test does not reject verifier bind mounts"
        )

    _require_fragments(
        " ".join(fragment for item in evidence for fragment in item.asserted_fragments),
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
