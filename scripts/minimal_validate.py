#!/usr/bin/env python3
"""Repository baseline validator used by inline Gitea consumer workflows."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


SENTINEL = "minimal-validate:sentinel:executed"
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "target",
    ".next",
    ".turbo",
    ".cache",
    "coverage",
    "htmlcov",
    ".molecule-ci",
    ".molecule-ci-canonical",
    "vendor",
    "fixtures",
    "testdata",
    "test-data",
    "__fixtures__",
    "__snapshots__",
    ".gradle",
    ".idea",
    ".vscode-test",
    "site-packages",
}
README_NAMES = {
    "readme",
    "readme.md",
    "readme.rst",
    "readme.txt",
    "readme.markdown",
}
JSONC_HINTS = re.compile(
    r"(^|/)(tsconfig[^/]*\.json|devcontainer\.json|.*\.jsonc)$", re.I
)


def _strip_jsonc(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(^|\s)//[^\n]*", r"\1", text)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def validate(root: Path, *, readme_required: bool) -> tuple[dict[str, int], list[str], list[str]]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - CI uses Python 3.11+
        tomllib = None

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - workflow installs PyYAML
        raise RuntimeError("pyyaml is required") from exc

    checked = {"yaml": 0, "json": 0, "toml": 0, "manifest": 0}
    errors: list[str] = []
    warnings: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            relative = str(path.relative_to(root))
            suffix = path.suffix.lower()
            try:
                if suffix in {".yml", ".yaml"}:
                    checked["yaml"] += 1
                    list(yaml.safe_load_all(path.read_bytes()))
                elif suffix == ".json":
                    checked["json"] += 1
                    raw = path.read_text(encoding="utf-8", errors="replace")
                    try:
                        json.loads(raw)
                    except json.JSONDecodeError as exc:
                        if JSONC_HINTS.search(relative.replace(os.sep, "/")):
                            try:
                                json.loads(_strip_jsonc(raw))
                                warnings.append(f"JSONC tolerated: {relative}")
                            except json.JSONDecodeError as jsonc_exc:
                                warnings.append(
                                    f"JSONC still unparseable, not failing: {relative}: {jsonc_exc}"
                                )
                        else:
                            errors.append(f"JSON parse error: {relative}: {exc}")
                elif suffix == ".toml" and tomllib is not None:
                    checked["toml"] += 1
                    with path.open("rb") as handle:
                        tomllib.load(handle)
            except Exception as exc:  # parsing and unreadable-file failures are hard failures
                kind = "YAML" if suffix in {".yml", ".yaml"} else suffix.lstrip(".").upper()
                kind = kind or "file"
                errors.append(f"{kind} parse error: {relative}: {str(exc).splitlines()[0]}")

    root_files = {child.name.lower() for child in root.iterdir() if child.is_file()}
    if readme_required and not (root_files & README_NAMES):
        errors.append("No README at repo root")

    candidates = [
        root / name
        for name in (
            "plugin.yaml",
            "plugin.yml",
            "plugin.json",
            "manifest.yaml",
            "manifest.yml",
            "manifest.json",
        )
        if (root / name).is_file()
    ]
    claude_manifest = root / ".claude-plugin" / "plugin.json"
    if claude_manifest.is_file():
        candidates.append(claude_manifest)

    for path in candidates:
        checked["manifest"] += 1
        relative = str(path.relative_to(root))
        try:
            if path.suffix.lower() == ".json":
                manifest = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            else:
                manifest = yaml.safe_load(path.read_bytes())
        except Exception as exc:
            errors.append(f"Manifest unparseable: {relative}: {str(exc).splitlines()[0]}")
            continue
        if not isinstance(manifest, dict):
            errors.append(f"Manifest is not a mapping/object: {relative}")
        elif not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
            errors.append(f"Manifest missing non-empty string 'name': {relative}")

    return checked, warnings, errors


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0] if args else os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    if not root.is_dir():
        print(f"::error::repository root is not a directory: {root}")
        return 2
    readme_required = os.environ.get("README_REQUIRED", "true").strip().lower() != "false"
    try:
        checked, warnings, errors = validate(root, readme_required=readme_required)
    except RuntimeError as exc:
        print(f"::error::{exc}")
        return 3

    print(SENTINEL)
    print(
        "minimal-validate: checked "
        + " ".join(f"{name}={count}" for name, count in checked.items())
    )
    for warning in warnings:
        print(f"::warning::{warning}")
    for error in errors:
        print(f"::error::{error}")
    if errors:
        print(f"minimal-validate FAILED with {len(errors)} problem(s).")
        return 1
    print("minimal-validate PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
