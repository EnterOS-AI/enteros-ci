# molecule-ci

Shared CI workflows for the Molecule AI ecosystem. Every plugin, workspace template, and org template repo calls these reusable workflows to enforce a standard validation gate.

## Usage

### Plugin repos (`molecule-ai-plugin-*`)

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  validate:
    uses: molecule-ai/molecule-ci/.gitea/workflows/validate-plugin.yml@v1
```

### Workspace template repos (`molecule-ai-workspace-template-*`)

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  validate:
    uses: molecule-ai/molecule-ci/.gitea/workflows/validate-workspace-template.yml@v1
```

### Org template repos (`molecule-ai-org-template-*`)

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  validate:
    uses: molecule-ai/molecule-ci/.gitea/workflows/validate-org-template.yml@v1
```

### Any repo with auto-merge enabled

PR-time guards (currently: disable auto-merge on follow-up push). Consume from a thin caller:

```yaml
# .github/workflows/pr-guards.yml
name: pr-guards
on:
  pull_request:
    types: [synchronize]
permissions:
  pull-requests: write
jobs:
  disable-auto-merge-on-push:
    uses: molecule-ai/molecule-ci/.gitea/workflows/disable-auto-merge-on-push.yml@v1
```

When the team lands more PR-time guards in this repo, add them as additional jobs in the same caller — keeps each consuming repo's footprint to one file.

## What each workflow validates

### validate-plugin

| Check | Severity | What it catches |
|---|---|---|
| `plugin.yaml` exists | Error | Missing manifest |
| Required fields (name, version, description) | Error | Incomplete plugin |
| Has content (SKILL.md, hooks/, skills/, or rules/) | Error | Empty plugin |
| SKILL.md starts with heading | Warning | Bad formatting |
| No committed secrets | Error | Leaked API keys |
| No build artifacts | Error | node_modules, __pycache__ |

### validate-workspace-template

| Check | Severity | What it catches |
|---|---|---|
| `config.yaml` exists | Error | Missing config |
| Required fields (name, runtime) | Error | Incomplete template |
| `template_schema_version: 1` | Error | Missing version contract |
| Known runtime check | Warning | Typo in runtime name |
| `adapter.py` imports molecule_runtime | Warning | Legacy imports |
| Dockerfile builds | Error | Broken image |
| molecule-ai-workspace-runtime dependency | Warning | Missing base package |
| No committed secrets | Error | Leaked API keys |

### validate-org-template

| Check | Severity | What it catches |
|---|---|---|
| `org.yaml` exists | Error | Missing org definition |
| Required fields (name) | Error | Incomplete template |
| Workspace structure valid | Error | Malformed hierarchy |
| `files_dir` references exist | Warning | Broken system-prompt paths |
| `template_schema_version` present | Warning | Missing version contract |
| No committed secrets | Error | Leaked API keys |

### disable-auto-merge-on-push

PR-time safety guard. When `pull_request:synchronize` fires (= a new commit pushed to an open PR) and auto-merge is already enabled, this workflow disables auto-merge and posts a comment requiring the operator to re-engage explicitly.

**Why it exists:** on 2026-04-27, molecule-core PR #2174 auto-merged with only its first commit because the second commit was pushed AFTER the merge queue had locked the PR's SHA. The second commit ended up orphaned on a merged-and-deleted branch.

**Pairs with the org-wide repo setting** "Automatically delete head branches" (already enabled on all 10 Molecule-AI repos). Defense in depth:

1. Repo setting blocks pushes to a merged-and-deleted branch (catches the post-merge orphan case).
2. This workflow catches the in-queue race (push during queue processing) by force-disabling auto-merge.

Together they cover the full lifecycle of "auto-merge enabled → new commits arrive" without operator discipline.

**False-positive note:** if a CI bot pushes (dependency update, secret rotation), this also disables auto-merge. That's intentional — the operator who originally enabled auto-merge gets notified and re-engages, which is exactly the verify-after-machine-edits behavior we want.

## License

Business Source License 1.1 — © Molecule AI.
