from pathlib import Path
import re

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
    assert "git -C \"$fetch_dir\" archive \"$actual\"" in archive_runs
    assert 'python3 scripts/meta-ci.py --repo-root "$archive_dir"' in archive_runs


def test_readme_does_not_claim_the_retired_guard_is_active() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "### disable-auto-merge-on-push" not in readme
    assert "this workflow disables auto-merge" not in readme
