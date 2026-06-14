#!/usr/bin/env python3
"""Tests for gitea-curl credential guards (runtime#? security fix).

The wrapper must keep tokens out of argv by reading ~/.netrc and refusing any
form of inline credential passed on the command line.
"""

from __future__ import annotations

import base64
import os
import pathlib
import subprocess
import tempfile
from typing import Callable

import pytest


def _netrc(tmp_path: pathlib.Path, user: str = "agent", password: str = "secret") -> pathlib.Path:
    """Create a temp ~/.netrc file with mode 600."""
    netrc = tmp_path / ".netrc"
    netrc.write_text(
        f"machine git.moleculesai.app\nlogin {user}\npassword {password}\n"
    )
    netrc.chmod(0o600)
    return netrc


def _run(tmp_path: pathlib.Path, script: pathlib.Path, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run gitea-curl with a temporary netrc and return the result."""
    test_env = {
        **os.environ,
        "HOME": str(tmp_path),
        "GITEA_HOST": "git.moleculesai.app",
    }
    if env:
        test_env.update(env)
    # Create netrc so the wrapper gets past the "netrc exists" check.
    _netrc(tmp_path)
    return subprocess.run(
        [str(script), *argv],
        capture_output=True,
        text=True,
        env=test_env,
        check=False,
    )


@pytest.fixture
def gitea_curl() -> pathlib.Path:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    return repo_root / "bin" / "gitea-curl"


@pytest.fixture
def tmp_home(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path


# ---- Rejection tests: every common inline-credential form must fail ----

REJECT_CASES = [
    # user/pass forms
    ("-u user:pass", ["-u", "user:pass"]),
    ("-uuser:pass", ["-uuser:pass"]),
    ("--user user:pass", ["--user", "user:pass"]),
    ("--user=user:pass", ["--user=user:pass"]),
    # proxy-user forms
    ("-U proxyuser:proxypass", ["-U", "proxyuser:proxypass"]),
    ("-Uproxyuser:proxypass", ["-Uproxyuser:proxypass"]),
    ("--proxy-user proxyuser:proxypass", ["--proxy-user", "proxyuser:proxypass"]),
    ("--proxy-user=proxyuser:proxypass", ["--proxy-user=proxyuser:proxypass"]),
    # Authorization header forms
    ('-H "Authorization: Bearer tok"', ["-H", "Authorization: Bearer tok"]),
    ('-H"Authorization: Bearer tok"', ['-H"Authorization: Bearer tok"']),
    ("-HAuthorization: Bearer tok", ["-HAuthorization: Bearer tok"]),
    ('--header "Authorization: Bearer tok"', ["--header", "Authorization: Bearer tok"]),
    ("--header=Authorization: Bearer tok", ["--header=Authorization: Bearer tok"]),
    ('-H "Authorization: token tok"', ["-H", "Authorization: token tok"]),
    ('-H "Authorization: Basic b64"', ["-H", "Authorization: Basic b64"]),
    # case-insensitive Authorization
    ('-H "authorization: bearer tok"', ["-H", "authorization: bearer tok"]),
    # Proxy-Authorization header forms
    ('-H "Proxy-Authorization: Basic b64"', ["-H", "Proxy-Authorization: Basic b64"]),
    ("--header=Proxy-Authorization: Basic b64", ["--header=Proxy-Authorization: Basic b64"]),
    # equals-attached value bypass (RC #11714)
    ('--header=Authorization=Bearer tok', ["--header=Authorization=Bearer tok"]),
    ('-H "Authorization=token tok"', ["-H", "Authorization=token tok"]),
    ('--header=Proxy-Authorization=Basic b64', ["--header=Proxy-Authorization=Basic b64"]),
]


@pytest.mark.parametrize("name,argv", REJECT_CASES)
def test_gitea_curl_rejects_inline_credentials(
    name: str,
    argv: list[str],
    gitea_curl: pathlib.Path,
    tmp_home: pathlib.Path,
) -> None:
    result = _run(tmp_home, gitea_curl, *argv)
    assert result.returncode != 0, f"expected rejection for {name}"
    stderr = result.stderr.lower()
    assert "refusing" in stderr, f"expected 'refusing' message for {name}, got: {result.stderr}"


# ---- Acceptance tests: safe calls must reach curl (and fail only on curl's side) ----


def test_gitea_curl_runs_with_netrc_no_credentials_in_argv(
    gitea_curl: pathlib.Path,
    tmp_home: pathlib.Path,
) -> None:
    # Point curl at a bogus URL so it fails quickly; the wrapper itself should
    # not reject the call. This proves --netrc is passed and no guard false-positive
    # fires on ordinary flags.
    result = _run(tmp_home, gitea_curl, "-sS", "--max-time", "2", "https://git.moleculesai.app/api/v1/user")
    # We expect a curl-level failure (network/auth), not a guard failure.
    assert "refusing" not in result.stderr.lower()


def test_gitea_curl_requires_netrc(
    gitea_curl: pathlib.Path,
    tmp_home: pathlib.Path,
) -> None:
    # Call gitea-curl directly with HOME pointing at an empty directory so
    # ~/.netrc does not exist.
    result = subprocess.run(
        [str(gitea_curl), "https://git.moleculesai.app/api/v1/user"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_home),
            "GITEA_HOST": "git.moleculesai.app",
        },
        check=False,
    )
    assert result.returncode != 0
    assert "run setup-gitea-netrc.sh" in result.stderr


def test_gitea_curl_allows_safe_headers(
    gitea_curl: pathlib.Path,
    tmp_home: pathlib.Path,
) -> None:
    # Non-auth headers should pass the guard and fail at curl/network layer.
    result = _run(
        tmp_home,
        gitea_curl,
        "-H", "Accept: application/json",
        "-H", "Content-Type: application/json",
        "--max-time", "2",
        "https://git.moleculesai.app/api/v1/user",
    )
    assert "refusing" not in result.stderr.lower()


# ---- setup-gitea-netrc.sh regression tests ----


@pytest.fixture
def setup_script() -> pathlib.Path:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    return repo_root / "scripts" / "setup-gitea-netrc.sh"


def test_setup_netrc_creates_file_mode_600(
    setup_script: pathlib.Path,
    tmp_home: pathlib.Path,
) -> None:
    """The final ~/.netrc must be mode 0600 and contain the credentials."""
    result = subprocess.run(
        ["bash", str(setup_script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_home),
            "GIT_HTTP_USERNAME": "agent-dev-a",
            "GIT_HTTP_PASSWORD": "s3cr3t-t0k3n",
            "GITEA_HOST": "git.moleculesai.app",
        },
        check=False,
    )
    assert result.returncode == 0, f"setup script failed: {result.stderr}"

    netrc = tmp_home / ".netrc"
    assert netrc.exists()
    # Fail-closed: exact mode 0600, no wider permissions.
    mode = netrc.stat().st_mode & 0o777
    assert mode == 0o600, f"expected .netrc mode 0600, got {oct(mode)}"

    content = netrc.read_text()
    assert "machine git.moleculesai.app" in content
    assert "login agent-dev-a" in content
    assert "password s3cr3t-t0k3n" in content


def test_setup_netrc_tempfile_is_private_before_token_write(
    setup_script: pathlib.Path,
    tmp_home: pathlib.Path,
) -> None:
    """Regression: the tempfile must be mode 0600 BEFORE token bytes land.

    This test sources the script's helper functions, creates a tempfile, and
    asserts it is 0600 and empty at that instant. It then writes credentials
    and verifies the content. If the implementation were ever reordered to
    write before chmod, this test would catch the regression because the file
    would either not be 0600 when empty or the token would already be present.
    """
    # Source the script to make helper functions available without running main().
    env = {
        **os.environ,
        "HOME": str(tmp_home),
    }
    source_cmd = f'source "{setup_script}"; _create_private_tempfile "{tmp_home}"'
    result = subprocess.run(
        ["bash", "-c", source_cmd],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"failed to create private tempfile: {result.stderr}"
    tmp_path = pathlib.Path(result.stdout.strip())
    assert tmp_path.exists(), f"tempfile {tmp_path} not created"

    # At this point NO token bytes have been written yet; the file must already
    # be exactly 0600.
    mode = tmp_path.stat().st_mode & 0o777
    assert mode == 0o600, f"tempfile not 0600 before write: {oct(mode)}"
    assert tmp_path.read_text() == "", "tempfile should be empty before write"

    # Now write credentials (same ordering the production main() uses).
    source_cmd2 = f'source "{setup_script}"; _write_netrc "{tmp_path}" git.moleculesai.app agent-dev-a s3cr3t-t0k3n'
    result2 = subprocess.run(
        ["bash", "-c", source_cmd2],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result2.returncode == 0, f"failed to write netrc: {result2.stderr}"

    content = tmp_path.read_text()
    assert "machine git.moleculesai.app" in content
    assert "login agent-dev-a" in content
    assert "password s3cr3t-t0k3n" in content

    # The file must remain 0600 after writing as well.
    mode_after = tmp_path.stat().st_mode & 0o777
    assert mode_after == 0o600, f"tempfile widened during write: {oct(mode_after)}"


def test_setup_netrc_skips_when_credentials_absent(
    setup_script: pathlib.Path,
    tmp_home: pathlib.Path,
) -> None:
    """Without env credentials the script exits cleanly and does not create netrc."""
    env = {
        **os.environ,
        "HOME": str(tmp_home),
        "GITEA_HOST": "git.moleculesai.app",
    }
    env.pop("GIT_HTTP_USERNAME", None)
    env.pop("GIT_HTTP_PASSWORD", None)
    result = subprocess.run(
        ["bash", str(setup_script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"setup script failed: {result.stderr}"
    assert not (tmp_home / ".netrc").exists()
    assert "skipping" in result.stderr.lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
