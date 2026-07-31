#!/usr/bin/env python3
"""Unit tests for scripts/check_crlf_in_index.py.

The reject arms are the point. A guard whose failing path is never exercised is
indistinguishable from `exit 0`, so every test below that expects rc=1 is doing
the real work; the passing cases only prove it is not a blanket `exit 1`.

Each case builds a throwaway git repo and commits real blobs, because the whole
value of this check is what git actually STORED — asserting against a mocked
`ls-files` would test the mock.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_crlf_in_index as guard  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def new_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    # Store bytes verbatim so the test controls exactly what lands in the index,
    # regardless of the config on the machine running the suite.
    git(repo, "config", "core.autocrlf", "false")
    return repo


def commit(repo: Path, name: str, data: bytes) -> None:
    (repo / name).write_bytes(data)
    git(repo, "add", "--", name)
    git(repo, "commit", "-qm", f"add {name}")


class CrlfGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = new_repo(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- clean cases: must PASS -------------------------------------------
    def test_lf_only_repo_passes(self) -> None:
        commit(self.repo, "a.py", b"x = 1\ny = 2\n")
        self.assertEqual(guard.find_offenders(str(self.repo)), [])

    def test_empty_repo_passes(self) -> None:
        self.assertEqual(guard.find_offenders(str(self.repo)), [])

    def test_binary_blob_with_cr_bytes_is_not_flagged(self) -> None:
        """A PNG-ish blob contains CR bytes but is not CRLF text."""
        commit(self.repo, "img.bin", b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")
        self.assertEqual(guard.find_offenders(str(self.repo)), [])

    # ---- the arm that matters: must FAIL -----------------------------------
    def test_crlf_blob_is_flagged(self) -> None:
        commit(self.repo, "bad.py", b"x = 1\r\ny = 2\r\n")
        self.assertEqual(guard.find_offenders(str(self.repo)), ["bad.py"])

    def test_mixed_endings_blob_is_flagged(self) -> None:
        commit(self.repo, "mixed.py", b"x = 1\r\ny = 2\n")
        self.assertEqual(guard.find_offenders(str(self.repo)), ["mixed.py"])

    def test_offenders_are_reported_by_path_and_sorted(self) -> None:
        commit(self.repo, "b.py", b"b\r\n")
        commit(self.repo, "a.py", b"a\r\n")
        self.assertEqual(guard.find_offenders(str(self.repo)), ["a.py", "b.py"])

    # ---- declared intent: must PASS ---------------------------------------
    def test_declared_eol_crlf_is_exempt(self) -> None:
        commit(self.repo, ".gitattributes", b"win.bat eol=crlf\n")
        commit(self.repo, "win.bat", b"@echo off\r\n")
        self.assertEqual(guard.find_offenders(str(self.repo)), [])

    def test_declared_minus_text_is_exempt(self) -> None:
        commit(self.repo, ".gitattributes", b"fixture.crlf -text\n")
        commit(self.repo, "fixture.crlf", b"line\r\n")
        self.assertEqual(guard.find_offenders(str(self.repo)), [])

    def test_exemption_is_per_path_not_repo_wide(self) -> None:
        """Declaring one file CRLF must not silence a DIFFERENT contaminated file."""
        commit(self.repo, ".gitattributes", b"win.bat eol=crlf\n")
        commit(self.repo, "win.bat", b"@echo off\r\n")
        commit(self.repo, "leaked.py", b"x = 1\r\n")
        self.assertEqual(guard.find_offenders(str(self.repo)), ["leaked.py"])

    # ---- exit codes + sentinel --------------------------------------------
    def test_main_returns_zero_and_prints_sentinel_when_clean(self) -> None:
        commit(self.repo, "a.py", b"ok\n")
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "check_crlf_in_index.py"),
             str(self.repo)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(guard.SENTINEL, proc.stdout)

    def test_main_returns_one_and_names_the_file_when_dirty(self) -> None:
        commit(self.repo, "bad.py", b"x\r\n")
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "check_crlf_in_index.py"),
             str(self.repo)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bad.py", proc.stderr)
        # A failing run must NOT emit the success sentinel, or a workflow that
        # greps for it would treat a red run as proof of a green one.
        self.assertNotIn(guard.SENTINEL, proc.stdout)

    def test_non_repo_fails_closed(self) -> None:
        """An unreadable index must never be reported as clean."""
        plain = self.tmp / "notarepo"
        plain.mkdir()
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "check_crlf_in_index.py"),
             str(plain)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(guard.SENTINEL, proc.stdout)


if __name__ == "__main__":
    unittest.main()
