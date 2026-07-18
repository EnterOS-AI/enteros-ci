from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).with_name("scan_diff_secrets.py")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "README.md").write_text("# test\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def _scan(repo: Path, base: str, head: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--base",
            base,
            "--head",
            head,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_clean_diff_passes_and_emits_sentinel(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "safe.txt").write_text("TOKEN comes from Infisical\n")
    _git(repo, "add", "safe.txt")
    _git(repo, "commit", "-qm", "safe")
    result = _scan(repo, base, _git(repo, "rev-parse", "HEAD"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "secret-scan:sentinel:executed" in result.stdout


def test_secret_fails_without_echoing_value(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    secret = "ghp_" + "A" * 40
    (repo / "credential.txt").write_text(f"TOKEN={secret}\n")
    _git(repo, "add", "credential.txt")
    _git(repo, "commit", "-qm", "unsafe")
    result = _scan(repo, base, _git(repo, "rev-parse", "HEAD"))
    assert result.returncode == 1
    assert "credential.txt (GitHub classic PAT)" in result.stdout
    assert secret not in result.stdout


def test_unreachable_base_falls_back_to_entire_head_tree(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    (repo / "nested file.txt").write_text("sk-cp-" + "B" * 64 + "\n")
    _git(repo, "add", "nested file.txt")
    _git(repo, "commit", "-qm", "unsafe")
    result = _scan(repo, "f" * 40, _git(repo, "rev-parse", "HEAD"))
    assert result.returncode == 1
    assert "nested file.txt (MiniMax API key)" in result.stdout


def test_deleted_secret_is_not_a_new_leak(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    (repo / "old.txt").write_text("ghs_" + "C" * 40 + "\n")
    _git(repo, "add", "old.txt")
    _git(repo, "commit", "-qm", "old")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "old.txt").unlink()
    _git(repo, "add", "old.txt")
    _git(repo, "commit", "-qm", "remove")
    assert _scan(repo, base, _git(repo, "rev-parse", "HEAD")).returncode == 0


def test_secret_added_while_renaming_file_is_scanned(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    original = repo / "safe.txt"
    original.write_text("ordinary content\n" * 100)
    _git(repo, "add", "safe.txt")
    _git(repo, "commit", "-qm", "add safe file")
    base = _git(repo, "rev-parse", "HEAD")

    secret = "ghp_" + "D" * 40
    original.rename(repo / "renamed.txt")
    with (repo / "renamed.txt").open("a") as handle:
        handle.write(f"TOKEN={secret}\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "rename and modify")
    head = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "diff", "--name-status", base, head).startswith("R")

    result = _scan(repo, base, head)
    assert result.returncode == 1
    assert "renamed.txt (GitHub classic PAT)" in result.stdout
    assert secret not in result.stdout
