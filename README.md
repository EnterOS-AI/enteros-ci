# molecule-ci

Shared CI contracts for the Molecule AI ecosystem. Canonical consumer templates enforce the same validation gate across plugin, workspace-template, and org-template repositories.

## Usage

Cross-repository `workflow_call` is not a valid gate on the current Gitea
deployment: a caller can be recorded green with zero referenced steps executed.
Install the matching canonical template as `.gitea/workflows/ci.yml` in the
consumer repository:

| Consumer | Canonical template |
|---|---|
| `molecule-ai-plugin-*` | [`templates/ci-plugin.yml`](templates/ci-plugin.yml) |
| `molecule-ai-workspace-template-*` | [`templates/ci-workspace-template.yml`](templates/ci-workspace-template.yml) |
| `molecule-ai-org-template-*` | [`templates/ci-org-template.yml`](templates/ci-org-template.yml) |
| satellite/channel/misc repositories | [`templates/ci-minimal.yml`](templates/ci-minimal.yml) |
| repositories needing the canonical diff secret gate | [`templates/ci-secret-scan.yml`](templates/ci-secret-scan.yml) |
| repositories requiring the SOP-checklist peer-ack gate | [`templates/ci-sop-checklist-gate.yml`](templates/ci-sop-checklist-gate.yml) |

Installing `ci-sop-checklist-gate.yml` means **deleting** the repository's
vendored `.gitea/scripts/sop-checklist-gate.py`. That script existed as three
hand-maintained copies which drifted in both directions and produced an
org-wide gate outage on 2026-07-25; the union now lives once as
[`scripts/sop_checklist_gate.py`](scripts/sop_checklist_gate.py). Keep the
per-repo `.gitea/sop-checklist-config.yaml` — that is legitimately per-repo.

### The checklist is tiered by what the diff touches

The gate used to demand all seven acks — `comprehensive-testing`,
`local-postgres-e2e`, `staging-smoke`, … — for every PR, including one-line
config and docs edits. A gate that asks for a staging smoke test on an Upptime
monitor repoint does not get satisfied; it gets resented and routed around,
which is worse than no gate. Since 2026-07-25 the item set is proportional:

| tier | when | items required |
|---|---|---|
| `light` | **every** changed path matches `tiering.trivial_paths` (docs, `*.md`, `.upptimerc.yml`, images) | `tiering.light_items` — by default `five-axis-review` + `memory-consulted`, both satisfiable by one non-author engineer |
| `full` | anything else — **and always** if any changed path matches `tiering.reserved_paths` | every item in the repo config |

Reserved is evaluated **before** the trivial allowlist, so a reserved path can
never be downgraded: not by an over-broad `trivial_paths` entry, not by a
label, not by a small diff. `reserved_paths` from a repo config are **added
to** the SSOT defaults, never substituted for them, and
`.gitea/sop-checklist-config.yaml` is itself reserved so the tiering rules
cannot be widened by a PR the widened rules would then wave in. An unreadable
or truncated changed-file list fails closed to `full`.

**Line count is deliberately not an input.** It is gameable by splitting a
commit, and it is wrong — one line in a migration, a deploy workflow, or the
auth path is not a trivial change.

The tier is disclosed on the status description (`[tier:light] acked: 2/2 …`),
in the job log with the deciding path and glob, and as a machine-greppable
`sop-checklist:tier:<tier>:<reason>` line that the consumer template asserts is
present — a run that picks a tier without saying so does not pass.

Defaults live in the SSOT script, so a consumer repo gets tiering with **no
config change**. `tiering:` in `.gitea/sop-checklist-config.yaml` only tunes it.

The gate's credential is `SOP_CHECKLIST_GATE_TOKEN` (Infisical prod
`/shared/gitea-bot-tokens`, mirrored as an **org** Actions secret so no repo
needs a pasted copy). It needs `write:repository` for the status POST.
`SOP_TIER_CHECK_TOKEN` belongs to the separate sop-tier-check gate, carries
`write:issue` and **not** `write:repository`, and is not interchangeable —
using it is what took the required context red across two repos on 2026-07-25.

The inline templates fetch an immutable, verified `molecule-ci` commit from
`git.moleculesai.app` and execute the canonical validators from `scripts/`.
Validator logic remains centralized without a cross-repository action fetch,
while every consumer update stays an explicit reviewed pin change.

### Merge and promotion automation

This repository does not publish a reusable auto-promote or
disable-auto-merge workflow. The former definitions combined unsupported
cross-repository `workflow_call` behavior with GitHub CLI commands, so Gitea
could index or report them without providing the claimed protection. Git
history preserves those designs; they are not active templates.

Any future guard must be implemented as a repository-local, base-trusted Gitea
workflow, use the `git.moleculesai.app` API with a least-privilege identity, and
prove its emitted context before branch protection requires it. Until then,
operators must treat every new PR head as a new review and CI boundary.

Every inline consumer template pins an immutable molecule-ci commit and
verifies the fetched SHA before execution. The minimal and secret-scan gates
also assert script sentinels. Updating any pin is a reviewed dependency change.

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

## Composite actions

### conformance-gate

A reusable, parameterized **conformance gate** (P1 of RFC #3285): one shared
boundary-gate that any consumer adopts, replacing the per-repo bespoke scripts
(mcp-server `provenance-gate.sh` + core `check-published-mcp-manifest.mjs`). It
fetches a PRODUCER's PUBLISHED manifest for a pinned version/dist-tag and
asserts it **satisfies** a consumer contract's required capabilities, evaluated
**mode- and version-specifically**, and is **FAIL-CLOSED** on any error/miss.

Lives at `.gitea/actions/conformance-gate/` and is a **composite action** (not a
`workflow_call` reusable workflow). Cross-repo action resolution is not part of
the validated Gitea CI contract, so adopters must not assume it works: they
fetch this SSOT at an immutable SHA into a guarded local path and reference the
action there. See
`templates/ci-conformance-gate.yml`.

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

**Adoption** (immutable-fetch-then-`uses:`-local; full example in `templates/ci-conformance-gate.yml`):

```yaml
env:
  MOLECULE_CI_REF: 9a2fd33e4ece9e54a3e1364c74daa59336ed151b
steps:
  - run: |
      mkdir .molecule-ci-ssot
      git init -q .molecule-ci-ssot
      git -C .molecule-ci-ssot remote add origin https://git.moleculesai.app/molecule-ai/molecule-ci.git
      git -C .molecule-ci-ssot fetch -q --depth 1 origin "$MOLECULE_CI_REF"
      git -C .molecule-ci-ssot checkout -q --detach FETCH_HEAD
      test "$(git -C .molecule-ci-ssot rev-parse HEAD)" = "$MOLECULE_CI_REF"
  - uses: ./.molecule-ci-ssot/.gitea/actions/conformance-gate
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
