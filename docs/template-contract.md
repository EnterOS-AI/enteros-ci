# Workspace Template Contract

Hard rules every `molecule-ai-workspace-template-*` repo must satisfy. Enforced by `scripts/validate-workspace-template.py` on every CI run via the reusable `validate-workspace-template.yml` workflow.

The contract exists because the 8 template repos were extracted from a single monolithic Dockerfile pre-#87, and have drifted as each was edited piecemeal since. Without this gate, a 28-line cascade-friendly Dockerfile in one repo silently regresses to a 25-line non-cache-friendly one in another, and the next runtime publish ships the previous wheel from a stale layer (cache trap observed five times in a row on 2026-04-27).

## Dockerfile

| Rule | Why |
|---|---|
| `FROM python:3.11-slim` | Single base everywhere — keeps apt + pip behaviour identical and lets us reason about CVE patches on one base. |
| `ARG RUNTIME_VERSION=` declared | The arg invalidates the pip-install layer's cache key whenever the cascade publishes a new wheel. Without it the cache hit replays the previous runtime. |
| `${RUNTIME_VERSION}` referenced in a `RUN` | Just declaring the ARG isn't enough — it has to be in the layer's command line so docker hashes it. Pattern: `if [ -n "${RUNTIME_VERSION}" ]; then pip install --no-cache-dir --upgrade "molecule-ai-workspace-runtime==${RUNTIME_VERSION}"; fi` |
| `RUN useradd -u 1000 -m -s /bin/bash agent` | The runtime drops to uid 1000 before exec'ing the SDK. Claude Code refuses `--dangerously-skip-permissions` as root for safety. The `/workspace` volume is also chown'd to 1000 by the platform provisioner. |
| `ENTRYPOINT ["molecule-runtime"]` *or* a wrapper script that exec's `molecule-runtime` | Single entrypoint means the platform's container-restart contract is uniform across templates. Wrapper scripts are allowed (claude-code has `entrypoint.sh` for gosu drop-priv; hermes has `start.sh` to boot the hermes-agent daemon first). |
| `molecule-ai-workspace-runtime` listed in `requirements.txt` (or installed in the Dockerfile directly) | The runtime wheel is the contract — without it the container has no A2A server, no heartbeat, no MCP bridge. |

## config.yaml

| Required key | Type | Notes |
|---|---|---|
| `name` | str | Human-readable; appears on the canvas card. |
| `runtime` | str | Must be one of: `langgraph`, `claude-code`, `crewai`, `autogen`, `deepagents`, `hermes`, `gemini-cli`, `openclaw`. Custom runtimes warn but are allowed. |
| `template_schema_version` | int | Currently `1`. Bump when adding a key that changes how the platform consumes config.yaml. **Must be int**, not string — a quoted `"1"` will fail validation. |

| Optional key | Notes |
|---|---|
| `description` | Free text, surfaces on canvas. |
| `version`, `tier` | int, controls platform-side rollout gating. |
| `model`, `models` | Either a single model id or a list of model ids the agent may use. |
| `runtime_config` | Nested block of runtime-specific settings (used by claude-code, gemini-cli, hermes). |
| `env`, `skills`, `tools`, `a2a`, `delegation`, `prompt_files`, `bridge`, `governance` | Optional feature blocks. Add new keys to `OPTIONAL_KEYS` in the validator when introducing them. |

Unknown top-level keys produce a warning (not an error) so accidental drift is visible without blocking.

## adapter.py

Optional. When present, `adapter.py` should:
- Import `BaseAdapter` from `molecule_runtime.adapter_base`.
- Override `setup()` and `create_executor()` for the runtime's specific entry point.

The pre-#87 import path (`molecule_ai`) produces a warning if it appears.

## requirements.txt

Must declare `molecule-ai-workspace-runtime` (with a version pin or floor).

## CI

Every template repo's `.github/workflows/ci.yml` should be a one-liner that calls the canonical reusable workflow:

```yaml
name: CI
on: [push, pull_request]
jobs:
  validate:
    uses: molecule-ai/molecule-ci/.gitea/workflows/validate-workspace-template.yml@v1
```

The reusable workflow checks out `molecule-ci` itself (into `.molecule-ci-canonical`) and runs the canonical `validate-workspace-template.py` from there — so no per-repo vendoring of the script is needed. The legacy `.molecule-ci/scripts/` directory in each template repo is being phased out.

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

## Adding a new runtime

1. Add the runtime name to `KNOWN_RUNTIMES` in `scripts/validate-workspace-template.py`.
2. Add the runtime + image ref to `RuntimeImages` in `molecule-core/workspace-server/internal/provisioner/provisioner.go`.
3. Stand up the `molecule-ai-workspace-template-<runtime>` repo from the existing template-of-templates pattern (issue #105 covers this).
4. Confirm CI green on the new repo before opening it for general use.
