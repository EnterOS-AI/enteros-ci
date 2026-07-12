# molecule-ci

Shared CI contracts for the Molecule AI ecosystem. Canonical consumer templates enforce the same validation gate across plugin, workspace-template, and org-template repositories.

## Usage

Cross-repository `workflow_call` is not supported by the current Gitea deployment. Install the matching canonical template as `.gitea/workflows/ci.yml` in the consumer repository:

| Consumer | Canonical template |
|---|---|
| `molecule-ai-plugin-*` | [`templates/ci-plugin.yml`](templates/ci-plugin.yml) |
| `molecule-ai-workspace-template-*` | [`templates/ci-workspace-template.yml`](templates/ci-workspace-template.yml) |
| `molecule-ai-org-template-*` | [`templates/ci-org-template.yml`](templates/ci-org-template.yml) |

The inline templates clone `molecule-ci` from `git.moleculesai.app` and execute the canonical validators from `scripts/`, so validator logic remains centralized without a cross-repository action fetch.

### Any repo with auto-merge enabled

The reusable `disable-auto-merge-on-push.yml` definition is retained for the point when cross-repository `workflow_call` is supported. Do not install a thin cross-repository caller before that capability is enabled; it will not resolve on the current deployment.

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
| RuntimeId shape (open, bounded, path-safe) | Error | Unsafe or malformed runtime ID |
| `adapter.py` imports legacy `molecule_ai` | Warning | Pre-runtime-package imports |
| Dockerfile builds | Error | Broken image |
| Source-pinned `molecules-workspace-runtime` wheel | Error | Missing, retired, or public-index runtime package |
| No committed secrets | Error | Leaked API keys |

### validate-org-template

| Check | Severity | What it catches |
|---|---|---|
| `org.yaml` exists | Error | Missing org definition |
| Required fields (name) | Error | Incomplete template |
| SDK org schema | Error | Malformed workspace tree, defaults, plugins, or RuntimeIds |
| Direct-workspace count | Notice | Resolved inline workspace inventory |
| No committed secrets | Error | Leaked API keys |

### disable-auto-merge-on-push

PR-time safety guard. When `pull_request:synchronize` fires (= a new commit pushed to an open PR) and auto-merge is already enabled, this workflow disables auto-merge and posts a comment requiring the operator to re-engage explicitly.

**Why it exists:** on 2026-04-27, molecule-core PR #2174 auto-merged with only its first commit because the second commit was pushed AFTER the merge queue had locked the PR's SHA. The second commit ended up orphaned on a merged-and-deleted branch.

**Pairs with the org-wide repo setting** "Automatically delete head branches" (already enabled on all 10 Molecule-AI repos). Defense in depth:

1. Repo setting blocks pushes to a merged-and-deleted branch (catches the post-merge orphan case).
2. This workflow catches the in-queue race (push during queue processing) by force-disabling auto-merge.

Together they cover the full lifecycle of "auto-merge enabled → new commits arrive" without operator discipline.

**False-positive note:** if a CI bot pushes (dependency update, secret rotation), this also disables auto-merge. That's intentional — the operator who originally enabled auto-merge gets notified and re-engages, which is exactly the verify-after-machine-edits behavior we want.

## Composite actions

### conformance-gate

A reusable, parameterized **conformance gate** (P1 of RFC #3285): one shared
boundary-gate that any consumer adopts, replacing the per-repo bespoke scripts
(mcp-server `provenance-gate.sh` + core `check-published-mcp-manifest.mjs`). It
fetches a PRODUCER's PUBLISHED manifest for a pinned version/dist-tag and
asserts it **satisfies** a consumer contract's required capabilities, evaluated
**mode- and version-specifically**, and is **FAIL-CLOSED** on any error/miss.

Lives at `.gitea/actions/conformance-gate/` and is a **composite action** (not a
`workflow_call` reusable workflow) for the same reason as `audit-force-merge`:
cross-repo `uses:` does not resolve on Gitea 1.22.6. Adopters inline-clone this
SSOT and reference the action by local path — see `templates/ci-conformance-gate.yml`.

**Two modes** (`mode:` input):

| mode | generalizes | asserts |
|---|---|---|
| `registry-provenance` | mcp-server `provenance-gate.sh` | every PUBLISHED npm version on the registry packument has a matching `v<version>` git tag (catches out-of-band publishes) |
| `package-introspection` | core `check-published-mcp-manifest.mjs` | the PUBLISHED build's ACTUAL tool manifest (introspected under `server-mode`) ⊇ the contract's accepted capabilities (`required_tools ∪ transitional_tool_aliases`) |

**Fail-closed invariants** (both modes):

| Condition | Result |
|---|---|
| producer manifest unreachable / non-200 / empty / unparseable | **FAIL (exit 1)** |
| manifest parseable but zero capabilities / zero tools introspected | **FAIL (exit 1)** |
| required-capability set empty (contract declares none / `required-caps` empty) | **FAIL (exit 1)** |
| introspected server name != `expected-server-name` (when asserted) | **FAIL (exit 1)** |
| producer satisfies NONE of the accepted capabilities | **FAIL (exit 1)** — the headline staging stale-build degrade catch |
| producer satisfies ONLY a transitional alias (canonical absent) | **WARN (exit 0, `::warning::`)** — the one narrow band, keeps the migration window mergeable |
| `require-token: true` + empty token on a **trusted** context | **FAIL (exit 1)** |
| `require-token: true` + empty token on an **untrusted** fork PR | soft-skip (exit 0; forks can't hold secrets, the trusted run gates before any provision) |

**Key inputs:** `mode` (req), `package` (req), `registry`, `version` (pinned
version/dist-tag — evaluation is version-specific), `contract-path` *or*
`required-caps` (+`transitional-aliases`), `server-mode`, `expected-server-name`,
`registry-token` (OPTIONAL read:package bearer), `require-token`, `is-trusted`.

**Adoption** (clone-then-`uses:`-local; full example in `templates/ci-conformance-gate.yml`):

```yaml
- run: git clone --depth 1 https://git.moleculesai.app/molecule-ai/molecule-ci.git .molecule-ci
- uses: ./.molecule-ci/.gitea/actions/conformance-gate
  with:
    mode: package-introspection
    package: "@molecule-ai/mcp-server"
    contract-path: contracts/mcp-plugin-delivery.contract.json
    server-mode: management
    require-token: "true"
    registry-token: ${{ secrets.MCP_SERVER_READPKG_TOKEN }}
```

**Rollout** (soak-then-promote): ship the adopter caller as a STANDALONE
workflow, NOT a `ci.yml` job, NOT in branch protection. Promote a consumer's
emitted context into BP `status_check_contexts` (owner-only) only AFTER it soaks
green and any pre-gate cleanup lands — and only a name actually being emitted
(a BP-required context with no emitter = perma-pending = permanent merge-block).

A self-test (`.gitea/actions/conformance-gate/test-conformance-gate.sh`, wired
into `.gitea/workflows/conformance-gate-selftest.yml`) exercises both modes'
fail-closed branches + the WARN band offline.

## License

Business Source License 1.1 — © Molecule AI.
