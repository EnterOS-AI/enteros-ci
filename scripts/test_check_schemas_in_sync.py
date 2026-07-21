import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check-schemas-in-sync.sh"
WORKFLOW = ROOT / ".gitea/workflows/schema-sync.yml"


def test_schema_sync_workflow_never_waives_source_unavailability() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "soft-skip" not in script.lower()
    assert "soft-skip" not in workflow.lower()
    assert "raw/branch/main" not in script
    assert "safe_git()" in script
    assert script.count("-c http.userAgent=curl/8.4.0") == 2
    assert 'fetch --depth=1 origin "$SDK_COMMIT"' in script
    assert "fetch --depth=1 origin main" in script
    assert "GIT_CONFIG_GLOBAL=/dev/null" in script
    assert "GIT_ASKPASS=/bin/false" in script
    assert "run: bash scripts/check-schemas-in-sync.sh" in workflow
    assert "set +e" not in workflow
    assert (
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
        in workflow
    )


def test_schema_sync_source_failure_is_nonzero(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("curl", "git"):
        executable = fake_bin / name
        executable.write_text("#!/bin/sh\nexit 22\n", encoding="utf-8")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "error" in (result.stdout + result.stderr).lower()
