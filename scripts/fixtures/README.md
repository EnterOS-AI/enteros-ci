# validate-workspace-template fixtures

Committed mini-templates exercised end-to-end (real CLI, real exit code) by
`test_validate_workspace_template.py::test_official_lint_cli_*`. They prove the
`--official` SSOT-inheritance gate (`check_no_hardcoded_provider_model`) reds on
a re-pinned official template and passes on a silent / inheriting one — the
principal's "enforce the SSOT, not just convention" rule.

- `official-inherit/`  — silent: NO top-level `model:`, NO `runtime_config.model`
  / `runtime_config.provider`, NO platform-proxy `base_url` pin. Exits 0 under
  `--official --static-only`.
- `official-repinned/` — re-pins all four (top-level `model:`,
  `runtime_config.model`, `runtime_config.provider`, and the platform LLM proxy
  `base_url`). Exits 1 under `--official`.

Both share a canonical Dockerfile + requirements.txt so the ONLY difference the
CLI test asserts on is the provider/model/proxy pin, not unrelated drift.
