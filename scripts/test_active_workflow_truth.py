from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".gitea" / "workflows"


def test_unsupported_reusable_workflows_are_not_active() -> None:
    """Future GitHub-era designs must not be indexed as runnable Gitea jobs."""

    unsupported = {
        "auto-promote-branch.yml",
        "auto-promote-staging-pr.yml",
        "auto-promote-staging.yml",
        "disable-auto-merge-on-push.yml",
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


def test_readme_does_not_claim_the_retired_guard_is_active() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "### disable-auto-merge-on-push" not in readme
    assert "this workflow disables auto-merge" not in readme
