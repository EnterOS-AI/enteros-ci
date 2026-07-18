#!/usr/bin/env python3
"""Fail closed when changed Git content contains credential-shaped strings."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


SENTINEL = "secret-scan:sentinel:executed"
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GitHub classic PAT", re.compile(r"ghp_[A-Za-z0-9]{36,}")),
    ("GitHub App token", re.compile(r"ghs_[A-Za-z0-9]{36,}")),
    ("GitHub OAuth token", re.compile(r"gh[our]_[A-Za-z0-9]{36,}")),
    ("GitHub fine-grained PAT", re.compile(r"github_pat_[A-Za-z0-9_]{82,}")),
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_-]{40,}")),
    ("OpenAI project key", re.compile(r"sk-proj-[A-Za-z0-9_-]{40,}")),
    ("OpenAI service key", re.compile(r"sk-svcacct-[A-Za-z0-9_-]{40,}")),
    ("MiniMax API key", re.compile(r"sk-cp-[A-Za-z0-9_-]{60,}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}")),
    ("AWS access key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
)
SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        timeout=45,
    )


def _usable_sha(value: str) -> bool:
    return bool(value and not set(value) <= {"0"} and SHA.fullmatch(value))


def _ensure_commit(repo: Path, value: str) -> bool:
    if not _usable_sha(value):
        return False
    if _git(repo, "cat-file", "-e", f"{value}^{{commit}}", check=False).returncode == 0:
        return True
    fetched = _git(repo, "fetch", "--depth=1", "origin", value, check=False)
    return fetched.returncode == 0 and _git(
        repo, "cat-file", "-e", f"{value}^{{commit}}", check=False
    ).returncode == 0


def _tree_paths(repo: Path, target: str) -> list[str]:
    result = _git(repo, "ls-tree", "-r", "--name-only", "-z", target)
    return [part.decode("utf-8", errors="surrogateescape") for part in result.stdout.split(b"\0") if part]


def _changed_paths(repo: Path, base: str, head: str) -> tuple[list[str], str]:
    head_ok = _ensure_commit(repo, head)
    target = head if head_ok else "HEAD"
    if not head_ok and _git(repo, "cat-file", "-e", "HEAD^{commit}", check=False).returncode != 0:
        raise RuntimeError("neither requested head nor checkout HEAD resolves")

    base_ok = _ensure_commit(repo, base)
    if base_ok and head_ok:
        diff = _git(
            repo,
            "diff",
            "--name-only",
            "--diff-filter=ACMRT",
            "-z",
            base,
            head,
            check=False,
        )
        if diff.returncode == 0:
            paths = [
                part.decode("utf-8", errors="surrogateescape")
                for part in diff.stdout.split(b"\0")
                if part
            ]
            return paths, target
        print("::warning::git diff failed; scanning the entire target tree")

    return _tree_paths(repo, target), target


def _content(repo: Path, path: str, target: str) -> str:
    # Read the complete resulting blob instead of parsing a textual patch. Git
    # suppresses patch bodies for binary/NUL-containing files, which would let
    # an added credential evade an added-line-only scan.
    result = _git(repo, "show", f"{target}:{path}", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"cannot read {path!r} from target tree")
    return result.stdout.decode("utf-8", errors="replace")


def scan(repo: Path, *, base: str, head: str) -> list[tuple[str, str]]:
    paths, target = _changed_paths(repo, base, head)
    findings: list[tuple[str, str]] = []
    total_bytes = 0
    for path in paths:
        size_result = _git(repo, "cat-file", "-s", f"{target}:{path}", check=False)
        if size_result.returncode != 0:
            raise RuntimeError(f"cannot determine blob size for {path!r}")
        try:
            size = int(size_result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError(f"invalid blob size for {path!r}") from exc
        total_bytes += size
        if size > MAX_FILE_BYTES or total_bytes > MAX_TOTAL_BYTES:
            raise RuntimeError(
                f"scan budget exceeded at {path!r} (file={size}, total={total_bytes})"
            )
        content = _content(repo, path, target)
        for label, pattern in PATTERNS:
            if pattern.search(content):
                findings.append((path, label))
                break
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    args = parser.parse_args(argv)
    repo = Path(args.repo_root).resolve()

    print(SENTINEL)
    try:
        findings = scan(repo, base=args.base, head=args.head)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"::error::secret scan could not establish a complete diff/tree: {exc}")
        return 2

    if findings:
        print("::error::Credential-shaped strings detected in changed content:")
        for path, label in findings:
            print(f"  {path} ({label})")
        print("Matched values are deliberately not printed. Remove and rotate any pushed credential.")
        return 1
    print("No credential-shaped strings detected in changed content.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
