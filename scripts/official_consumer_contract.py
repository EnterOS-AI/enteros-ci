#!/usr/bin/env python3
"""Fail-closed semantic contract for official final-image MCP CI wiring.

The meta-CI archive job supplies two bounded blobs from an exact consumer
commit.  Both are treated as inert data.  The workflow is parsed with a strict
YAML loader and is the authoritative proof; the consumer pytest module is only
a repository-local regression marker and is never imported or executed.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import stat
import sys
from typing import Any

import yaml


FINAL_IMAGE_VERIFIER_REF = "".join(("11b8598e5c0b3f0b1031733a8d5f6bc", "238f146a4"))
SENTINEL = "official-consumer-contract:sentinel:executed"
_MAX_CONTRACT_BYTES = 512 * 1024
_WORKFLOW_PATH = Path(".gitea/workflows/ci.yml")
_TEST_PATH = Path("tests/test_ci_runtime_image_pin.py")
_PROOF_JOB = "t4-conformance"
_AGGREGATE_JOBS = frozenset({"validate", "all-required"})
_CHECKOUT_ACTION = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
_T4_TAG_ASSIGNMENT = (
    'T4_TAG="t4-conformance-test:${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"'
)
_NON_FORK_GUARDS = frozenset(
    {
        "github.event.pull_request.head.repo.fork != true",
        (
            "github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.fork == false"
        ),
    }
)
_FORK_GUARDS = frozenset(
    {
        "github.event.pull_request.head.repo.fork == true",
        (
            "github.event_name == 'pull_request' && "
            "github.event.pull_request.head.repo.fork == true"
        ),
    }
)
_FORK_EXPRESSION = (
    "github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.fork == true"
)
_DANGEROUS_ENVIRONMENT_NAMES = frozenset(
    {
        "BASH_ENV",
        "BASHOPTS",
        "CDPATH",
        "CURL_CA_BUNDLE",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "ENV",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "NODE_OPTIONS",
        "NODE_PATH",
        "GITHUB_ENV",
        "GITHUB_PATH",
        "IFS",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SHELLOPTS",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CONFIG_HOME",
    }
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


class OfficialConsumerContractError(Exception):
    """A static official-consumer CI contract violation."""


class _StrictWorkflowLoader(yaml.SafeLoader):
    """SafeLoader variant with YAML-1.2 booleans and duplicate-key rejection."""


# Copy before changing resolvers so importing this checker cannot mutate PyYAML's
# process-global SafeLoader behavior in its own unit-test process.
_StrictWorkflowLoader.yaml_implicit_resolvers = {
    key: list(resolvers)
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _resolver_key, _resolvers in list(
    _StrictWorkflowLoader.yaml_implicit_resolvers.items()
):
    _StrictWorkflowLoader.yaml_implicit_resolvers[_resolver_key] = [
        resolver for resolver in _resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
_StrictWorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def _construct_unique_mapping(
    loader: _StrictWorkflowLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise OfficialConsumerContractError(
                "workflow contains an invalid YAML mapping key"
            ) from exc
        if duplicate:
            raise OfficialConsumerContractError(
                "workflow contains a duplicate YAML mapping key"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictWorkflowLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class _TraceUnit:
    step_index: int
    text: str
    controls: tuple[str, ...]
    heredoc_bodies: tuple[str, ...]


@dataclass(frozen=True)
class _ShellUnit:
    text: str
    controls: tuple[str, ...]
    heredoc_bodies: tuple[str, ...]


def _decode_contract(payload: bytes, label: str) -> str:
    if len(payload) > _MAX_CONTRACT_BYTES:
        raise OfficialConsumerContractError(f"{label} exceeds the size limit")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OfficialConsumerContractError(f"{label} is not UTF-8") from exc


def _strict_workflow_load(workflow: str) -> dict[str, Any]:
    try:
        loaded = yaml.load(workflow, Loader=_StrictWorkflowLoader)
    except OfficialConsumerContractError:
        raise
    except yaml.YAMLError as exc:
        raise OfficialConsumerContractError(
            "workflow is not valid strict YAML"
        ) from exc
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise OfficialConsumerContractError(
            "workflow root must be a string-keyed mapping"
        )
    return loaded


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise OfficialConsumerContractError(f"{label} must be a string-keyed mapping")
    return value


def _steps(job: dict[str, Any], label: str) -> list[dict[str, Any]]:
    value = job.get("steps")
    if not isinstance(value, list) or not value:
        raise OfficialConsumerContractError(f"{label} must define non-empty steps")
    result: list[dict[str, Any]] = []
    for step in value:
        result.append(_mapping(step, f"{label} step"))
    return result


def _needs(value: object, label: str) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str) and value:
        return {value}
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) and item for item in value)
    ):
        return set(value)
    raise OfficialConsumerContractError(f"{label} has invalid needs dependencies")


def _normalize_condition(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return ""
    condition = " ".join(value.strip().split())
    if condition.startswith("${{") and condition.endswith("}}"):
        condition = " ".join(condition[3:-2].strip().split())
    return condition


def _validate_triggers(workflow: dict[str, Any]) -> None:
    trigger = workflow.get("on")
    if isinstance(trigger, list):
        events = (
            set(trigger) if all(isinstance(item, str) for item in trigger) else set()
        )
        configs: dict[str, object] = {}
    elif isinstance(trigger, dict) and all(isinstance(key, str) for key in trigger):
        events = set(trigger)
        configs = trigger
    else:
        events = set()
        configs = {}
    if not {"push", "pull_request"}.issubset(events):
        raise OfficialConsumerContractError(
            "workflow must trigger the proof on push and pull_request"
        )

    for event in ("push", "pull_request"):
        config = configs.get(event)
        if config is None:
            continue
        if not isinstance(config, dict):
            raise OfficialConsumerContractError(
                f"workflow {event} trigger has an unsupported filter"
            )
        forbidden = {"paths", "paths-ignore", "branches-ignore", "tags", "tags-ignore"}
        if forbidden.intersection(config):
            raise OfficialConsumerContractError(
                f"workflow {event} trigger can skip the proof"
            )
        if event == "push" and "branches" in config:
            branches = config["branches"]
            if not (
                isinstance(branches, list)
                and branches
                and all(isinstance(branch, str) for branch in branches)
                and "main" in branches
            ):
                raise OfficialConsumerContractError(
                    "workflow push trigger does not include main"
                )
        if event == "pull_request" and "branches" in config:
            raise OfficialConsumerContractError(
                "workflow pull_request trigger can skip the proof"
            )
        if event == "pull_request" and "types" in config:
            types = config["types"]
            if not (
                isinstance(types, list)
                and all(isinstance(activity, str) for activity in types)
                and {"opened", "synchronize", "reopened"}.issubset(set(types))
            ):
                raise OfficialConsumerContractError(
                    "workflow pull_request trigger omits a required activity"
                )


def _continue_on_error_is_masking(container: dict[str, Any]) -> bool:
    return (
        "continue-on-error" in container and container["continue-on-error"] is not False
    )


def _validate_dependency_graph(jobs: dict[str, Any]) -> set[str]:
    dependencies: dict[str, set[str]] = {}
    for name, raw_job in jobs.items():
        job = _mapping(raw_job, f"workflow job {name}")
        dependencies[name] = _needs(job.get("needs"), f"workflow job {name}")
        unknown = dependencies[name].difference(jobs)
        if unknown:
            raise OfficialConsumerContractError(
                f"workflow job {name} has an unknown dependency"
            )

    complete: set[str] = set()
    active: set[str] = set()

    def visit(name: str) -> None:
        if name in active:
            raise OfficialConsumerContractError(
                "workflow dependency graph contains a dependency cycle"
            )
        if name in complete:
            return
        active.add(name)
        for dependency in dependencies[name]:
            visit(dependency)
        active.remove(name)
        complete.add(name)

    for name in dependencies:
        visit(name)

    reverse: dict[str, set[str]] = {name: set() for name in jobs}
    for name, required in dependencies.items():
        for dependency in required:
            reverse[dependency].add(name)
    reachable = {_PROOF_JOB}
    frontier = [_PROOF_JOB]
    while frontier:
        dependency = frontier.pop()
        for consumer in reverse.get(dependency, set()):
            if consumer not in reachable:
                reachable.add(consumer)
                frontier.append(consumer)

    aggregates = reachable.intersection(_AGGREGATE_JOBS)
    if not aggregates:
        raise OfficialConsumerContractError(
            "t4-conformance proof does not reach a downstream aggregate"
        )
    always_aggregates = {
        name
        for name in aggregates
        if _normalize_condition(_mapping(jobs[name], f"workflow job {name}").get("if"))
        == "always()"
    }
    if not always_aggregates:
        raise OfficialConsumerContractError(
            "t4-conformance proof does not reach an unconditional always aggregate"
        )
    for name in reachable:
        if name == _PROOF_JOB:
            continue
        job = _mapping(jobs[name], f"workflow job {name}")
        condition = _normalize_condition(job.get("if"))
        if condition not in (None, "always()"):
            raise OfficialConsumerContractError(
                "t4-conformance downstream aggregate is conditionally unreachable"
            )
        if _continue_on_error_is_masking(job):
            raise OfficialConsumerContractError(
                "t4-conformance dependency chain uses continue-on-error"
            )
        for step in _steps(job, f"workflow job {name}"):
            if _continue_on_error_is_masking(step):
                raise OfficialConsumerContractError(
                    "t4-conformance dependency chain uses continue-on-error"
                )
        if name in _AGGREGATE_JOBS and condition == "always()":
            _validate_always_aggregate(job, name)
    return reachable


def _validate_always_aggregate(job: dict[str, Any], name: str) -> None:
    steps = _steps(job, f"workflow job {name}")
    try:
        _validate_safe_environment(job, f"workflow {name} always aggregate")
        for step in steps:
            _validate_safe_environment(step, f"workflow {name} always aggregate step")
    except OfficialConsumerContractError as exc:
        raise OfficialConsumerContractError(
            f"workflow {name} always aggregate execution boundary is unsafe"
        ) from exc
    if (
        "defaults" in job
        or "container" in job
        or "services" in job
        or len(steps) != 1
        or "uses" in steps[0]
    ):
        raise OfficialConsumerContractError(
            f"workflow {name} always aggregate execution boundary is unsafe"
        )

    result_expression = "${{ needs.t4-conformance.result }}"
    saw_result = False
    saw_conditional = False
    saw_non_failing_assertion = False
    for step in steps:
        script = step.get("run")
        if not isinstance(script, str):
            continue
        if result_expression not in script:
            continue
        saw_result = True
        if _normalize_condition(step.get("if")) is not None:
            saw_conditional = True
            continue
        if "shell" in step:
            saw_non_failing_assertion = True
            continue
        units = _script_units(script)
        if any(_relaxes_fail_closed_shell_mode(unit.text) for unit in units):
            saw_non_failing_assertion = True
            continue
        assignment_entry = next(
            (
                (index, match)
                for index, unit in enumerate(units)
                if not unit.controls
                and (
                    match := re.fullmatch(
                        rf'(?P<name>[A-Za-z_][A-Za-z0-9_]*)="{re.escape(result_expression)}"',
                        unit.text,
                    )
                )
            ),
            None,
        )
        if assignment_entry is None:
            saw_non_failing_assertion = True
            continue
        assignment_index, assignment = assignment_entry
        variable = assignment.group("name")
        variable_assignments = [
            candidate.text
            for candidate in units
            if re.match(
                rf"^{re.escape(variable)}(?:\[[^]]*\])?\s*(?:\+?=)",
                candidate.text,
            )
        ]
        if (
            variable_assignments != [assignment.group(0)]
            or re.search(rf"\$\{{{re.escape(variable)}(?::?[=+])", script)
            or _contains_state_mutating_shell_command(units)
        ):
            saw_non_failing_assertion = True
            continue
        failure_index = next(
            (
                index
                for index, unit in enumerate(units)
                if re.fullmatch(
                    rf'if \[ "\${variable}" != "success" \]; then',
                    unit.text,
                )
            ),
            None,
        )
        if failure_index is None or assignment_index >= failure_index:
            saw_non_failing_assertion = True
            continue
        failure_condition = units[failure_index].text
        for exit_index, unit in enumerate(
            units[failure_index + 1 :], failure_index + 1
        ):
            if unit.text != "exit 1" or failure_condition not in unit.controls:
                continue
            nested_controls = tuple(
                control for control in unit.controls if control != failure_condition
            )
            if not nested_controls:
                return
            if len(nested_controls) != 1:
                continue
            carveout = nested_controls[0]
            carveout_match = re.fullmatch(
                rf'if \[ "\${variable}" = "skipped" \] && '
                r'\[ "\$(?P<fork>[A-Za-z_][A-Za-z0-9_]*)" = "true" \]; then',
                carveout,
            )
            if carveout_match is None or not any(
                candidate.text == "else"
                and candidate.controls == (failure_condition, carveout)
                for candidate in units[failure_index + 1 : exit_index]
            ):
                continue
            if _aggregate_fork_binding_is_exact(
                job, step, units, failure_index, carveout_match.group("fork")
            ):
                return
        saw_non_failing_assertion = True
    if saw_conditional:
        raise OfficialConsumerContractError(
            f"workflow {name} always aggregate assertion must be unconditional"
        )
    if saw_result and saw_non_failing_assertion:
        raise OfficialConsumerContractError(
            f"workflow {name} always aggregate does not fail closed"
        )
    raise OfficialConsumerContractError(
        f"workflow {name} always aggregate does not enforce t4-conformance success"
    )


def _aggregate_fork_binding_is_exact(
    job: dict[str, Any],
    step: dict[str, Any],
    units: list[_ShellUnit],
    before_index: int,
    variable: str,
) -> bool:
    direct = f'"${{{{ {_FORK_EXPRESSION} }}}}"'
    assignments = [
        (index, match.group("value"))
        for index, unit in enumerate(units)
        if (match := re.fullmatch(rf"{re.escape(variable)}=(?P<value>.+)", unit.text))
        is not None
    ]
    if len(assignments) != 1 or assignments[0][0] >= before_index:
        return False
    value = assignments[0][1]
    if value == direct:
        return True
    if value != '"${IS_FORK_PR:-false}"' or "IS_FORK_PR" in _env(
        step, "aggregate step"
    ):
        return False
    return _normalize_condition(_env(job, "aggregate job").get("IS_FORK_PR")) == (
        _FORK_EXPRESSION
    )


def _strip_shell_comment(line: str) -> str:
    single = False
    double = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and not single:
            escaped = True
            continue
        if character == "'" and not double:
            single = not single
            continue
        if character == '"' and not single:
            double = not double
            continue
        if (
            character == "#"
            and not single
            and not double
            and (index == 0 or line[index - 1].isspace())
        ):
            return line[:index].rstrip()
    return line.rstrip()


def _executable_lines(script: str) -> list[str]:
    return [
        stripped.strip()
        for line in script.splitlines()
        if (stripped := _strip_shell_comment(line)).strip()
    ]


_HEREDOC_RE = re.compile(
    r"<<(?P<strip>-)?\s*(?:(?P<quote>['\"])(?P<quoted>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P=quote)|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)
_FUNCTION_HEADER_RE = re.compile(
    r"^(?:function\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*\{$"
)
_FUNCTION_DECLARATION_RE = re.compile(
    r"^(?:function\s+(?P<function_name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*\(\s*\))?|(?P<paren_name>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\))"
    r"(?=\s|\{|$)"
)
_REQUIRED_EXECUTABLES = frozenset(
    {"docker", "git", "grep", "mv", "python3", "sha256sum", "tee", "test"}
)
_CLEANUP_FUNCTIONS = frozenset(
    {"cleanup_mcp_e2e", "cleanup_mcp_proof_fetch", "cleanup_t4_build"}
)
_SAFE_PROBE_TRAPS = frozenset(
    {
        'trap \'docker rm -f "$T4_PROBE" >/dev/null 2>&1 || true; '
        'docker rmi -f "$T4_TAG" >/dev/null 2>&1 || true\' EXIT',
        'trap \'docker rm -f "$CAPLESS_PROBE" "$T4_PROBE" '
        '>/dev/null 2>&1 || true; docker rmi -f "$T4_TAG" '
        ">/dev/null 2>&1 || true' EXIT",
    }
)
_CLEANUP_FILE_VARIABLES = frozenset(
    {
        "ATTESTATION",
        "ATTESTATION_SHA256",
        "ATTESTATION_TMP",
        "MCP_ATTESTATION",
        "MCP_ATTESTATION_SHA256",
        "MCP_ATTESTATION_TMP",
        "MCP_E2E_LOG",
        "MCP_VERIFY_LOG",
        "RUNTIME_VERSION_FILE",
    }
)


def _validate_cleanup_function(name: str, units: list[str]) -> None:
    if name not in _CLEANUP_FUNCTIONS:
        raise OfficialConsumerContractError(
            "workflow ordered executable proof contains an unsupported shell function"
        )
    safe_exact = {
        'docker rm -f "$MCP_VERIFY_CONTAINER" >/dev/null 2>&1 || true',
        'docker image rm -f "$T4_TAG" >/dev/null 2>&1 || true',
        'if [ "$KEEP_MCP_PROOF" -ne 1 ]; then',
        'if [ "$KEEP_T4_IMAGE" -ne 1 ]; then',
        "fi",
    }
    safe_roots = {
        'rm -rf -- "$CI_ROOT"',
        'rm -rf -- "$MOLECULE_CI_ROOT"',
    }
    safe_files = re.compile(
        r"rm -f -- "
        + r"(?:\"\$(?:"
        + "|".join(sorted(_CLEANUP_FILE_VARIABLES))
        + r")\"(?:\s+|$))+"
    )
    if not units or any(
        unit not in safe_exact
        and unit not in safe_roots
        and safe_files.fullmatch(unit) is None
        for unit in units
    ):
        raise OfficialConsumerContractError(
            "workflow cleanup function contains an unsupported command"
        )


def _opens_control(unit: str) -> bool:
    return bool(re.match(r"^(?:if|for|while|until|case)\b", unit)) or unit in {
        "{",
        "(",
    }


def _closes_control(unit: str) -> bool:
    return unit in {"fi", "done", "esac", "}", ")"}


_SHELL_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_SHELL_REDIRECTION_RE = re.compile(
    r"^(?:[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})?"
    r"(?P<operator>&>>|&>|<<<|<<-|<<|<>|>>|>&|<&|>\||>|<)"
    r"(?P<target>.*)$"
)


def _skip_shell_assignments_and_redirections(
    tokens: list[str], position: int
) -> int:
    """Skip simple-command prefixes while retaining the executable position."""

    while position < len(tokens):
        token = tokens[position]
        if _SHELL_ASSIGNMENT_RE.fullmatch(token):
            position += 1
            continue
        redirection = _SHELL_REDIRECTION_RE.fullmatch(token)
        if redirection is None:
            break
        position += 1
        if not redirection.group("target") and position < len(tokens):
            position += 1
    return position


def _shell_command(unit: str) -> tuple[list[str], int] | None:
    try:
        tokens = shlex.split(unit, posix=True)
    except ValueError:
        return None
    position = 0
    while position < len(tokens) and tokens[position] in {
        "(",
        "{",
        "do",
        "else",
        "then",
    }:
        position += 1
    if position < len(tokens) and tokens[position] in {
        "elif",
        "if",
        "until",
        "while",
    }:
        position += 1
    if position < len(tokens) and tokens[position] == "time":
        position += 1
        if position < len(tokens) and tokens[position] == "-p":
            position += 1
    if position < len(tokens) and tokens[position] == "!":
        position += 1
    position = _skip_shell_assignments_and_redirections(tokens, position)
    while position < len(tokens):
        wrapper = tokens[position]
        if wrapper == "builtin":
            position += 1
            if position < len(tokens) and tokens[position] == "--":
                position += 1
        elif wrapper == "command":
            # `command -v/-V` queries a name rather than executing it. `-p` and
            # `--` still wrap an executable command.
            if position + 1 < len(tokens) and tokens[position + 1] in {"-v", "-V"}:
                break
            position += 1
            while position < len(tokens) and tokens[position] in {"-p", "--"}:
                position += 1
        elif wrapper == "exec":
            position += 1
            while position < len(tokens):
                option = tokens[position]
                if option == "--":
                    position += 1
                    break
                if option in {"-c", "-l"}:
                    position += 1
                    continue
                if option == "-a" and position + 1 < len(tokens):
                    position += 2
                    continue
                break
        elif wrapper == "env":
            position += 1
            while position < len(tokens):
                option = tokens[position]
                if option == "--":
                    position += 1
                    break
                if option in {"-i", "--ignore-environment", "-0", "--null"}:
                    position += 1
                    continue
                if option in {"-u", "--unset", "-C", "--chdir", "-a", "--argv0"}:
                    if position + 1 >= len(tokens):
                        return None
                    position += 2
                    continue
                if re.match(r"^--(?:unset|chdir|argv0)=", option):
                    position += 1
                    continue
                break
        else:
            break
        position = _skip_shell_assignments_and_redirections(tokens, position)
    return (tokens, position) if position < len(tokens) else None


def _shell_command_segments(unit: str) -> list[str]:
    """Split a shell unit at unquoted command-list operators.

    The contract does not execute a general shell parser.  This narrow scan is
    only used to make security-sensitive declarations and builtins visible
    even when an earlier no-op precedes them on the same physical line.
    """

    segments: list[str] = []
    start = 0
    single = False
    double = False
    escaped = False
    index = 0
    while index < len(unit):
        character = unit[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\" and not single:
            escaped = True
            index += 1
            continue
        if character == "'" and not double:
            single = not single
            index += 1
            continue
        if character == '"' and not single:
            double = not double
            index += 1
            continue
        if single or double or character not in ";|&":
            index += 1
            continue
        previous = unit[index - 1] if index else ""
        following = unit[index + 1] if index + 1 < len(unit) else ""
        if character == "&" and (previous in {">", "<"} or following == ">"):
            index += 1
            continue
        segment = unit[start:index].strip()
        if segment:
            segments.append(segment)
        operator = character
        index += 1
        if index < len(unit) and unit[index] == operator:
            index += 1
        start = index
    tail = unit[start:].strip()
    if tail:
        segments.append(tail)
    return segments


def _raw_shell_words(segment: str) -> list[str] | None:
    words: list[str] = []
    start: int | None = None
    single = False
    double = False
    escaped = False
    for index, character in enumerate(segment):
        if start is None and not character.isspace():
            start = index
        if escaped:
            escaped = False
            continue
        if character == "\\" and not single:
            escaped = True
            continue
        if character == "'" and not double:
            single = not single
            continue
        if character == '"' and not single:
            double = not double
            continue
        if character.isspace() and not single and not double and start is not None:
            words.append(segment[start:index])
            start = None
    if single or double or escaped:
        return None
    if start is not None:
        words.append(segment[start:])
    return words


def _uses_nonliteral_command_word(unit: str) -> bool:
    for segment in _shell_command_segments(unit):
        command = _shell_command(segment)
        raw_words = _raw_shell_words(segment)
        if command is None or raw_words is None:
            continue
        tokens, command_index = command
        if command_index >= len(raw_words):
            return True
        if raw_words[command_index] != tokens[command_index]:
            return True
    return False


def _function_declaration_name(segment: str) -> str | None:
    candidate = re.sub(
        r"^(?:(?:\{|\(|!|then|do|else|if|elif|until|while)\s+|"
        r"time(?:\s+-p)?\s+)+",
        "",
        segment.lstrip(),
    )
    match = _FUNCTION_DECLARATION_RE.match(candidate)
    if match is not None:
        return match.group("function_name") or match.group("paren_name")
    command = _shell_command(candidate)
    if command is None:
        return None
    tokens, position = command
    if tokens[position] == "function" and position + 1 < len(tokens):
        name = re.sub(r"\(\)$", "", tokens[position + 1])
    else:
        candidate_name = tokens[position]
        if not candidate_name.endswith("()"):
            return None
        name = candidate_name[:-2]
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else None


def _script_units(script: str) -> list[_ShellUnit]:
    """Return reachable shell units after validating bounded cleanup functions.

    This is intentionally a narrow parser for the reviewed official workflow
    shape. Heredoc bodies remain data, while unknown nesting fails closed.
    """

    physical = script.splitlines()
    units: list[_ShellUnit] = []
    controls: list[str] = []
    in_function = False
    function_name = ""
    function_units: list[str] = []
    safe_functions: set[str] = set()
    index = 0
    while index < len(physical):
        pending = ""
        while index < len(physical):
            line = _strip_shell_comment(physical[index]).strip()
            index += 1
            if not line and not pending:
                continue
            if line.endswith("\\"):
                pending += line[:-1].rstrip() + " "
                continue
            unit = (pending + line).strip()
            break
        else:
            break
        if not unit:
            continue

        if any(
            command is not None and command[0][command[1]] == "case"
            for segment in _shell_command_segments(unit)
            if (command := _shell_command(segment)) is not None
        ):
            raise OfficialConsumerContractError(
                "workflow script contains unsupported case control"
            )

        function_header = _FUNCTION_HEADER_RE.fullmatch(unit)
        if not in_function and function_header:
            function_name = function_header.group("name")
            if function_name in _REQUIRED_EXECUTABLES:
                raise OfficialConsumerContractError(
                    "workflow proof shadows a required executable"
                )
            if function_name in safe_functions:
                raise OfficialConsumerContractError(
                    "workflow ordered executable proof redefines a cleanup function"
                )
            in_function = True
            function_units = []
            continue
        if not in_function:
            declared_functions = [
                name
                for segment in _shell_command_segments(unit)
                if (name := _function_declaration_name(segment)) is not None
            ]
            if declared_functions:
                if any(name in _REQUIRED_EXECUTABLES for name in declared_functions):
                    raise OfficialConsumerContractError(
                        "workflow proof shadows a required executable"
                    )
                raise OfficialConsumerContractError(
                    "workflow script contains an unsupported shell function"
                )
        if in_function:
            if unit == "}":
                _validate_cleanup_function(function_name, function_units)
                safe_functions.add(function_name)
                in_function = False
                function_name = ""
                function_units = []
            else:
                function_units.append(unit)
            continue

        trap_segments = []
        for segment in _shell_command_segments(unit):
            command = _shell_command(segment)
            if command is not None and command[0][command[1]] == "trap":
                trap_segments.append(segment)
        if trap_segments:
            trap = re.fullmatch(
                r"trap (?P<function>cleanup_(?:mcp_e2e|mcp_proof_fetch|t4_build)) EXIT",
                unit,
            )
            if len(trap_segments) != 1 or (
                unit not in _SAFE_PROBE_TRAPS
                and (trap is None or trap.group("function") not in safe_functions)
            ):
                raise OfficialConsumerContractError(
                    "workflow proof contains an unsupported exit trap"
                )

        if _closes_control(unit) and controls:
            controls.pop()

        heredoc_bodies: list[str] = []
        for match in _HEREDOC_RE.finditer(unit):
            delimiter = match.group("quoted") or match.group("plain")
            strip_tabs = match.group("strip") is not None
            body: list[str] = []
            found = False
            while index < len(physical):
                raw = physical[index]
                index += 1
                candidate = raw.lstrip("\t") if strip_tabs else raw
                if candidate == delimiter:
                    found = True
                    break
                body.append(raw)
            if not found:
                raise OfficialConsumerContractError(
                    "workflow proof script contains an unterminated heredoc"
                )
            heredoc_bodies.append("\n".join(body))

        units.append(_ShellUnit(unit, tuple(controls), tuple(heredoc_bodies)))
        if _opens_control(unit):
            controls.append(unit)

    if in_function or controls:
        raise OfficialConsumerContractError(
            "workflow proof script has unbalanced shell control flow"
        )
    return units


def _logical_units(script: str) -> list[str]:
    return [unit.text for unit in _script_units(script)]


def _contains_state_mutating_shell_command(units: list[_ShellUnit]) -> bool:
    mutating_commands = {
        ".",
        "declare",
        "eval",
        "export",
        "local",
        "mapfile",
        "read",
        "readarray",
        "readonly",
        "source",
        "typeset",
        "unset",
    }
    for unit in units:
        for segment in _shell_command_segments(unit.text):
            command = _shell_command(segment)
            if command is None:
                continue
            tokens, command_index = command
            name = tokens[command_index]
            if name in mutating_commands or (
                name == "printf" and "-v" in tokens[command_index + 1 :]
            ):
                return True
    return False


_MUTATING_COMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:builtin\s+|command\s+)?"
    r"(?:eval|unset|read|readarray|mapfile|declare|typeset|local|readonly|export|source)\b"
)
_PRINTF_V_RE = re.compile(r"(?:^|[;&|]\s*)printf\b[^;&|\n]*\s-v(?:\s|$)")
_SPLIT_REF_NAME_RE = re.compile(r"MOLECULE_CI_(?:['\"\\]+)REF")


def _validate_ref_script(script: str) -> None:
    executable = "\n".join(_executable_lines(script))
    if (
        _MUTATING_COMMAND_RE.search(executable)
        or _PRINTF_V_RE.search(executable)
        or "GITHUB_ENV" in executable
        or "BASH_ENV" in executable
    ):
        if "MOLECULE_CI_REF" not in executable and not _SPLIT_REF_NAME_RE.search(
            executable
        ):
            raise OfficialConsumerContractError(
                "proof script uses unsupported environment mutation"
            )
        raise OfficialConsumerContractError("proof script can mutate MOLECULE_CI_REF")
    if "MOLECULE_CI_REF" not in executable and not _SPLIT_REF_NAME_RE.search(
        executable
    ):
        return
    if _SPLIT_REF_NAME_RE.search(executable):
        raise OfficialConsumerContractError("proof script can mutate MOLECULE_CI_REF")
    for occurrence in re.finditer("MOLECULE_CI_REF", executable):
        start = occurrence.start()
        end = occurrence.end()
        simple_read = (
            start > 0
            and executable[start - 1] == "$"
            and (
                end == len(executable)
                or not (executable[end].isalnum() or executable[end] in "_[")
            )
        )
        braced_read = (
            start > 1
            and executable[start - 2 : start] == "${"
            and end < len(executable)
            and executable[end] == "}"
        )
        if not (simple_read or braced_read):
            raise OfficialConsumerContractError(
                "proof script can mutate MOLECULE_CI_REF"
            )


def _env(container: dict[str, Any], label: str) -> dict[str, Any]:
    value = container.get("env")
    if value is None:
        return {}
    return _mapping(value, f"{label} env")


def _dangerous_environment_name(name: str) -> bool:
    normalized = name.upper()
    if normalized == "GITHUB_SERVER_URL":
        return False
    return normalized in _DANGEROUS_ENVIRONMENT_NAMES or normalized.startswith(
        (
            "ACTIONS_",
            "BASH_FUNC_",
            "BUILDX_",
            "DOCKER_",
            "DYLD_",
            "GIT_",
            "GITHUB_",
            "LD_",
            "NODE_",
            "NPM_",
            "PIP_",
            "PNPM_",
            "PYTHON",
            "RUNNER_",
            "YARN_",
        )
    )


def _validate_safe_environment(container: dict[str, Any], label: str) -> None:
    environment = _env(container, label)
    dangerous_name = next(
        (name for name in environment if _dangerous_environment_name(name)), None
    )
    if dangerous_name is not None or (
        "GITHUB_SERVER_URL" in environment
        and environment["GITHUB_SERVER_URL"] != "https://git.moleculesai.app"
    ):
        raise OfficialConsumerContractError(
            f"{label} contains a dangerous environment override"
        )


def _validate_permissions(workflow: dict[str, Any], proof_job: dict[str, Any]) -> None:
    expected = {"contents": "read"}
    if workflow.get("permissions") != expected:
        raise OfficialConsumerContractError(
            "workflow must declare exact contents: read permissions"
        )
    if "permissions" in proof_job and proof_job["permissions"] != expected:
        raise OfficialConsumerContractError(
            "t4-conformance proof job must retain exact contents: read permissions"
        )


def _validate_checkout_step(step: dict[str, Any]) -> None:
    if step.get("uses") != _CHECKOUT_ACTION:
        raise OfficialConsumerContractError(
            "t4-conformance proof uses a non-immutable allowlisted action"
        )
    options = _mapping(step.get("with"), "t4-conformance checkout with")
    if options.get("persist-credentials") is not False:
        raise OfficialConsumerContractError(
            "t4-conformance checkout must set persist-credentials: false"
        )
    if options != {"persist-credentials": False}:
        raise OfficialConsumerContractError(
            "t4-conformance checkout has unsupported options"
        )
    if _normalize_condition(step.get("if")) is not None or set(step).difference(
        {"name", "uses", "with"}
    ):
        raise OfficialConsumerContractError(
            "t4-conformance checkout must be unconditional and side-effect bounded"
        )


def _strict_inert_fork_notice(step: dict[str, Any]) -> bool:
    if set(step).difference({"name", "if", "run"}):
        return False
    script = step.get("run")
    if not isinstance(script, str):
        return False
    units = _script_units(script)
    if len(units) != 1 or units[0].controls or units[0].heredoc_bodies:
        return False
    unit = units[0].text
    if any(
        character in unit for character in ("$", "`", "\\", ";", "|", "&", "<", ">")
    ):
        return False
    tokens = _tokens(unit, "fork notice")
    return (
        len(tokens) == 2
        and tokens[0] == "echo"
        and tokens[1].startswith("::notice::")
        and unit.startswith('echo "::notice::')
        and unit.endswith('"')
    )


def _validate_ref_scopes(
    workflow: dict[str, Any], jobs: dict[str, Any], proof_steps: list[dict[str, Any]]
) -> None:
    workflow_env = _env(workflow, "workflow")
    assignments = 0
    for job_name, raw_job in jobs.items():
        job = _mapping(raw_job, f"workflow job {job_name}")
        job_env = _env(job, f"workflow job {job_name}")
        scopes = [
            job_env,
            *[
                _env(step, f"workflow job {job_name} step")
                for step in _steps(job, f"workflow job {job_name}")
            ],
        ]
        for scope in scopes:
            if "MOLECULE_CI_REF" in scope:
                assignments += 1
                if scope["MOLECULE_CI_REF"] != FINAL_IMAGE_VERIFIER_REF:
                    raise OfficialConsumerContractError(
                        "workflow contains a dynamic or divergent MOLECULE_CI_REF assignment"
                    )
    if "MOLECULE_CI_REF" in workflow_env:
        assignments += 1
        if workflow_env["MOLECULE_CI_REF"] != FINAL_IMAGE_VERIFIER_REF:
            raise OfficialConsumerContractError(
                "workflow contains a dynamic or divergent MOLECULE_CI_REF assignment"
            )
    if not assignments:
        raise OfficialConsumerContractError(
            "workflow does not pin the reviewed molecule-ci verifier"
        )

    proof_job = _mapping(jobs[_PROOF_JOB], "t4-conformance proof job")
    proof_job_env = _env(proof_job, "t4-conformance proof job")
    ref_read = False
    for step in proof_steps:
        script = step.get("run")
        if not isinstance(script, str):
            continue
        _validate_ref_script(script)
        if "$MOLECULE_CI_REF" not in "\n".join(_executable_lines(script)):
            continue
        ref_read = True
        effective = {
            **workflow_env,
            **proof_job_env,
            **_env(step, "t4-conformance proof step"),
        }
        if effective.get("MOLECULE_CI_REF") != FINAL_IMAGE_VERIFIER_REF:
            raise OfficialConsumerContractError(
                "workflow final-image fetch does not use the reviewed MOLECULE_CI_REF"
            )
    if not ref_read:
        raise OfficialConsumerContractError(
            "workflow final-image fetch does not use the reviewed MOLECULE_CI_REF"
        )


def _starts_git_command(unit: str, operation: str) -> bool:
    return (
        bool(
            re.match(
                r"^(?:if\s+)?(?:[A-Z_][A-Z0-9_]*=[^ ]+\s+)*git\b",
                unit,
            )
        )
        and operation in unit
    )


_EXACT_FETCH_RE = re.compile(
    r"^(?:if )?GIT_ASKPASS=/bin/false GIT_TERMINAL_PROMPT=0 "
    r"git -c credential\.helper= -c http\.userAgent=curl/8\.4\.0 "
    r'-C "\$[A-Z_][A-Z0-9_]*" fetch (?:-q )?--no-tags --depth 1 '
    r'origin "\$MOLECULE_CI_REF"(?:; then)?$'
)


def _exact_fetch_command(unit: str) -> bool:
    if not (_starts_git_command(unit, " fetch ") and "MOLECULE_CI_REF" in unit):
        return False
    match = _EXACT_FETCH_RE.fullmatch(unit)
    if match is None or unit.startswith("if ") != unit.endswith("; then"):
        raise OfficialConsumerContractError(
            "workflow exact-ref fetch masks or alters a required command"
        )
    return True


def _tokens(unit: str, label: str) -> list[str]:
    try:
        return shlex.split(unit, posix=True)
    except ValueError as exc:
        raise OfficialConsumerContractError(
            f"workflow {label} command is not statically parseable"
        ) from exc


def _relaxes_fail_closed_shell_mode(unit: str) -> bool:
    for segment in _shell_command_segments(unit):
        command = _shell_command(segment)
        if command is None:
            continue
        tokens, command_index = command
        if tokens[command_index] != "set":
            continue
        options = tokens[command_index + 1 :]
        for index, token in enumerate(options):
            if re.fullmatch(r"\+[A-Za-z]*[eu][A-Za-z]*", token):
                return True
            if token == "+o" and index + 1 < len(options):
                if options[index + 1] in {"errexit", "nounset", "pipefail"}:
                    return True
    return False


def _dangerous_shell_environment_override(unit: str) -> bool:
    if any(marker in unit for marker in ("$GITHUB_PATH", "${GITHUB_PATH}")):
        return True
    for segment in _shell_command_segments(unit):
        command = _shell_command(segment)
        if command is not None and command[0][command[1]] in {
            "alias",
            "enable",
            "hash",
            "shopt",
            "unalias",
        }:
            return True
    if _EXACT_FETCH_RE.fullmatch(unit) is not None:
        return False
    assignments = re.finditer(r"(?<![A-Za-z0-9_])(?P<name>[A-Z_][A-Z0-9_]*)=", unit)
    return any(
        match.group("name") == "GITHUB_SERVER_URL"
        or _dangerous_environment_name(match.group("name"))
        for match in assignments
    )


def _contains_background_operator(unit: str) -> bool:
    for index, character in enumerate(unit):
        if character != "&":
            continue
        previous = unit[index - 1] if index else ""
        following = unit[index + 1] if index + 1 < len(unit) else ""
        if previous in {"&", ">", "<"} or following in {"&", ">"}:
            continue
        return True
    return False


def _uses_dynamic_command_dispatch(unit: str) -> bool:
    for segment in _shell_command_segments(unit):
        command = _shell_command(segment)
        if command is None:
            continue
        tokens, position = command
        if position < len(tokens) and any(
            marker in tokens[position] for marker in ("$", "`")
        ):
            return True
    return False


def _validate_pre_sentinel_shell(
    trace: list[_TraceUnit], loader_index: int, sentinel_index: int
) -> None:
    for index, unit in enumerate(trace[: sentinel_index + 1]):
        if unit.heredoc_bodies and index != loader_index:
            raise OfficialConsumerContractError(
                "workflow proof contains an unreviewed executable heredoc before "
                "the verifier sentinel"
            )
        if _relaxes_fail_closed_shell_mode(unit.text):
            raise OfficialConsumerContractError(
                "workflow proof relaxes fail-closed shell mode before the sentinel"
            )
        if _dangerous_shell_environment_override(unit.text):
            raise OfficialConsumerContractError(
                "workflow proof contains a dangerous environment override"
            )
        if _uses_dynamic_command_dispatch(unit.text):
            raise OfficialConsumerContractError(
                "workflow proof uses dynamic command dispatch before the verifier sentinel"
            )
        if _uses_nonliteral_command_word(unit.text):
            raise OfficialConsumerContractError(
                "workflow proof uses a quoted or escaped command name before "
                "the verifier sentinel"
            )
        if _contains_background_operator(unit.text):
            raise OfficialConsumerContractError(
                "workflow proof starts background work before the verifier sentinel"
            )


def _validate_no_privileged_pre_sentinel(
    trace: list[_TraceUnit],
    sentinel_index: int,
    allowed_docker_indexes: frozenset[int],
) -> None:
    forbidden_option = re.compile(
        r"(?:^|\s)(?:--privileged(?:=true)?|--pid(?:=|\s+)host|"
        r"--network(?:=|\s+)host|--volume(?:=|\s+)|-v(?:=|\s+)|"
        r"--mount(?:=|\s+)|--volumes-from(?:=|\s+))"
    )
    safe_runtime_probe = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "python3",
        "$T4_TAG",
        "-c",
        "from importlib.metadata import version; "
        'print(version("molecules-workspace-runtime"))',
    ]
    safe_cleanup = {
        'docker rm -f "$MCP_VERIFY_CONTAINER" >/dev/null 2>&1 || true',
        'docker rm "$MCP_VERIFY_CONTAINER" >/dev/null',
    }
    for index, unit in enumerate(trace[:sentinel_index]):
        text = unit.text
        if (
            forbidden_option.search(text)
            or "/var/run/docker.sock" in text
            or re.search(r"\b(?:nsenter|pkexec|sudo|unshare)\b", text)
        ):
            raise OfficialConsumerContractError(
                "workflow privileged or host-bound operation precedes the verifier sentinel"
            )
        docker_commands = []
        for segment in _shell_command_segments(text):
            command = _shell_command(segment)
            if command is not None and command[0][command[1]] == "docker":
                docker_commands.append(segment)
        if not docker_commands or index in allowed_docker_indexes:
            continue
        if text == "if ! docker info >/dev/null 2>&1; then" or text in safe_cleanup:
            continue
        if (
            text.startswith("docker run ")
            and _tokens(text, "pre-sentinel docker run") == safe_runtime_probe
        ):
            continue
        raise OfficialConsumerContractError(
            "workflow privileged or host-bound operation precedes the verifier sentinel"
        )


def _is_docker_image_mutation(unit: str) -> bool:
    for segment in _shell_command_segments(unit):
        command = _shell_command(segment)
        if command is None:
            continue
        tokens, position = command
        if tokens[position] != "docker" or position + 1 >= len(tokens):
            continue
        operation = tokens[position + 1 :]
        if operation[0] in {"build", "commit", "import", "load", "tag"} or (
            len(operation) >= 2
            and operation[0] == "buildx"
            and operation[1] == "build"
        ) or (
            len(operation) >= 2
            and operation[0] == "image"
            and operation[1] in {"build", "import", "load", "tag"}
        ):
            return True
    return False


def _option_values(tokens: list[str], option: str) -> list[str]:
    values: list[str] = []
    for index, token in enumerate(tokens):
        if token == option:
            if index + 1 < len(tokens):
                values.append(tokens[index + 1])
        elif token.startswith(f"{option}="):
            values.append(token[len(option) + 1 :])
    return values


def _build_command(unit: str) -> bool:
    if not unit.startswith("docker build "):
        return False
    tokens = _tokens(unit, "docker build")
    tags = _option_values(tokens, "-t") + _option_values(tokens, "--tag")
    if tags != ["$T4_TAG"]:
        raise OfficialConsumerContractError(
            "verifier does not use the same final image that was built"
        )
    command = [
        "docker",
        "build",
        "--build-arg",
        "RUNTIME_VERSION=$EXPECTED_RUNTIME_VERSION",
        "-t",
        "$T4_TAG",
        ".",
    ]
    if tokens not in (command, command + ["--no-cache", "2>&1", "|", "tail", "-5"]):
        raise OfficialConsumerContractError(
            "workflow final-image build is unsafe or ambiguous"
        )
    return True


def _create_command(unit: str) -> bool:
    if not unit.startswith("docker create "):
        return False
    tokens = _tokens(unit, "docker create")
    if any(
        token.startswith("-v")
        or token == "--volume"
        or token.startswith("--volume=")
        or token == "--mount"
        or token.startswith("--mount=")
        or token == "--volumes-from"
        or token.startswith("--volumes-from=")
        for token in tokens
    ):
        raise OfficialConsumerContractError("final-image verifier uses a bind mount")
    required_options = {
        ("--name", "$MCP_VERIFY_CONTAINER"),
        ("--network", "none"),
        ("--user", "1000:1000"),
        ("--workdir", "/tmp"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--pids-limit", "128"),
        ("--memory", "768m"),
        ("--cpus", "1"),
        ("--tmpfs", "/tmp:size=64m"),
        ("--entrypoint", "python3"),
    }
    if (
        any(
            _option_values(tokens, option) != [value]
            for option, value in required_options
        )
        or tokens.count("--interactive") != 1
    ):
        raise OfficialConsumerContractError(
            "final-image verifier has unsafe or ambiguous container options"
        )

    allowed_value_options = {option for option, _ in required_options} | {"--env"}
    allowed_flags = {"--interactive"}
    position = 2
    env_values: list[str] = []
    while position < len(tokens):
        token = tokens[position]
        if token == "$T4_TAG":
            break
        if token in allowed_flags:
            position += 1
            continue
        matched_option = next(
            (
                option
                for option in allowed_value_options
                if token == option or token.startswith(f"{option}=")
            ),
            None,
        )
        if matched_option is None:
            if re.fullmatch(r"\$[A-Z_][A-Z0-9_]*TAG", token):
                raise OfficialConsumerContractError(
                    "verifier does not use the same final image that was built"
                )
            raise OfficialConsumerContractError(
                "final-image verifier has unsafe or ambiguous container options"
            )
        if token == matched_option:
            if position + 1 >= len(tokens):
                raise OfficialConsumerContractError(
                    "final-image verifier has unsafe or ambiguous container options"
                )
            value = tokens[position + 1]
            position += 2
        else:
            value = token[len(matched_option) + 1 :]
            position += 1
        if matched_option == "--env":
            env_values.append(value)

    if env_values not in (
        [],
        ["MOLECULE_PREBAKE_NODE_BIN=/home/agent/.hermes/node/bin"],
    ):
        raise OfficialConsumerContractError(
            "final-image verifier has unsafe or ambiguous container options"
        )
    tail = tokens[position:]
    if tail not in (
        ["$T4_TAG", "/mcp_built_image_e2e.py"],
        ["$T4_TAG", "/mcp_built_image_e2e.py", ">/dev/null"],
    ):
        raise OfficialConsumerContractError(
            "final-image verifier has unsafe or ambiguous container options"
        )
    return True


def _find_trace(trace: list[_TraceUnit], predicate: Any, start: int, label: str) -> int:
    for index in range(start, len(trace)):
        if predicate(trace[index].text):
            if "|| true" in trace[index].text or "|| :" in trace[index].text:
                raise OfficialConsumerContractError(
                    "ordered executable proof masks a required command failure"
                )
            return index
    raise OfficialConsumerContractError(
        f"workflow ordered executable proof is missing {label}"
    )


def _validate_tag_binding(script: str, stage: str) -> None:
    units = _logical_units(script)
    try:
        stage_index = units.index(stage)
    except ValueError as exc:
        raise OfficialConsumerContractError(
            "verifier does not use the same final image that was built"
        ) from exc
    assignments = [
        unit
        for unit in units[:stage_index]
        if re.match(r"^T4_TAG(?:\[[^]]*\])?\s*(?:\+?=)", unit)
    ]
    if assignments != [_T4_TAG_ASSIGNMENT]:
        raise OfficialConsumerContractError(
            "verifier does not use the same final image that was built"
        )


_PATH_ATTESTATION_LOADER = """\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
from mcp_built_image_e2e import load_attestation

with Path(sys.argv[2]).open("rb") as stream:
    sys.stdout.write(load_attestation(stream).runtime_version)"""
_IMPORTLIB_ATTESTATION_LOADER = """\
import importlib.util
from pathlib import Path
import sys

verifier_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "_molecule_ci_mcp_built_image_e2e", verifier_path
)
if spec is None or spec.loader is None:
    raise SystemExit("could not load immutable built-image verifier")
verifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)
with Path(sys.argv[2]).open("rb") as stream:
    print(verifier.load_attestation(stream).runtime_version)"""
_ALLOWED_ATTESTATION_LOADER_BODIES = frozenset(
    {
        _PATH_ATTESTATION_LOADER,
        _IMPORTLIB_ATTESTATION_LOADER,
    }
)


def _has_attestation_load(unit: _TraceUnit) -> bool:
    if "python3 " not in unit.text or "<<" not in unit.text:
        return False
    conditional_nodes = (
        ast.Assert,
        ast.AsyncFunctionDef,
        ast.BoolOp,
        ast.ClassDef,
        ast.comprehension,
        ast.For,
        ast.FunctionDef,
        ast.If,
        ast.IfExp,
        ast.Lambda,
        ast.Match,
        ast.Try,
        ast.While,
    )

    def contains_reachable_call(node: ast.AST, conditional: bool = False) -> bool:
        conditional = conditional or isinstance(node, conditional_nodes)
        if isinstance(node, ast.Call):
            function = node.func
            if (
                (isinstance(function, ast.Name) and function.id == "load_attestation")
                or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "load_attestation"
                )
            ) and not conditional:
                return True
        return any(
            contains_reachable_call(child, conditional)
            for child in ast.iter_child_nodes(node)
        )

    for body in unit.heredoc_bodies:
        if body.strip() not in _ALLOWED_ATTESTATION_LOADER_BODIES:
            continue
        try:
            module = ast.parse(body, mode="exec")
        except SyntaxError:
            continue
        if contains_reachable_call(module):
            return True
    return False


def _validate_attestation_loader(
    trace: list[_TraceUnit],
    remote_index: int,
    loader_index: int,
    start_index: int,
) -> str:
    unit = trace[loader_index]
    if unit.controls or len(unit.heredoc_bodies) != 1:
        raise OfficialConsumerContractError(
            "workflow reviewed attestation loader is not top-level and exact"
        )
    body = unit.heredoc_bodies[0].strip()
    prefix = r'(?:EXPECTED_RUNTIME_VERSION="\$\()?'
    if body == _PATH_ATTESTATION_LOADER:
        loader_pattern = (
            prefix
            + r'python3 - "\$(?P<root>[A-Z_][A-Z0-9_]*)" '
            + r'"\$(?P<attestation>[A-Z_][A-Z0-9_]*)" <<\'PY\''
        )
    elif body == _IMPORTLIB_ATTESTATION_LOADER:
        loader_pattern = (
            prefix
            + r'python3 - "\$(?P<root>[A-Z_][A-Z0-9_]*)/'
            + r'scripts/mcp_built_image_e2e\.py" '
            + r'"\$(?P<attestation>[A-Z_][A-Z0-9_]*)" <<\'PY\''
        )
    else:
        raise OfficialConsumerContractError(
            "workflow reviewed attestation loader contains unreviewed Python"
        )
    loader = re.fullmatch(loader_pattern, unit.text)
    remote = re.search(
        r'git\s+-C\s+"\$([A-Z_][A-Z0-9_]*)"\s+remote add origin',
        trace[remote_index].text,
    )
    if loader is None or remote is None:
        raise OfficialConsumerContractError(
            "workflow reviewed attestation loader source is invalid"
        )
    if _binding_snapshot(
        trace, loader_index, loader.group("root")
    ) != _binding_snapshot(trace, remote_index, remote.group(1)):
        raise OfficialConsumerContractError(
            "workflow reviewed attestation loader source is divergent"
        )

    start_attestation, _ = _start_bindings(trace[start_index].text)
    start_attestation_name = _variable_token_name(
        start_attestation, "attestation input"
    )
    if _binding_snapshot(
        trace, loader_index, loader.group("attestation")
    ) != _binding_snapshot(trace, start_index, start_attestation_name):
        raise OfficialConsumerContractError(
            "workflow reviewed attestation loader reads a divergent attestation"
        )
    return loader.group("root")


def _require_top_level(trace: list[_TraceUnit], index: int, label: str) -> None:
    if trace[index].controls:
        raise OfficialConsumerContractError(
            f"workflow ordered executable proof hides {label} behind shell control flow"
        )


def _start_command(unit: str) -> bool:
    if not unit.startswith("docker start "):
        return False
    tokens = _tokens(unit, "verifier start")
    if (
        len(tokens) != 10
        or tokens[:4] != ["docker", "start", "--attach", "--interactive"]
        or tokens[4] != "$MCP_VERIFY_CONTAINER"
        or tokens[5] != "<"
        or re.fullmatch(r"\$[A-Z_][A-Z0-9_]*", tokens[6]) is None
        or tokens[7:9] != ["|", "tee"]
        or re.fullmatch(r"\$[A-Z_][A-Z0-9_]*", tokens[9]) is None
    ):
        raise OfficialConsumerContractError(
            "workflow verifier start masks or alters the required command"
        )
    return True


def _start_bindings(unit: str) -> tuple[str, str]:
    if not _start_command(unit):
        raise OfficialConsumerContractError("workflow verifier start is invalid")
    tokens = _tokens(unit, "verifier start")
    return tokens[6], tokens[9]


_SENTINEL_ASSERTION_RE = re.compile(
    r"^(?P<conditional>if ! )?grep -qxF "
    r"(?P<quote>['\"])mcp-built-image-e2e:sentinel:executed(?P=quote) "
    r'"?(?P<log>\$[A-Z_][A-Z0-9_]*)"?(?P<tail>; then)?$'
)


def _sentinel_assertion(unit: str) -> bool:
    if not (unit.startswith("grep ") or unit.startswith("if ! grep ")):
        return False
    if "mcp-built-image-e2e:sentinel:executed" not in unit:
        return False
    match = _SENTINEL_ASSERTION_RE.fullmatch(unit)
    if match is None or (match.group("conditional") is None) != (
        match.group("tail") is None
    ):
        raise OfficialConsumerContractError(
            "workflow sentinel assertion masks or alters the required command"
        )
    return True


def _validate_sentinel_assertion(trace: list[_TraceUnit], sentinel_index: int) -> str:
    unit = trace[sentinel_index]
    match = _SENTINEL_ASSERTION_RE.fullmatch(unit.text)
    if match is None:
        raise OfficialConsumerContractError("workflow sentinel assertion is invalid")
    if match.group("conditional") is not None and not any(
        candidate.text == "exit 1" and candidate.controls == (unit.text,)
        for candidate in trace[sentinel_index + 1 :]
        if candidate.step_index == unit.step_index
    ):
        raise OfficialConsumerContractError(
            "workflow sentinel assertion does not fail closed"
        )
    return match.group("log")


_STABLE_RUNNER_VARIABLES = frozenset(
    {
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_RUN_ID",
        "RUNNER_TEMP",
    }
)
_SHELL_VARIABLE_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Z_][A-Z0-9_]*)(?::-[^}]*)?\}|"
    r"(?P<plain>[A-Z_][A-Z0-9_]*))"
)


def _binding_before(trace: list[_TraceUnit], index: int, name: str) -> str:
    step_index = trace[index].step_index
    pattern = re.compile(rf"^{re.escape(name)}=(?P<value>.+)$")
    bindings = [
        match.group("value")
        for unit in trace[:index]
        if unit.step_index == step_index
        and not unit.controls
        and (match := pattern.fullmatch(unit.text)) is not None
    ]
    if len(bindings) != 1:
        raise OfficialConsumerContractError(f"workflow has an ambiguous {name} binding")
    return bindings[0]


def _binding_snapshot(
    trace: list[_TraceUnit], index: int, name: str, seen: frozenset[str] = frozenset()
) -> tuple[str, tuple[tuple[str, object], ...]]:
    if name in seen:
        raise OfficialConsumerContractError(f"workflow has a recursive {name} binding")
    value = _binding_before(trace, index, name)
    if (
        re.fullmatch(r'"(?:[^"`\\]|\\.)*"', value) is None
        or "$(" in value
        or "`" in value
    ):
        raise OfficialConsumerContractError(
            f"workflow has a dynamic or unsafe {name} binding"
        )
    unparsed = _SHELL_VARIABLE_RE.sub("", value)
    if "$" in unparsed:
        raise OfficialConsumerContractError(
            f"workflow has a dynamic or unsafe {name} binding"
        )
    nested: list[tuple[str, object]] = []
    for match in _SHELL_VARIABLE_RE.finditer(value):
        dependency = match.group("braced") or match.group("plain")
        if dependency in _STABLE_RUNNER_VARIABLES:
            continue
        nested.append(
            (
                dependency,
                _binding_snapshot(trace, index, dependency, seen | {name}),
            )
        )
    return value, tuple(nested)


def _variable_token_name(token: str, label: str) -> str:
    match = re.fullmatch(r"\$([A-Z_][A-Z0-9_]*)", token)
    if match is None:
        raise OfficialConsumerContractError(
            f"workflow ordered executable proof has an invalid {label} path"
        )
    return match.group(1)


def _exact_sha_check(unit: _TraceUnit, sidecar: str) -> bool:
    return (
        unit.text.startswith("sha256sum ")
        and not unit.controls
        and _tokens(unit.text, "attestation content seal")
        == [
            "sha256sum",
            "--check",
            sidecar,
        ]
    )


def _loader_seal_check_index(
    trace: list[_TraceUnit], loader_index: int, sidecar: str
) -> int | None:
    if loader_index > 0 and _exact_sha_check(trace[loader_index - 1], sidecar):
        return loader_index - 1
    if (
        loader_index > 1
        and not trace[loader_index - 1].controls
        and re.fullmatch(r'[A-Z_][A-Z0-9_]*="\$\(', trace[loader_index - 1].text)
        and _exact_sha_check(trace[loader_index - 2], sidecar)
    ):
        return loader_index - 2
    return None


def _attestation_reference_is_allowed(
    unit: _TraceUnit,
    attestation: str,
    sidecar: str,
    loader_index: int,
    current_index: int,
    start_index: int,
) -> bool:
    if current_index == loader_index or current_index == start_index:
        return True
    tokens = _tokens(unit.text, "attestation use")
    if tokens in (["test", "-s", attestation], ["test", "-f", attestation]):
        return True
    if re.fullmatch(
        rf'if \[ ! -s "{re.escape(attestation)}" \]'
        r'(?: \|\| \[ ! -s "\$[A-Z_][A-Z0-9_]*" \])?; then',
        unit.text,
    ):
        return True
    return _exact_sha_check(unit, sidecar)


def _validate_attestation_flow(
    trace: list[_TraceUnit],
    lockstep_index: int,
    loader_index: int,
    start_index: int,
) -> None:
    lockstep_tokens = _tokens(trace[lockstep_index].text, "MCP lockstep")
    if len(lockstep_tokens) != 7 or lockstep_tokens[2:6] != [
        "--repo-root",
        ".",
        "--json",
        ">",
    ]:
        raise OfficialConsumerContractError(
            "workflow ordered executable proof does not persist the attestation safely"
        )
    produced = lockstep_tokens[6]
    consumed, _ = _start_bindings(trace[start_index].text)
    produced_name = _variable_token_name(produced, "attestation output")
    consumed_name = _variable_token_name(consumed, "attestation input")
    consumed_at_generation = _binding_snapshot(trace, lockstep_index, consumed_name)
    consumed_at_start = _binding_snapshot(trace, start_index, consumed_name)
    if consumed_at_generation != consumed_at_start:
        raise OfficialConsumerContractError(
            "workflow ordered executable proof consumes a different attestation"
        )
    produced_value = _binding_before(trace, lockstep_index, produced_name)
    if produced_value != f'"${{{consumed_name}}}.tmp"':
        raise OfficialConsumerContractError(
            "workflow ordered executable proof consumes a different attestation"
        )

    move_index = lockstep_index + 1
    optional_size_check = _tokens(
        trace[move_index].text, "attestation temporary file check"
    )
    if optional_size_check == ["test", "-s", produced]:
        move_index += 1
    if move_index >= loader_index or (
        trace[move_index].controls
        or _tokens(trace[move_index].text, "attestation move")
        not in (["mv", produced, consumed], ["mv", "-f", produced, consumed])
    ):
        raise OfficialConsumerContractError(
            "workflow ordered executable proof consumes a different attestation"
        )

    seal_index = move_index + 1
    if seal_index >= loader_index:
        raise OfficialConsumerContractError(
            "workflow attestation content seal is missing"
        )
    seal_tokens = _tokens(trace[seal_index].text, "attestation content seal")
    if (
        trace[seal_index].controls
        or len(seal_tokens) != 4
        or seal_tokens[:2] != ["sha256sum", consumed]
        or seal_tokens[2] != ">"
        or re.fullmatch(r"\$[A-Z_][A-Z0-9_]*", seal_tokens[3]) is None
    ):
        raise OfficialConsumerContractError(
            "workflow attestation content seal is missing"
        )
    sidecar = seal_tokens[3]
    sidecar_name = _variable_token_name(sidecar, "attestation content seal")
    if _binding_before(trace, seal_index, sidecar_name) != (
        f'"${{{consumed_name}}}.sha256"'
    ):
        raise OfficialConsumerContractError(
            "workflow attestation content seal has a divergent path"
        )

    loader_check_index = _loader_seal_check_index(trace, loader_index, sidecar)
    if loader_check_index is None:
        raise OfficialConsumerContractError(
            "workflow attestation content seal is not checked before parsing"
        )
    if start_index == 0 or not _exact_sha_check(trace[start_index - 1], sidecar):
        raise OfficialConsumerContractError(
            "workflow attestation content seal is not checked immediately before start"
        )
    sidecar_at_generation = _binding_snapshot(trace, seal_index, sidecar_name)
    for check_index in (loader_check_index, start_index - 1):
        if _binding_snapshot(trace, check_index, sidecar_name) != sidecar_at_generation:
            raise OfficialConsumerContractError(
                "workflow attestation content seal has a divergent path"
            )

    for index, unit in enumerate(trace[seal_index + 1 : start_index], seal_index + 1):
        if attestation_in_unit := (consumed in unit.text or sidecar in unit.text):
            if attestation_in_unit and not _attestation_reference_is_allowed(
                unit,
                consumed,
                sidecar,
                loader_index,
                index,
                start_index,
            ):
                raise OfficialConsumerContractError(
                    "workflow attestation is rewritten after its content seal"
                )


def _validate_verifier_copy(
    trace: list[_TraceUnit], remote_index: int, copy_index: int
) -> str:
    tokens = _tokens(trace[copy_index].text, "docker cp")
    if len(tokens) != 4 or tokens[:2] != ["docker", "cp"]:
        raise OfficialConsumerContractError(
            "workflow ordered executable proof has an invalid verifier copy"
        )
    source, destination = tokens[2], tokens[3]
    if destination != "$MCP_VERIFY_CONTAINER:/mcp_built_image_e2e.py":
        raise OfficialConsumerContractError(
            "workflow ordered executable proof copies the verifier elsewhere"
        )
    source_match = re.fullmatch(
        r"\$([A-Z_][A-Z0-9_]*)/scripts/mcp_built_image_e2e\.py", source
    )
    if source_match is not None:
        copy_root = source_match.group(1)
    elif source == "$VERIFIER":
        verifier_value = _binding_before(trace, copy_index, "VERIFIER")
        verifier_match = re.fullmatch(
            r'"\$([A-Z_][A-Z0-9_]*)/scripts/mcp_built_image_e2e\.py"',
            verifier_value,
        )
        if verifier_match is None:
            raise OfficialConsumerContractError(
                "workflow ordered executable proof copies an unreviewed verifier"
            )
        copy_root = verifier_match.group(1)
    else:
        raise OfficialConsumerContractError(
            "workflow ordered executable proof copies an unreviewed verifier"
        )
    remote_match = re.search(
        r'git\s+-C\s+"\$([A-Z_][A-Z0-9_]*)"\s+remote add origin',
        trace[remote_index].text,
    )
    if remote_match is None:
        raise OfficialConsumerContractError(
            "workflow ordered executable proof copies an unreviewed verifier"
        )
    fetch_root = remote_match.group(1)
    if _binding_snapshot(trace, remote_index, fetch_root) != _binding_snapshot(
        trace, copy_index, copy_root
    ):
        raise OfficialConsumerContractError(
            "workflow ordered executable proof copies an unreviewed verifier"
        )
    return copy_root


def _validate_lockstep_checker(
    trace: list[_TraceUnit], remote_index: int, lockstep_index: int
) -> str:
    tokens = _tokens(trace[lockstep_index].text, "MCP lockstep")
    if (
        len(tokens) != 7
        or tokens[0] != "python3"
        or tokens[2:6] != ["--repo-root", ".", "--json", ">"]
        or re.fullmatch(r"\$[A-Z_][A-Z0-9_]*", tokens[6]) is None
    ):
        raise OfficialConsumerContractError(
            "workflow ordered executable proof uses an unreviewed lockstep checker"
        )
    checker_match = re.fullmatch(
        r"\$([A-Z_][A-Z0-9_]*)/scripts/mcp_pin_lockstep\.py", tokens[1]
    )
    remote_match = re.search(
        r'git\s+-C\s+"\$([A-Z_][A-Z0-9_]*)"\s+remote add origin',
        trace[remote_index].text,
    )
    if checker_match is None or remote_match is None:
        raise OfficialConsumerContractError(
            "workflow ordered executable proof uses an unreviewed lockstep checker"
        )
    if _binding_snapshot(
        trace, lockstep_index, checker_match.group(1)
    ) != _binding_snapshot(trace, remote_index, remote_match.group(1)):
        raise OfficialConsumerContractError(
            "workflow ordered executable proof uses an unreviewed lockstep checker"
        )
    return checker_match.group(1)


def _validate_reviewed_tool_content_seal(
    trace: list[_TraceUnit], use_index: int, root_name: str
) -> None:
    if use_index == 0:
        raise OfficialConsumerContractError(
            "workflow reviewed tool content seal is missing"
        )
    seal = trace[use_index - 1]
    expected = [
        "git",
        "-C",
        f"${root_name}",
        "diff",
        "--quiet",
        "--no-ext-diff",
        "--no-textconv",
        "$MOLECULE_CI_REF",
        "--",
        "scripts/mcp_pin_lockstep.py",
        "scripts/mcp_built_image_e2e.py",
    ]
    if seal.controls or _tokens(seal.text, "reviewed tool content seal") != expected:
        raise OfficialConsumerContractError(
            "workflow reviewed tool content seal is missing or not adjacent"
        )
    if _binding_snapshot(trace, use_index, root_name) != _binding_snapshot(
        trace, use_index - 1, root_name
    ):
        raise OfficialConsumerContractError(
            "workflow reviewed tool content seal uses a divergent source root"
        )


def _validate_verifier_container_binding(
    trace: list[_TraceUnit], create_index: int, copy_index: int, start_index: int
) -> None:
    expected = (
        r'"mcp-(?:built-image-e2e|verify)-'
        r'\$\{GITHUB_RUN_ID:-local\}-\$\{GITHUB_RUN_ATTEMPT:-1\}"'
    )
    value = _binding_before(trace, create_index, "MCP_VERIFY_CONTAINER")
    if re.fullmatch(expected, value) is None:
        raise OfficialConsumerContractError(
            "workflow verifier container name is not run-scoped"
        )
    snapshot = _binding_snapshot(trace, create_index, "MCP_VERIFY_CONTAINER")
    if any(
        _binding_snapshot(trace, index, "MCP_VERIFY_CONTAINER") != snapshot
        for index in (copy_index, start_index)
    ):
        raise OfficialConsumerContractError(
            "workflow verifier container binding changes during the proof"
        )


def _validate_ordered_proof(
    proof_steps: list[dict[str, Any]],
    proof_step_indexes: set[int],
    job_condition: str | None,
) -> None:
    scripts: dict[int, str] = {}
    trace: list[_TraceUnit] = []
    for step_index, step in enumerate(proof_steps):
        script = step.get("run")
        if not isinstance(script, str):
            continue
        scripts[step_index] = script
        units = _script_units(script)
        if step_index in proof_step_indexes and (
            not units or units[0].text != "set -euo pipefail"
        ):
            raise OfficialConsumerContractError(
                "ordered executable proof step does not enable fail-closed shell mode"
            )
        trace.extend(
            _TraceUnit(
                step_index,
                unit.text,
                unit.controls,
                unit.heredoc_bodies,
            )
            for unit in units
        )

    remote_index = _find_trace(
        trace,
        lambda unit: (
            unit.startswith("git ")
            and " remote add origin " in unit
            and unit.endswith("https://git.moleculesai.app/molecule-ai/molecule-ci.git")
        ),
        0,
        "the canonical molecule-ci remote",
    )
    fetch_index = _find_trace(
        trace,
        _exact_fetch_command,
        remote_index + 1,
        "the anonymous exact-ref fetch",
    )
    checkout_index = _find_trace(
        trace,
        lambda unit: (
            unit.startswith("git ") and " checkout -q --detach FETCH_HEAD" in unit
        ),
        fetch_index + 1,
        "the detached checkout",
    )
    rev_index = _find_trace(
        trace,
        lambda unit: (
            "rev-parse HEAD" in unit and not unit.startswith(("echo ", "printf "))
        ),
        checkout_index + 1,
        "the fetched-ref resolution",
    )
    comparison_index = _find_trace(
        trace,
        lambda unit: (
            "$MOLECULE_CI_REF" in unit
            and unit.startswith(("test ", "if [", "if [["))
            and (" = " in unit or " != " in unit)
        ),
        rev_index,
        "the fetched-ref equality check",
    )
    lockstep_index = _find_trace(
        trace,
        lambda unit: (
            unit.startswith("python3 ")
            and "mcp_pin_lockstep.py" in unit
            and "--repo-root ." in unit
            and "--json" in unit
        ),
        comparison_index + 1,
        "the immutable pin attestation",
    )
    loader_index = _find_trace(
        trace,
        lambda unit: "python3 " in unit and "<<" in unit,
        lockstep_index + 1,
        "the attestation parser",
    )
    if not _has_attestation_load(trace[loader_index]):
        raise OfficialConsumerContractError(
            "workflow ordered executable proof is missing the hardened attestation "
            "load from the reviewed attestation loader"
        )
    build_index = _find_trace(
        trace, _build_command, loader_index + 1, "the final-image build"
    )
    create_index = _find_trace(
        trace, _create_command, build_index + 1, "the unprivileged verifier container"
    )
    copy_index = _find_trace(
        trace,
        lambda unit: unit.startswith("docker cp "),
        create_index + 1,
        "the verifier copy",
    )
    start_index = _find_trace(
        trace,
        _start_command,
        copy_index + 1,
        "the offline verifier start",
    )
    sentinel_index = _find_trace(
        trace,
        _sentinel_assertion,
        start_index + 1,
        "the verifier sentinel assertion",
    )
    keep_index = _find_trace(
        trace,
        lambda unit: unit == "KEEP_T4_IMAGE=1",
        sentinel_index + 1,
        "the post-proof image handoff",
    )

    stages = (
        (remote_index, "the canonical molecule-ci remote"),
        (checkout_index, "the detached checkout"),
        (rev_index, "the fetched-ref resolution"),
        (comparison_index, "the fetched-ref equality check"),
        (lockstep_index, "the immutable pin attestation"),
        (loader_index, "the attestation parser"),
        (build_index, "the final-image build"),
        (create_index, "the verifier container"),
        (copy_index, "the verifier copy"),
        (start_index, "the verifier start"),
        (sentinel_index, "the sentinel assertion"),
        (keep_index, "the image handoff"),
    )
    for index, label in stages:
        _require_top_level(trace, index, label)
    if trace[fetch_index].controls not in (
        (),
        ("for attempt in 1 2 3; do",),
    ):
        raise OfficialConsumerContractError(
            "workflow ordered executable proof hides the exact-ref fetch behind shell control flow"
        )

    stage_indexes = {index for index, _ in stages} | {fetch_index}
    for step_index in {trace[index].step_index for index in stage_indexes}:
        condition = _normalize_condition(proof_steps[step_index].get("if"))
        if condition in _FORK_GUARDS or (
            job_condition not in _NON_FORK_GUARDS and condition not in _NON_FORK_GUARDS
        ):
            raise OfficialConsumerContractError(
                "workflow ordered executable proof stage lacks an exact non-fork guard"
            )
        units = _script_units(scripts[step_index])
        if not units or units[0].text != "set -euo pipefail":
            raise OfficialConsumerContractError(
                "ordered executable proof step does not enable fail-closed shell mode"
            )

    if any(
        _is_docker_image_mutation(unit.text)
        for unit in trace[build_index + 1 : create_index]
    ):
        raise OfficialConsumerContractError(
            "verifier does not use the same final image that was built"
        )

    _validate_pre_sentinel_shell(trace, loader_index, sentinel_index)
    _validate_no_privileged_pre_sentinel(
        trace,
        sentinel_index,
        frozenset({build_index, create_index, copy_index, start_index}),
    )
    _validate_verifier_container_binding(trace, create_index, copy_index, start_index)
    _, start_log = _start_bindings(trace[start_index].text)
    sentinel_log = _validate_sentinel_assertion(trace, sentinel_index)
    start_log_name = _variable_token_name(start_log, "verifier log")
    sentinel_log_name = _variable_token_name(sentinel_log, "sentinel log")
    if _binding_snapshot(trace, start_index, start_log_name) != _binding_snapshot(
        trace, sentinel_index, sentinel_log_name
    ):
        raise OfficialConsumerContractError(
            "workflow sentinel assertion reads a different verifier log"
        )

    build_step = trace[build_index].step_index
    create_step = trace[create_index].step_index
    if "$T4_TAG" not in _tokens(trace[create_index].text, "docker create"):
        raise OfficialConsumerContractError(
            "verifier does not use the same final image that was built"
        )
    _validate_tag_binding(scripts[build_step], trace[build_index].text)
    _validate_tag_binding(scripts[create_step], trace[create_index].text)
    checker_root = _validate_lockstep_checker(trace, remote_index, lockstep_index)
    verifier_root = _validate_verifier_copy(trace, remote_index, copy_index)
    _validate_reviewed_tool_content_seal(trace, lockstep_index, checker_root)
    _validate_reviewed_tool_content_seal(trace, copy_index, verifier_root)
    _validate_attestation_flow(trace, lockstep_index, loader_index, start_index)
    loader_root = _validate_attestation_loader(
        trace, remote_index, loader_index, start_index
    )
    loader_guard_index = loader_index - 1
    if (
        loader_guard_index >= 0
        and re.fullmatch(r'[A-Z_][A-Z0-9_]*="\$\(', trace[loader_guard_index].text)
        is not None
    ):
        loader_guard_index -= 1
    try:
        _validate_reviewed_tool_content_seal(trace, loader_guard_index, loader_root)
    except OfficialConsumerContractError as exc:
        raise OfficialConsumerContractError(
            "workflow reviewed attestation loader content seal is missing or stale"
        ) from exc


def _validate_workflow(workflow_text: str) -> None:
    workflow = _strict_workflow_load(workflow_text)
    _validate_triggers(workflow)
    if "defaults" in workflow:
        raise OfficialConsumerContractError(
            "workflow proof cannot use a custom shell default"
        )
    jobs = _mapping(workflow.get("jobs"), "workflow jobs")
    if _PROOF_JOB not in jobs:
        raise OfficialConsumerContractError(
            "workflow is missing the t4-conformance proof job"
        )
    proof_job = _mapping(jobs[_PROOF_JOB], "t4-conformance proof job")
    _validate_permissions(workflow, proof_job)
    _validate_safe_environment(workflow, "workflow")
    _validate_safe_environment(proof_job, "t4-conformance proof job")
    if "defaults" in proof_job:
        raise OfficialConsumerContractError(
            "t4-conformance proof cannot use a custom shell default"
        )
    if "container" in proof_job or "services" in proof_job:
        raise OfficialConsumerContractError(
            "t4-conformance proof job cannot use a container or services"
        )
    runners = proof_job.get("runs-on")
    runner_is_exact = runners == "docker-host" or (
        isinstance(runners, list)
        and len(runners) == 2
        and all(isinstance(runner, str) for runner in runners)
        and set(runners) == {"ubuntu-latest", "docker-host"}
    )
    if not runner_is_exact:
        raise OfficialConsumerContractError(
            "t4-conformance proof job has unsupported runner labels; "
            "docker-host is required"
        )
    if "validate-static" not in _needs(
        proof_job.get("needs"), "t4-conformance proof job"
    ):
        raise OfficialConsumerContractError(
            "t4-conformance proof job must depend on validate-static"
        )
    if _continue_on_error_is_masking(proof_job):
        raise OfficialConsumerContractError(
            "t4-conformance proof job uses continue-on-error"
        )

    proof_steps = _steps(proof_job, "t4-conformance proof job")
    job_condition = _normalize_condition(proof_job.get("if"))
    if job_condition is not None and job_condition not in _NON_FORK_GUARDS:
        raise OfficialConsumerContractError(
            "t4-conformance proof job has an unsupported fork guard"
        )
    proof_step_indexes: set[int] = set()
    checkout_indexes: list[int] = []
    proof_markers = (
        "mcp_pin_lockstep.py",
        "docker build",
        "docker create",
        "mcp-built-image-e2e:sentinel:executed",
    )
    for step_index, step in enumerate(proof_steps):
        _validate_safe_environment(step, "t4-conformance proof step")
        if "shell" in step:
            raise OfficialConsumerContractError(
                "t4-conformance proof cannot use a custom shell"
            )
        if _continue_on_error_is_masking(step):
            raise OfficialConsumerContractError(
                "t4-conformance proof step uses continue-on-error"
            )
        condition = _normalize_condition(step.get("if"))
        if condition is not None and condition not in _NON_FORK_GUARDS | _FORK_GUARDS:
            raise OfficialConsumerContractError(
                "t4-conformance proof step has an unsupported fork guard"
            )
        has_action = "uses" in step
        has_script = "run" in step
        if has_action and has_script:
            raise OfficialConsumerContractError(
                "t4-conformance proof step cannot combine uses and run"
            )
        if job_condition not in _NON_FORK_GUARDS:
            is_unconditional_checkout = (
                has_action
                and step.get("uses") == _CHECKOUT_ACTION
                and condition is None
                and step_index == 0
            )
            if has_action and not is_unconditional_checkout:
                if condition in _FORK_GUARDS:
                    raise OfficialConsumerContractError(
                        "t4-conformance fork-executable step is not a strict inert notice"
                    )
                if condition not in _NON_FORK_GUARDS:
                    raise OfficialConsumerContractError(
                        "t4-conformance fork-executable step lacks an exact non-fork guard"
                    )
            if has_script and condition in _FORK_GUARDS:
                if not _strict_inert_fork_notice(step):
                    raise OfficialConsumerContractError(
                        "t4-conformance fork-executable step is not a strict inert notice"
                    )
            elif has_script and condition not in _NON_FORK_GUARDS:
                raise OfficialConsumerContractError(
                    "t4-conformance fork-executable step lacks an exact non-fork guard"
                )
        if has_action:
            _validate_checkout_step(step)
            checkout_indexes.append(step_index)
        script = step.get("run")
        executable = (
            "\n".join(_executable_lines(script)) if isinstance(script, str) else ""
        )
        bears_proof = any(marker in executable for marker in proof_markers)
        if bears_proof:
            proof_step_indexes.add(step_index)
            if condition in _FORK_GUARDS:
                raise OfficialConsumerContractError(
                    "t4-conformance proof runs only on a fork"
                )
            if (
                job_condition not in _NON_FORK_GUARDS
                and condition not in _NON_FORK_GUARDS
            ):
                raise OfficialConsumerContractError(
                    "t4-conformance proof step lacks an exact non-fork guard"
                )
    if checkout_indexes != [0]:
        raise OfficialConsumerContractError(
            "t4-conformance proof must begin with exactly one immutable allowlisted action"
        )
    if not proof_step_indexes:
        raise OfficialConsumerContractError(
            "t4-conformance proof job contains no executable proof"
        )

    _validate_dependency_graph(jobs)
    _validate_ref_scopes(workflow, jobs, proof_steps)
    _validate_ordered_proof(proof_steps, proof_step_indexes, job_condition)


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


def _validate_test_contract(consumer: str, test_source: str) -> None:
    """Check regression-file identity only; workflow semantics are authoritative."""

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
    for name in _REQUIRED_TESTS[consumer]:
        definitions = [
            node
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ]
        if not definitions:
            raise OfficialConsumerContractError(
                "test contract is missing a required regression test"
            )
        if (
            len(definitions) != 1
            or not isinstance(definitions[0], ast.FunctionDef)
            or definitions[0].decorator_list
        ):
            raise OfficialConsumerContractError(
                "required regression test is not a plain synchronous function"
            )


def validate_contract(
    consumer: str, workflow_payload: bytes, test_payload: bytes
) -> None:
    """Validate one official consumer's workflow and regression marker as data."""

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
        description="validate semantic final-image MCP wiring in an official consumer"
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
