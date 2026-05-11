"""Tests for runners-restart-safe.sh.

Uses a PATH-prepended fake-docker + fake-sleep so we can test the script's
logic (container existence check, task-wait, restart, re-register verify)
without a real Docker daemon.

In test mode (TEST_MODE=1) the script sets MAX_WAIT_MINUTES=0 so the
wait loop exits on the first iteration.  Fake docker ps outputs ""
(no running tasks) so the runner is always idle.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "runners-restart-safe.sh"

# ---------------------------------------------------------------------------
# Fake docker helpers
# ---------------------------------------------------------------------------

def _mktmpdir() -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="fake-docker-"))
    (tmpdir / "sleep").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    os.chmod(tmpdir / "sleep", stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)
    return tmpdir


def _write_fake_docker(docker_content: str) -> Path:
    tmpdir = _mktmpdir()
    docker = tmpdir / "docker"
    docker.write_text(docker_content, encoding="utf-8")
    os.chmod(docker, stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)
    return tmpdir


def _run_script(fake_docker_dir: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{fake_docker_dir}:{os.environ['PATH']}", "TEST_MODE": "1"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Fake docker: all containers missing (inspect exits 1)
# ---------------------------------------------------------------------------
IDLE_DOCKER = textwrap.dedent("""\
    #!/bin/bash
    # All runners are absent; script should skip all and exit 0.
    case "$1" in
        inspect) exit 1 ;;
        ps) echo "" ;;
        restart) exit 1 ;;
        logs) exit 1 ;;
    esac
    exit 0
    """)


# ---------------------------------------------------------------------------
# Fake docker: molecule-runner-1 exists, no tasks, re-register OK
# ---------------------------------------------------------------------------
OK_DOCKER = textwrap.dedent("""\
    #!/bin/bash
    # Runner-1 exists and is idle; re-register succeeds.
    case "$1" in
        inspect) echo '{"Name": "molecule-runner-1"}'; exit 0 ;;
        ps) echo "" ;;
        restart) exit 0 ;;
        logs) echo "runner started, declare successfully registered"; exit 0 ;;
    esac
    exit 1
    """)


# ---------------------------------------------------------------------------
# Fake docker: molecule-runner-1 exists, no tasks, but re-register FAILS
# ---------------------------------------------------------------------------
FAIL_DOCKER = textwrap.dedent("""\
    #!/bin/bash
    # Runner-1 exists and is idle; re-register check fails.
    # All runners' inspect succeeds so the script attempts all 8 restarts.
    case "$1" in
        inspect) echo '{"Name": "'"$2"'"}'; exit 0 ;;
        ps) echo "" ;;
        restart) exit 0 ;;
        logs) echo "some other log output"; exit 0 ;;
    esac
    exit 0
    """)


# ---------------------------------------------------------------------------
# Fake docker: molecule-runner-1 has a running task (skip restart)
# ---------------------------------------------------------------------------
BUSY_DOCKER = textwrap.dedent("""\
    #!/bin/bash
    # Runner-1 has a running Gitea task; restart must be skipped.
    # docker ps outputs the busy task for runner-1 (grep matches), but
    # runners 2-8's grep patterns don't match the task suffix → treated as idle.
    # logs outputs "declare successfully" so re-register check passes.
    case "$1" in
        inspect) echo '{"Name": "'"$2"'"}'; exit 0 ;;
        ps) echo "GITEA-ACTIONS-TASK-abc123-molecule-runner-1" ;;
        restart) exit 0 ;;
        logs) echo "runner started, declare successfully registered"; exit 0 ;;
    esac
    exit 0
    """)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_script_passes_when_all_runners_missing():
    """Positive path: no runner containers exist → script exits 0."""
    fake = _write_fake_docker(IDLE_DOCKER)
    try:
        result = _run_script(fake)
        assert result.returncode == 0, f"stderr: {result.stderr}"
    finally:
        import shutil
        shutil.rmtree(fake)


def test_script_restarts_idle_runner_ok():
    """Positive path: runner exists, no tasks, re-registers successfully."""
    fake = _write_fake_docker(OK_DOCKER)
    try:
        result = _run_script(fake)
        assert result.returncode == 0, f"stderr: {result.stderr}"
    finally:
        import shutil
        shutil.rmtree(fake)


def test_script_fails_on_missing_declare_line():
    """Negative path: runner restarted but did not emit 'declare successfully'."""
    fake = _write_fake_docker(FAIL_DOCKER)
    try:
        result = _run_script(fake)
        assert result.returncode == 1, (
            f"expected failure, got rc={result.returncode}; stderr={result.stderr}"
        )
    finally:
        import shutil
        shutil.rmtree(fake)


def test_script_skips_runner_with_busy_task():
    """Positive path: runner has an in-flight task → script skips it silently."""
    fake = _write_fake_docker(BUSY_DOCKER)
    try:
        result = _run_script(fake)
        assert result.returncode == 0, (
            f"expected 0, got rc={result.returncode}; stderr={result.stderr}"
        )
        assert "restart called while task is running" not in result.stderr
    finally:
        import shutil
        shutil.rmtree(fake)
