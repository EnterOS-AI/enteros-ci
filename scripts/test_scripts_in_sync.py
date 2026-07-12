"""Test that the frozen public mirror `.molecule-ci/scripts/` is byte-identical
to its canonical SSOT in `scripts/`, per `.molecule-ci/scripts/MIRROR.manifest`.

This is the pytest twin of scripts/check-scripts-in-sync.sh (which delegates to
scripts/sync-scripts.sh --check). The shell guard runs in the dedicated
`Scripts Sync` workflow (path-filtered); this test runs in the always-on
`pytest scripts/` job, so a rename or move that dodges the workflow's `paths:`
filter still cannot ship drift silently.

`scripts/` is canonical. `.molecule-ci/scripts/` is a byte-identical mirror
pinned by external org-template/plugin consumers, so it cannot simply be
deleted. `MIRROR.manifest` is the explicit authoritative set of vendored files.
The invariant is enforced in BOTH directions:
  * every manifest entry exists in scripts/ and is byte-identical in the mirror;
  * every mirror file (other than the manifest) is declared in the manifest.
Reconcile any drift by running `bash scripts/sync-scripts.sh`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CANON_DIR = REPO_ROOT / "scripts"
MIRROR_DIR = REPO_ROOT / ".molecule-ci" / "scripts"
MANIFEST = MIRROR_DIR / "MIRROR.manifest"


def _manifest_entries() -> list[str]:
    names: list[str] = []
    for raw in MANIFEST.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def _mirror_files() -> list[Path]:
    return sorted(
        p
        for p in MIRROR_DIR.rglob("*")
        if p.is_file()
        and p.name != "MIRROR.manifest"
        and "__pycache__" not in p.parts
        and p.suffix != ".pyc"
    )


def test_mirror_dir_exists_and_nonempty():
    assert MIRROR_DIR.is_dir(), f"{MIRROR_DIR} is missing — external consumers pin it."
    assert _mirror_files(), f"{MIRROR_DIR} is unexpectedly empty."


def test_manifest_exists_and_nonempty():
    assert MANIFEST.is_file(), f"{MANIFEST} is missing — it declares the vendored surface."
    assert _manifest_entries(), f"{MANIFEST} lists no files."


@pytest.mark.parametrize("name", _manifest_entries(), ids=lambda n: n)
def test_manifest_entry_is_mirrored_from_canonical(name: str):
    """Every manifest entry must exist in scripts/ and be byte-identical in the
    mirror. Catches inverse drift: a declared-vendored script never copied in."""
    canon_file = CANON_DIR / name
    mirror_file = MIRROR_DIR / name
    assert canon_file.is_file(), (
        f"MIRROR.manifest lists '{name}' but scripts/{name} does not exist "
        f"(scripts/ is the SSOT)."
    )
    assert mirror_file.is_file(), (
        f"MIRROR.manifest lists '{name}' but .molecule-ci/scripts/{name} is "
        f"missing. Run `bash scripts/sync-scripts.sh`."
    )
    assert canon_file.read_bytes() == mirror_file.read_bytes(), (
        f".molecule-ci/scripts/{name} has drifted from scripts/{name}. "
        f"Reconcile by running `bash scripts/sync-scripts.sh`."
    )


@pytest.mark.parametrize("mirror_file", _mirror_files(), ids=lambda p: p.name)
def test_mirror_file_is_declared_in_manifest(mirror_file: Path):
    """Every mirror file must be declared in MIRROR.manifest — no stale or
    hand-added vendored file that dodges the generator."""
    assert mirror_file.name in _manifest_entries(), (
        f".molecule-ci/scripts/{mirror_file.name} is present but not listed in "
        f"MIRROR.manifest — add it to the manifest or remove the file."
    )
