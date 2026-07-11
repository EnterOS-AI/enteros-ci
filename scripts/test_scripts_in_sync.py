"""Test that the frozen public mirror `.molecule-ci/scripts/` is byte-identical
to its canonical SSOT in `scripts/`.

This is the pytest twin of scripts/check-scripts-in-sync.sh. The shell guard
runs in the dedicated `Scripts Sync` workflow (path-filtered); this test runs in
the always-on `pytest scripts/` job, so a rename or move that dodges the
workflow's `paths:` filter still cannot ship drift silently.

`scripts/` is canonical (the meta-CI router and molecule-ci's own ci.yml invoke
`scripts/...`). `.molecule-ci/scripts/` is a byte-identical mirror pinned by
external org-template/plugin consumers, so it cannot simply be deleted. Every
file under the mirror MUST have a byte-identical twin in `scripts/`. Reconcile
drift by copying FROM `scripts/` TO `.molecule-ci/scripts/`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CANON_DIR = REPO_ROOT / "scripts"
MIRROR_DIR = REPO_ROOT / ".molecule-ci" / "scripts"


def _mirror_files() -> list[Path]:
    return sorted(
        p
        for p in MIRROR_DIR.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix != ".pyc"
    )


def test_mirror_dir_exists_and_nonempty():
    assert MIRROR_DIR.is_dir(), f"{MIRROR_DIR} is missing — external consumers pin it."
    assert _mirror_files(), f"{MIRROR_DIR} is unexpectedly empty."


@pytest.mark.parametrize(
    "mirror_file", _mirror_files(), ids=lambda p: p.name
)
def test_mirror_file_matches_canonical(mirror_file: Path):
    canon_file = CANON_DIR / mirror_file.name
    assert canon_file.is_file(), (
        f".molecule-ci/scripts/{mirror_file.name} has no canonical twin in "
        f"scripts/ — add it to scripts/ (the SSOT) so the mirror derives from it."
    )
    assert canon_file.read_bytes() == mirror_file.read_bytes(), (
        f".molecule-ci/scripts/{mirror_file.name} has drifted from "
        f"scripts/{mirror_file.name}. Reconcile by copying FROM scripts/ TO "
        f".molecule-ci/scripts/ (scripts/ is canonical)."
    )
