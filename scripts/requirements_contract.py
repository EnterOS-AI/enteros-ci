"""Parse workspace requirements at the private-runtime trust boundary."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)

RUNTIME_PROJECT = "molecules-workspace-runtime"
RETIRED_RUNTIME_PROJECT = "molecule-ai-workspace-runtime"
PRIVATE_INDEX_URL = (
    "https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/"
)

_RUNTIME_NAME = canonicalize_name(RUNTIME_PROJECT)
_RETIRED_RUNTIME_NAME = canonicalize_name(RETIRED_RUNTIME_PROJECT)
_INCLUDE_RE = re.compile(r"^(?P<option>-r|--requirement|-c|--constraint)(?:\s+|=)(?P<value>.+)$")
_EXTRA_INDEX_RE = re.compile(r"^--extra-index-url(?:\s+|=)(?P<value>.+)$")
_SOURCE_OPTIONS = (
    "--index-url",
    "-i",
    "--find-links",
    "-f",
    "--trusted-host",
    "--no-index",
)


class RequirementsContractError(ValueError):
    """Raised when requirements can escape the private-runtime contract."""


@dataclass(frozen=True)
class RequirementsContract:
    runtime_requirement: str
    files: tuple[Path, ...]


def _one_shell_value(value: str, *, context: str) -> str:
    try:
        parts = shlex.split(value)
    except ValueError as exc:
        raise RequirementsContractError(f"{context}: invalid quoting: {exc}") from exc
    if len(parts) != 1:
        raise RequirementsContractError(f"{context}: expected exactly one value")
    return parts[0]


def _source_project_name(value: str) -> str | None:
    """Recover a legacy VCS egg or archive project name when possible."""
    parsed = urlparse(value)
    fragment = parse_qs(unquote(parsed.fragment), keep_blank_values=True)
    eggs = fragment.get("egg", [])
    if eggs:
        return canonicalize_name(eggs[0])

    filename = Path(unquote(parsed.path)).name
    if not filename:
        return None
    try:
        name, _, _, _ = parse_wheel_filename(filename)
        return canonicalize_name(name)
    except InvalidWheelFilename:
        pass
    try:
        name, _ = parse_sdist_filename(filename)
        return canonicalize_name(name)
    except InvalidSdistFilename:
        return None


def inspect_requirements(
    path: Path,
    *,
    root: Path | None = None,
) -> RequirementsContract:
    """Validate nested pip requirements and return the one runtime spec.

    Includes are recursively inspected but confined to ``root``. Package-source
    overrides are fail-closed except for the known Gitea extra index. Direct,
    VCS, archive, local, editable, and continued requirements are rejected when
    their provenance cannot be established from a normal PEP 508 declaration.
    """
    root = (root or path.parent).resolve()
    start = path.resolve()
    errors: list[str] = []
    runtime_requirements: list[str] = []
    visited: list[Path] = []
    seen: set[Path] = set()

    def record_requirement(raw: str, *, source: Path, line_number: int, constraint: bool) -> None:
        where = f"{source.relative_to(root)}:{line_number}"
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            source_name = _source_project_name(raw)
            if source_name == _RETIRED_RUNTIME_NAME:
                errors.append(
                    f"{where}: retired runtime distribution `{RETIRED_RUNTIME_PROJECT}` "
                    "must not be installed"
                )
            elif source_name == _RUNTIME_NAME:
                errors.append(
                    f"{where}: the canonical runtime must be resolved by name from "
                    "the private index, not from a direct/VCS/archive source"
                )
            else:
                errors.append(
                    f"{where}: unsupported direct, VCS, local, editable, or invalid "
                    f"requirement {raw!r}"
                )
            return

        name = canonicalize_name(requirement.name)
        if name == _RETIRED_RUNTIME_NAME:
            errors.append(
                f"{where}: retired runtime distribution `{RETIRED_RUNTIME_PROJECT}` "
                "must not be installed"
            )
            return
        if name != _RUNTIME_NAME:
            if requirement.url:
                source_name = _source_project_name(requirement.url)
                if source_name in {_RUNTIME_NAME, _RETIRED_RUNTIME_NAME}:
                    errors.append(
                        f"{where}: requirement name and runtime source metadata disagree"
                    )
                else:
                    errors.append(
                        f"{where}: unsupported direct, VCS, or local requirement "
                        f"for {requirement.name!r}"
                    )
            return
        if constraint:
            errors.append(
                f"{where}: `{RUNTIME_PROJECT}` must be a requirement, not only a constraint"
            )
            return
        if requirement.url:
            errors.append(
                f"{where}: `{RUNTIME_PROJECT}` must be resolved from the private index; "
                "direct URLs are not allowed"
            )
            return
        if requirement.extras or requirement.marker:
            errors.append(
                f"{where}: `{RUNTIME_PROJECT}` must not use extras or environment markers"
            )
            return
        runtime_requirements.append(f"{RUNTIME_PROJECT}{requirement.specifier}")

    def visit(candidate: Path, *, constraint: bool = False) -> None:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            errors.append(f"requirements include escapes repository root: {candidate}")
            return
        if resolved in seen:
            return
        seen.add(resolved)
        visited.append(resolved)
        if not resolved.is_file():
            errors.append(f"requirements include does not exist: {resolved.relative_to(root)}")
            return

        for line_number, raw_line in enumerate(resolved.read_text().splitlines(), 1):
            where = f"{resolved.relative_to(root)}:{line_number}"
            if raw_line.rstrip().endswith("\\"):
                errors.append(
                    f"{where}: backslash continuation is unsupported at this trust boundary"
                )
                continue
            line = re.split(r"(?<!\S)#", raw_line, maxsplit=1)[0].strip()
            if not line:
                continue

            include = _INCLUDE_RE.match(line)
            if include:
                try:
                    value = _one_shell_value(
                        include.group("value"),
                        context=f"{where} requirements include",
                    )
                except RequirementsContractError as exc:
                    errors.append(str(exc))
                    continue
                parsed = urlparse(value)
                if parsed.scheme or parsed.netloc:
                    errors.append(f"{where}: remote requirements include is not allowed: {value}")
                    continue
                visit(
                    resolved.parent / value,
                    constraint=include.group("option") in {"-c", "--constraint"},
                )
                continue

            extra_index = _EXTRA_INDEX_RE.match(line)
            if extra_index:
                try:
                    value = _one_shell_value(
                        extra_index.group("value"),
                        context=f"{where} package source",
                    )
                except RequirementsContractError as exc:
                    errors.append(str(exc))
                    continue
                if value.rstrip("/") != PRIVATE_INDEX_URL.rstrip("/"):
                    errors.append(f"{where}: untrusted package source: {value}")
                continue

            if line.startswith(_SOURCE_OPTIONS):
                errors.append(f"{where}: unsupported package source option: {line}")
                continue
            if line.startswith(("-e ", "--editable ", "-e=", "--editable=")):
                errors.append(f"{where}: unsupported editable requirement: {line}")
                continue
            if line.startswith("-"):
                errors.append(f"{where}: unsupported pip requirement option: {line}")
                continue
            record_requirement(
                line,
                source=resolved,
                line_number=line_number,
                constraint=constraint,
            )

    visit(start)
    if len(runtime_requirements) != 1:
        if not runtime_requirements:
            errors.append(
                f"requirements.txt must declare `{RUNTIME_PROJECT}` exactly once"
            )
        else:
            errors.append(
                f"requirements files must declare `{RUNTIME_PROJECT}` exactly once; "
                f"found {len(runtime_requirements)}"
            )
    if errors:
        raise RequirementsContractError("\n".join(errors))
    return RequirementsContract(runtime_requirements[0], tuple(visited))
