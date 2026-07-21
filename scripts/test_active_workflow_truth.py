import os
from pathlib import Path
import re
import subprocess
import textwrap

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".gitea" / "workflows"


def test_unsupported_reusable_workflows_are_not_active() -> None:
    """Future GitHub-era designs must not be indexed as runnable Gitea jobs."""

    unsupported = {
        "auto-promote-branch.yml",
        "auto-promote-staging-pr.yml",
        "auto-promote-staging.yml",
        "disable-auto-merge-on-push.yml",
        "validate-org-template.yml",
        "validate-plugin.yml",
        "validate-workspace-template.yml",
        "_reusable-minimal-validate.yml",
        "meta-ci.yml",
    }

    active = {path.name for path in WORKFLOWS.glob("*.yml")}
    assert active.isdisjoint(unsupported), (
        "unsupported promotion/auto-merge workflows remain active: "
        f"{sorted(active & unsupported)}"
    )


def test_active_workflows_do_not_call_the_github_cli() -> None:
    """The canonical SCM is Gitea; ``gh`` silently targets the wrong API."""

    violations: list[str] = []
    github_cli = re.compile(r"\bgh\s+(?:api|pr|run)\b")
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line_number, line in enumerate(path.read_text().splitlines(), 1):
            if github_cli.search(line) or "actions@github.com" in line:
                violations.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")

    assert not violations, "GitHub-only commands remain in active workflows:\n" + "\n".join(violations)


def test_active_workflows_do_not_expose_workflow_call() -> None:
    violations: list[str] = []
    workflow_call = re.compile(r"^\s+workflow_call\s*:", re.MULTILINE)
    remote_use = re.compile(r"uses:\s+\S+/\.gitea/workflows/\S+@")
    for path in sorted(WORKFLOWS.glob("*.yml")):
        content = path.read_text()
        if workflow_call.search(content) or remote_use.search(content):
            violations.append(str(path.relative_to(ROOT)))

    assert not violations, "unsupported reusable workflow surface remains: " + ", ".join(violations)


def test_meta_ci_selftest_keeps_local_execution_and_immutable_archive_gate() -> None:
    """Pin both sides of the conflict-sensitive post-workflow_call shape."""

    workflow = yaml.safe_load((WORKFLOWS / "meta-ci-selftest.yml").read_text())
    selftest = workflow["jobs"]["selftest"]
    archive = workflow["jobs"]["official-consumer-archives"]

    assert "uses" not in selftest
    selftest_runs = "\n".join(
        step["run"] for step in selftest["steps"] if "run" in step
    )
    assert "python3 scripts/meta-ci.py --repo-root scripts/fixtures/meta-ci" in selftest_runs
    assert "grep -qxF 'meta-ci:sentinel:executed'" in selftest_runs

    assert "uses" not in archive
    archive_runs = "\n".join(
        step["run"] for step in archive["steps"] if "run" in step
    )
    assert "scripts/fixtures/meta-ci/official-consumers.json" in archive_runs
    assert "strict_json_loads" in archive_runs
    assert 'git -C "$fetch_dir" show "$actual:.runtime-version"' in archive_runs
    assert 'python3 scripts/mcp_pin_lockstep.py --repo-root "$proof_dir"' in archive_runs
    assert "mcp-pin-lockstep:sentinel:executed" in archive_runs
    assert "scripts/mcp_pin_lockstep.py --repo-root \"$proof_dir\" --json" in archive_runs
    assert 'runtime_version="$(python3 - "$attestation"' in archive_runs
    assert 'runtime_version" != "$fleet_version' in archive_runs
    assert "official fleet runtime lockstep" in archive_runs
    assert 'tr -d \'\\r\\n\' < "$proof_dir/.runtime-version"' not in archive_runs
    assert archive_runs.index("--json") < archive_runs.index(
        'runtime_version="$(python3 - "$attestation"'
    )
    assert "git -C \"$fetch_dir\" archive" not in archive_runs
    assert 'python3 scripts/meta-ci.py --repo-root "$proof_dir"' not in archive_runs
    archive_gate = next(
        step
        for step in archive["steps"]
        if step.get("name") == "Validate immutable consumer artifact pins"
    )
    assert archive_gate["env"] == {
        "GIT_ASKPASS": "/bin/false",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for job in (selftest, archive):
        checkout = next(step for step in job["steps"] if "uses" in step)
        assert checkout["with"]["persist-credentials"] is False


def test_meta_ci_archive_gate_never_logs_raw_invalid_consumer_pin(tmp_path) -> None:
    """The checker must reject a raw pin before the shell compares or prints it."""

    workflow = yaml.safe_load((WORKFLOWS / "meta-ci-selftest.yml").read_text())
    archive = workflow["jobs"]["official-consumer-archives"]
    archive_runs = next(
        step["run"]
        for step in archive["steps"]
        if step.get("name") == "Validate immutable consumer artifact pins"
    )

    scripts = tmp_path / "scripts"
    fixture = scripts / "fixtures" / "meta-ci"
    fixture.mkdir(parents=True)
    fixture.joinpath("official-consumers.json").write_bytes(
        (ROOT / "scripts/fixtures/meta-ci/official-consumers.json").read_bytes()
    )
    scripts.joinpath("mcp_pin_lockstep.py").write_text(
        textwrap.dedent(
            """\
            import argparse
            import json
            import sys
            from pathlib import Path

            def strict_json_loads(payload, _label):
                return json.loads(payload)

            if __name__ == "__main__":
                parser = argparse.ArgumentParser()
                parser.add_argument("--repo-root", required=True)
                parser.add_argument("--json", action="store_true")
                args = parser.parse_args()
                pin = Path(args.repo_root, ".runtime-version").read_text().strip()
                if pin != "0.4.35":
                    print("invalid runtime pin", file=sys.stderr)
                    raise SystemExit(1)
                json.dump(
                    {
                        "checker_sentinel": "mcp-pin-lockstep:sentinel:executed",
                        "runtime": {"version": pin},
                    },
                    sys.stdout,
                )
            """
        )
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            while args[:1] == ["-c"]:
                args = args[2:]
            directory = None
            if args[:1] == ["-C"]:
                directory = Path(args[1])
                args = args[2:]
            command = args[0]
            if command == "init":
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
            elif command == "remote":
                pass
            elif command == "fetch":
                directory.joinpath("fetched").write_text(args[-1])
            elif command == "rev-parse":
                print(directory.joinpath("fetched").read_text())
            elif command == "show":
                consumer = directory.name.removesuffix(".fetch")
                pin = "0.4.35" if consumer == "claude-code" else "credential=must-not-log"
                print(pin)
            else:
                raise SystemExit(f"unexpected fake git command: {command}")
            """
        )
    )
    fake_git.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["bash", "-c", archive_runs],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "invalid runtime pin" in output
    assert "credential=must-not-log" not in output


def test_meta_ci_consumer_template_does_not_persist_checkout_credentials() -> None:
    workflow = yaml.safe_load((ROOT / "templates" / "ci-meta.yml").read_text())
    checkout = next(
        step
        for step in workflow["jobs"]["meta"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )

    assert checkout["with"]["persist-credentials"] is False


def test_readme_does_not_claim_the_retired_guard_is_active() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "### disable-auto-merge-on-push" not in readme
    assert "this workflow disables auto-merge" not in readme
