# Vendored marketplace-artifact JSON-Schemas (SSOT mirror)

These `*.schema.json` files are **byte-for-byte copies** of the SSOT contract
schemas that live in the [`molecule-contracts`](https://git.moleculesai.app/molecule-ai/molecule-contracts)
repository (the marketplace-catalog contract family, RFC
[molecule-core#3285](https://git.moleculesai.app/molecule-ai/molecule-core/issues/3285)).

| Vendored copy | Source path in `molecule-contracts` | Source commit |
| --- | --- | --- |
| `plugin-manifest.schema.json`    | `plugin-manifest/plugin-manifest.schema.json`       | `afa509bfbbfb7a1169662a52f72708239f9d80ed` |
| `workspace-template.schema.json` | `workspace-template/workspace-template.schema.json` | `afa509bfbbfb7a1169662a52f72708239f9d80ed` |
| `org-template.schema.json`       | `org-template/org-template.schema.json`             | `cef11de620ab010928dd057a101338798e11ebe5` |

`molecule-contracts` main at vendoring time: `057c7b3ba5499c99fb3f767d755bb61fa4fd62bd`.

IDL: JSON-Schema **draft 2020-12** (RFC §15 decision).

## Why vendored (and not fetched at validate time)

`scripts/validate-plugin.py`, `scripts/validate-workspace-template.py` and
`scripts/validate-org-template.py` validate the REAL artifact manifests
(`plugin.yaml` / `config.yaml` / `org.yaml`) against these schemas with
`jsonschema`'s `Draft202012Validator`. The validators run inside each artifact
repo's CI **offline** (anonymous `git clone` of `molecule-ci` only — no
authenticated cross-repo fetch of `molecule-contracts`), so the schemas are
vendored here rather than pulled at validate time.

These copies are the **SSOT mirror, not a fork**. They MUST stay byte-identical
to the `molecule-contracts` originals. Two things keep them honest:

1. `scripts/check-schemas-in-sync.sh` re-fetches each schema from
   `molecule-contracts` **main** and `diff`s it against the vendored copy,
   failing if they have drifted. It runs in CI via
   `.gitea/workflows/schema-sync.yml`.
2. The `$id` inside each schema points at the canonical `molecule-contracts`
   URL — a tripwire against silent edits.

## How to update

When the contracts schemas change, re-vendor (do NOT hand-edit):

```sh
# from a molecule-ci checkout, with molecule-contracts main cloned alongside
for s in plugin-manifest workspace-template org-template; do
  curl -fsS -A "curl/8.4.0" \
    "https://git.moleculesai.app/molecule-ai/molecule-contracts/raw/branch/main/$s/$s.schema.json" \
    -o "schemas/$s.schema.json"
done
# then bump the source-commit SHAs in the table above
bash scripts/check-schemas-in-sync.sh   # must pass
```

If re-vendoring would make a currently-conforming artifact fail, that is a
**schema gap** — widen the schema in `molecule-contracts` first (open a PR
there), then re-vendor. NEVER loosen the validator to paper over it.
