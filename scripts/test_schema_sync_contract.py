from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check-schemas-in-sync.sh"
WORKFLOW = ROOT / ".gitea/workflows/schema-sync.yml"
SOURCE_COMMIT = ROOT / "schemas/SDK_SOURCE_COMMIT"


def test_schema_sync_uses_one_immutable_sdk_snapshot() -> None:
    commit = SOURCE_COMMIT.read_text(encoding="utf-8").strip()
    script = SCRIPT.read_text(encoding="utf-8")

    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert 'fetch --depth=1 origin "$SDK_COMMIT"' in script
    assert "fetch --depth=1 origin main" in script
    assert "does not match molecule-ai-sdk main" in script
    assert 'SDK_URL="https://git.moleculesai.app/molecule-ai/molecule-ai-sdk.git"' in script
    assert "SDK_SCHEMA_SOURCE_URL" not in script
    assert "GIT_CONFIG_GLOBAL=/dev/null" in script
    assert "-u GIT_CONFIG_PARAMETERS" in script
    assert "GIT_ASKPASS=/bin/false" in script
    assert 'HOME="$SAFE_GIT_HOME"' in script
    assert 'CURL_HOME="$SAFE_GIT_HOME"' in script
    assert 'XDG_CONFIG_HOME="$SAFE_GIT_HOME/xdg"' in script
    assert "raw/branch/main" not in script


def test_schema_sync_workflow_propagates_fetch_failures() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    pull_request_block = workflow.split("  pull_request:", maxsplit=1)[1].split(
        "  push:", maxsplit=1
    )[0]

    assert "schema-sync soft-skipped" not in workflow
    assert 'if [ "$rc" -eq 2 ]' not in workflow
    assert "persist-credentials: false" in workflow
    assert "paths:" not in pull_request_block
    assert "workflow_dispatch: {}" in workflow


def test_schema_fetch_failure_is_terminal(tmp_path: Path) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  [ \"$arg\" = fetch ] && exit 22\n"
        "done\n"
        f"exec {shlex.quote(real_git)} \"$@\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "could not fetch" in result.stderr.lower()
    assert "soft skip" not in (result.stdout + result.stderr).lower()


def test_schema_source_pin_must_match_current_sdk_main(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(upstream)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(upstream), "config", "user.name", "Schema Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(upstream), "config", "user.email", "schema@test.invalid"],
        check=True,
    )
    for source in (ROOT / "schemas").glob("*.schema.json"):
        contract_name = source.name.removesuffix(".schema.json")
        contract = upstream / "contracts" / contract_name / source.name
        contract.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, contract)
    subprocess.run(["git", "-C", str(upstream), "add", "contracts"], check=True)
    subprocess.run(
        ["git", "-C", str(upstream), "commit", "-q", "-m", "source snapshot"],
        check=True,
    )
    source_commit = subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    plugin_schema = upstream / "contracts/plugin-manifest/plugin-manifest.schema.json"
    plugin_schema.write_text(
        plugin_schema.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(upstream), "add", str(plugin_schema)], check=True)
    subprocess.run(
        ["git", "-C", str(upstream), "commit", "-q", "-m", "main drift"],
        check=True,
    )

    consumer = tmp_path / "consumer"
    (consumer / "scripts").mkdir(parents=True)
    shutil.copytree(ROOT / "schemas", consumer / "schemas")
    shutil.copy2(SCRIPT, consumer / "scripts/check-schemas-in-sync.sh")
    (consumer / "schemas/SDK_SOURCE_COMMIT").write_text(
        source_commit + "\n",
        encoding="utf-8",
    )

    real_git = shutil.which("git")
    assert real_git is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    hostile_home = tmp_path / "hostile-home"
    hostile_home.mkdir()
    (hostile_home / ".netrc").write_text(
        "machine git.moleculesai.app login ambient password must-not-be-read\n",
        encoding="utf-8",
    )
    safe_env_probe = tmp_path / "safe-env-probe"
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        f"real_git = {real_git!r}\n"
        f"upstream = {str(upstream)!r}\n"
        f"hostile_home = {str(hostile_home)!r}\n"
        f"safe_env_probe = {str(safe_env_probe)!r}\n"
        "home = os.environ.get('HOME', '')\n"
        "curl_home = os.environ.get('CURL_HOME', '')\n"
        "xdg_home = os.environ.get('XDG_CONFIG_HOME', '')\n"
        "if (home == hostile_home or os.path.isfile(os.path.join(home, '.netrc'))\n"
        "        or curl_home != home or xdg_home != os.path.join(home, 'xdg')):\n"
        "    sys.exit(97)\n"
        "with open(safe_env_probe, 'a', encoding='utf-8') as probe:\n"
        "    probe.write('isolated\\n')\n"
        "canonical = 'https://git.moleculesai.app/molecule-ai/molecule-ai-sdk.git'\n"
        "args = [upstream if arg == canonical else arg for arg in sys.argv[1:]]\n"
        "os.execv(real_git, [real_git, *args])\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    result = subprocess.run(
        ["bash", "scripts/check-schemas-in-sync.sh"],
        cwd=consumer,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HOME": str(hostile_home),
            "CURL_HOME": str(hostile_home),
            "XDG_CONFIG_HOME": str(hostile_home / "xdg"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "does not match molecule-ai-sdk main" in result.stdout
    assert safe_env_probe.is_file()
