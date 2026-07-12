# Workspace Template Contract

Hard rules every `molecule-ai-workspace-template-*` repo must satisfy. Enforced by `scripts/validate-workspace-template.py` through the canonical inline consumer workflow in `templates/ci-workspace-template.yml`.

The official templates share a runtime and image contract but evolve independently. This gate prevents a template from silently losing cache invalidation, package provenance, adapter loading, or container-entrypoint behavior.

## Dockerfile

| Rule | Why |
|---|---|
| `FROM python:3.11-slim` | Single base everywhere — keeps apt + pip behaviour identical and lets us reason about CVE patches on one base. |
| `ARG RUNTIME_VERSION=` declared | The arg invalidates the pip-install layer's cache key whenever the cascade publishes a new wheel. Without it the cache hit replays the previous runtime. |
| `${RUNTIME_VERSION}` referenced in the private wheel download | Just declaring the ARG is not enough; it must select the runtime requirement in the cache-invalidating layer. |
| `ARG MOLECULE_RUNTIME_INDEX=https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/` | The private runtime source is explicit and reviewable. |
| Private-only wheel acquisition | Download exactly one runtime wheel with `pip download --isolated --only-binary=:all: --no-deps --index-url "${MOLECULE_RUNTIME_INDEX}"`; never use an extra index for this step. |
| Local-wheel dependency solve | Install `/tmp/molecule-runtime/*.whl` in the same `pip install` solve as a filtered requirements file that excludes the runtime declaration. The local wheel pins runtime provenance, lets `RUNTIME_VERSION` override a checked-in pin, and leaves public dependencies to resolve normally. |
| `RUN useradd -u 1000 -m -s /bin/bash agent` | The runtime drops to uid 1000 before exec'ing the SDK. Claude Code refuses `--dangerously-skip-permissions` as root for safety. The `/workspace` volume is also chown'd to 1000 by the platform provisioner. |
| `ENTRYPOINT ["molecule-runtime"]` *or* a wrapper script that exec's `molecule-runtime` | Single entrypoint means the platform's container-restart contract is uniform across templates. Wrapper scripts are allowed (claude-code has `entrypoint.sh` for gosu drop-priv; hermes has `start.sh` to boot the hermes-agent daemon first). |
| `molecules-workspace-runtime` listed exactly once in `requirements.txt` | The runtime wheel is the contract. The old distribution name is rejected because it was retired after a dependency-confusion incident. Direct/VCS/local runtime sources are rejected. |

## config.yaml

| Required key | Type | Notes |
|---|---|---|
| `name` | str | Human-readable; appears on the canvas card. |
| `runtime` | str | Open RuntimeId: 1-64 lowercase alphanumeric segments separated by `-` or `_`. Official first-party support is discovered separately and is not a universal allowlist. |
| `template_schema_version` | int | Currently `1`. Bump when adding a key that changes how the platform consumes config.yaml. **Must be int**, not string — a quoted `"1"` will fail validation. |

| Optional key | Notes |
|---|---|
| `description` | Free text, surfaces on canvas. |
| `version`, `tier` | `version` is a string; `tier` is an integer controlling platform-side rollout gating. |
| `model`, `models` | Either a single model id or a list of model ids the agent may use. |
| `runtime_config` | Nested block of runtime-specific settings (for example, claude-code and hermes adapters). |
| `env`, `skills`, `tools`, `a2a`, `delegation`, `prompt_files`, `bridge`, `governance` | Optional feature blocks. Add new contract keys to the SDK workspace-template schema SSOT, then re-vendor it here. |

Unknown top-level keys produce a warning (not an error) so accidental drift is visible without blocking.

### Official-template SSOT inheritance (the `--official` gate)

The principal's rule: the OFFICIAL repo must **enforce** the SSOT, not just rely on convention. An **official** workspace template MUST NOT hardcode the *default* provider/model or pin the Molecule platform LLM proxy, because the controlplane resolves and injects those at provision time:

- the LLM routing mode (`platform` vs `byok`) is derived from the env-identity SSOT — `molecule-controlplane internal/provisioner/llm_mode.go` (`ResolveLLMMode` / `LLMModeForEnv`): `production`/`staging`/`e2e` → `platform`, `dev` → `byok`;
- the platform proxy endpoint + `MOLECULE_LLM_USAGE_TOKEN` auth are injected by the CP (`PlatformLLMProxyEnv` → `MOLECULE_LLM_*` / `ANTHROPIC_BASE_URL` / …), never pinned per template;
- the default model comes from the `providers.yaml` registry SSOT.

A template that **re-pins** any of these re-introduces the silent prod-routing drift the CP SSOT eliminated — the "Not logged in" / unservable-option class.

`check_no_hardcoded_provider_model` ERRORs (under `--official`) on:

| Flagged | Why |
|---|---|
| top-level `model:` | a hardcoded model default (exempt only with `--allow-self-model`) |
| `runtime_config.model` | the default-model pin |
| `runtime_config.provider` | the default-provider pin |
| any `providers[*].base_url` containing `internal/llm/` | a pinned platform LLM proxy (CP injects it) |

It does **not** flag a `runtime_config.models` *catalog* (the user-selectable menu + per-entry `required_env`) — that is kept ⊆ the registry SSOT by the separate platform-model / full-providers drift gates.

**Activation is opt-in and dynamic** — no hardcoded repo allowlist. A template repo declares itself official by committing a `.official` marker file; the canonical consumer CI then runs the validator with `--official`. If `.official` contains the token `allow-self-model`, `--allow-self-model` is also passed — this exempts **only** the top-level `model:` for the platform-agent (Org Concierge) template whose own declared model IS its identity per core#2594. Community and un-migrated templates (no marker) are unaffected; the gate never fires for them.

## adapter.py

Optional. When present, `adapter.py` should:
- Import `BaseAdapter` from `molecule_runtime.adapter_base`.
- Override `setup()` and `create_executor()` for the runtime's specific entry point.

The pre-#87 import path (`molecule_ai`) produces a warning if it appears.

## requirements.txt

Must declare `molecules-workspace-runtime` exactly once, with an optional version pin or floor. Nested requirement files are inspected within the repository root. Untrusted index overrides, continuations, direct/VCS/archive/local sources, editable installs, and the retired runtime distribution fail closed.

## CI

Every template repo installs `templates/ci-workspace-template.yml` as `.gitea/workflows/ci.yml`. The current Gitea deployment does not resolve cross-repository `workflow_call`, so the canonical inline template clones `molecule-ci` into `.molecule-ci` and runs `scripts/validate-workspace-template.py` from that checkout. No validator script is vendored into the consumer repository.

### T4 live-gate aggregation (templates that inline T4)

Templates that run a live `t4-conformance` job must aggregate its result in a `validate` job that emits the branch-protection context. The aggregator must treat `t4-conformance` as a **hard gate**: it should require `success` on internal PRs and `push` to `main`. `skipped` is only acceptable on **fork PRs** (where the security-sensitive live gate is intentionally short-circuited). Do not accept `skipped` unconditionally; otherwise an internal PR can go green without proving host-root reach or token ownership.

Recommended pattern (bash):

```bash
t4="${{ needs.t4-conformance.result }}"
is_fork_pr="${{ github.event_name == 'pull_request' && github.event.pull_request.head.repo.fork == true }}"
if [ "$t4" != "success" ]; then
  if [ "$t4" = "skipped" ] && [ "$is_fork_pr" = "true" ]; then
    echo "::notice::t4-conformance skipped on fork PR — allowing aggregate to pass."
  else
    echo "::error::t4-conformance did not succeed: $t4"
    exit 1
  fi
fi
```

## Adding a runtime adapter

1. Choose a RuntimeId that satisfies the open SDK contract. Do not add it to an allowlist.
2. Implement the adapter socket in its template repo and prove the native config, persona, MCP, and tool-enumeration surfaces.
3. Third-party adapters remain valid without appearing in the official registry. Promotion to first-party support requires an SDK PR updating the official adapter registry and reconciled delivery contract.
4. For a platform-managed image, add the image mapping in Core and confirm the template publish/conformance pipeline is green before rollout.
