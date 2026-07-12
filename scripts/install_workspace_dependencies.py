#!/usr/bin/env python3
"""Install a workspace runtime without letting public indexes select it."""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from packaging.utils import canonicalize_name, parse_wheel_filename

from requirements_contract import (
    PRIVATE_INDEX_URL,
    RUNTIME_PROJECT,
    RequirementsContractError,
    inspect_requirements,
)


def build_download_command(
    runtime_requirement: str,
    destination: Path,
    *,
    python: str = sys.executable,
) -> list[str]:
    """Build the private-only, dependency-free wheel acquisition command."""
    return [
        python,
        "-m",
        "pip",
        "download",
        "--isolated",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--no-deps",
        "--index-url",
        PRIVATE_INDEX_URL,
        "--dest",
        str(destination),
        runtime_requirement,
    ]


def select_runtime_wheel(directory: Path) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            f"private runtime acquisition produced {len(wheels)} wheels; expected exactly one"
        )
    name, _, _, _ = parse_wheel_filename(wheels[0].name)
    if canonicalize_name(name) != canonicalize_name(RUNTIME_PROJECT):
        raise RuntimeError(
            f"private acquisition returned {name!s}, not canonical runtime {RUNTIME_PROJECT}"
        )
    return wheels[0]


def build_install_command(
    wheel: Path,
    requirements: Path | None,
    *,
    python: str = sys.executable,
    break_system_packages: bool = False,
) -> list[str]:
    """Pin the local wheel while resolving its public dependencies normally."""
    command = [
        python,
        "-m",
        "pip",
        "install",
        "--isolated",
        "--disable-pip-version-check",
        "--quiet",
    ]
    if break_system_packages:
        command.append("--break-system-packages")
    command.append(str(wheel))
    if requirements is not None:
        command.extend(("-r", str(requirements)))
    return command


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, default=Path("requirements.txt"))
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--break-system-packages", action="store_true")
    args = parser.parse_args()

    requirements = args.requirements.resolve()
    if requirements.is_file():
        try:
            contract = inspect_requirements(requirements, root=Path.cwd())
        except RequirementsContractError as exc:
            parser.error(str(exc))
        runtime_requirement = contract.runtime_requirement
        install_requirements: Path | None = requirements
    elif args.allow_missing:
        runtime_requirement = RUNTIME_PROJECT
        install_requirements = None
    else:
        parser.error(f"requirements file not found: {requirements}")

    with tempfile.TemporaryDirectory(prefix="molecule-runtime-wheel-") as tmp:
        destination = Path(tmp)
        subprocess.run(
            build_download_command(runtime_requirement, destination),
            check=True,
        )
        wheel = select_runtime_wheel(destination)
        subprocess.run(
            build_install_command(
                wheel,
                install_requirements,
                break_system_packages=args.break_system_packages,
            ),
            check=True,
        )


if __name__ == "__main__":
    main()
