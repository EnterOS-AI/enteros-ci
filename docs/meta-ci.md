# meta-ci — capability→bundle CI router (task internal#57)

`meta-ci` is the SSOT-owned, capability-auto-attached CI-enforcement spine. Today a
check runs on a repo only because a human hand-wired a workflow that opts in; a repo
with, say, plugin code but no `ci.yml` carries **zero** required checks and merges
green-empty. `meta-ci` replaces "opt in per repo" with "declare what the repo IS, and
the router derives what it must run".

**This is Phase 1: ADVISORY only.** Nothing here makes a check required, flips branch
protection, or retires the `["*"]` wildcard. Those are Phases 3–4 (owner-gated).

## The one declared fact: `repo-meta.yaml`

Every repo declares a `repo-meta.yaml` (SDK-owned SSOT schema —
`contracts/repo-meta/repo-meta.schema.json`, vendored here at
`schemas/repo-meta.schema.json` and kept byte-honest by `schema-sync.yml`):

```yaml
schema_version: 1
layer: plugin              # service | runtime-template | plugin | org-template | contract
capabilities:              # OPEN set; unknown = WARN (attaches no bundle), never reject
  - skills
  - settings-fragment
waivers:                   # optional, time-boxed escape hatch
  - bundle: mcp-pin-lockstep
    until: "2026-09-01"    # MUST be quoted — bare YYYY-MM-DD parses as a YAML date
    reason: "blocked on molecule-core#1234"
```

## The capability→bundle map (`scripts/meta-ci.py`)

The router UNIONs (and dedupes) the per-layer baseline with each capability's add-on,
plus a universal `secret-scan`, minus any live-waived bundle:

| `layer` | bundles |
| --- | --- |
| `service` | `go-build-vet-lint-test`, `secret-scan` |
| `runtime-template` | `adapter-conformance`, `docker-build-smoke`, `t4-assert`, `secret-scan` |
| `plugin` | `plugin-manifest-validate`, `secret-scan` |
| `org-template` | `org-template-validate`, `secret-scan` |
| `contract` | `contracts-codegen-drift`, `secret-scan` |

| `capability` | bundle |
| --- | --- |
| `go-service` | `go-build-vet-lint-test` |
| `python-package` | `py-ruff-pytest-build` |
| `node-package` | `node-install-lint-typecheck-build` |
| `adapter` | `adapter-conformance` |
| `mcp-server-bake` | `mcp-pin-lockstep` |
| `skills` | `skill-lint` |
| `settings-fragment` | `settings-fragment-validate` |
| `env-mutator` | `go-env-mutator-checks` |
| `docker-image` | `docker-build-smoke` |

`secret-scan` is attached to **every** repo. Unknown capabilities attach nothing and
are warned. Run `python3 scripts/meta-ci.py --repo-root . --plan-json` to see the
derived plan for any repo.

### `node-package` — one bundle for frontends *and* TS/JS services

`node-package` (RFC #57 Phase 2 — covers the deferred Node/TS repos) detects the package
manager from the lockfile (precedence **pnpm > yarn > npm**; a `package.json` with no
lockfile degrades to a non-frozen `npm install`), runs a **frozen** install, then runs
**only the repo's own declared** `lint` / `typecheck` / `build` scripts (skip-if-absent —
it never invents a script a repo lacks), on top of the universal `secret-scan`.

**Why no distinct `frontend` capability.** The bundle is *script-driven*: a Next.js/Astro
app's declared `build` runs under `node-package` exactly as a TS service's `build` does, so
frontends need no separate treatment in Phase 1 (and a second vocab entry would need a
second SDK-schema SSOT change for no behavioural gain). Ground truth across the fleet
supports one bundle — frontends (`molecule-app`, `molecule-admin`, `molecules-market`,
`landingpage`, `docs`) declare `build`+`lint` (± `typecheck`); `molecule-mobile` declares
`lint`+`typecheck` (no web `build`); TS services declare a subset (`molecule-mcp-server`:
`build`; `molecule-tenant-proxy`: none) — all handled by skip-if-absent. If a frontend-only
artifact check (e.g. assert `build` emitted `.next/`/`dist/`) is later wanted, it layers on
as a `frontend` capability then.

### Phase 1 executes only the cheap, self-guarding runners

The "matrix" runs **in-process** inside `meta-ci.py` (a loop), so exactly **one**
aggregate context is produced — not one-per-leg. Phase 1 executes the bundle runners that
are safe to run in-repo: `secret-scan` and the `node-install-lint-typecheck-build` bundle
(the latter no-ops to a clean pass when there is no `package.json` or a script is not
declared, but **fails closed** — it does not green-skip — when a repo that declares
`node-package` runs on a runner missing the package manager, because an unrun
lint/typecheck/build must never count as a passing leg; every step is also bounded by a
`timeout`, so a hanging build fails rather than wedging the job). The heavier language bundles
(`go-build-vet-lint-test`, `py-ruff-pytest-build`, `docker-build-smoke`, `t4-assert`, …)
stay reported as `planned (execution wired in Phase 2)`. The aggregate is: manifest-valid
AND every executed runner green. This is deliberately capture-first / enforce-later.

## Adoption (advisory)

Copy `templates/ci-meta.yml` into a repo as `.gitea/workflows/meta-ci-advisory.yml` and
commit a `repo-meta.yaml`. It anon-clones this SSOT and runs the canonical router with
`continue-on-error: true` and **no commit-status POST**, so it can never block a merge
(its result lives in the job log). Do **not** add it to branch-protection required
contexts.

### Why inline, not cross-repo `uses:`

Cross-repo `workflow_call` is not a trustworthy gate on Gitea Actions 1.26.4 — a
consumer job can be recorded green with `steps=[]` (internal#1000). The remote
definition was removed. `meta-ci-selftest.yml` and consumer templates execute the
router in ordinary repository-local jobs; the router prints a
`meta-ci:sentinel:executed` line so a hollow/no-op run is detectable.

## R1: `["*"]` absent-context semantics (verified 2026-07-17, Gitea 1.26.4)

The fail-closed design rests on "an absent required context blocks". Verified
empirically on a throwaway repo:

- BP requiring an **explicit never-emitted** context → merge endpoint returns
  **HTTP 405 "Not all required status checks successful"** → **BLOCK**.
- BP `status_check_contexts: ["*"]` with **zero emitted statuses** → after settling,
  **HTTP 405** → **BLOCK** (not a vacuous passthrough).
- In both cases the PR object's `mergeable` field read `true` — it is optimistic and
  **must not** be trusted; only the merge endpoint's HTTP code is authoritative.

**Verdict: absent required context == BLOCK.** So the BP direction fails closed and no
per-PR presence-gate is needed to stop a *missing* context from passing. The residual
risk is a *hollow-green emitter* (internal#1000) — which is why adoption is inline +
carries the sentinel, and why Phase 4 should assert the sentinel rather than merely
assert context presence.

## Phased rollout

1. **Phase 1 (this): advisory.** Schema vendored + router + inline advisory template +
   pilot adoption. No BP change.
2. **Phase 2: wire bundle execution.** Give each `planned` bundle a real runner.
3. **Phase 3: enforce.** Call the reusable with `advisory: false` (posts a real-state
   `meta-ci / required` context, fails the job on red) after a clean advisory soak.
4. **Phase 4: org-enforce.** Add `meta-ci / required` to BP + a sentinel-execution
   assertion + `block_admin_merge_override` fleet-wide. Owner-gated.
