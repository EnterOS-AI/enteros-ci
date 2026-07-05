# Vendored marketplace-artifact JSON-Schemas (SSOT mirror)

These `*.schema.json` files are **byte-for-byte copies** of the SSOT contract
schemas that live in the [`molecule-ai-sdk (contracts/)`](https://git.moleculesai.app/molecule-ai/molecule-ai-sdk)
repository (the marketplace-catalog contract family, RFC
[molecule-core#3285](https://git.moleculesai.app/molecule-ai/molecule-core/issues/3285)).

| Vendored copy | Source path in `molecule-ai-sdk (contracts/)` | Source commit |
| --- | --- | --- |
| `plugin-manifest.schema.json`    | `contracts/plugin-manifest/plugin-manifest.schema.json`       | `56f7248455ee1a1b6a5e9f7885800d03f8f2493b` |
| `workspace-template.schema.json` | `contracts/workspace-template/workspace-template.schema.json` | `56f7248455ee1a1b6a5e9f7885800d03f8f2493b` |
| `org-template.schema.json`       | `contracts/org-template/org-template.schema.json`             | `56f7248455ee1a1b6a5e9f7885800d03f8f2493b` |

`molecule-ai-sdk` main at repoint time: `423469623a1e071f5b74e9d28a3d8a8211408823`. (Source commits above are
`molecule-ai-sdk` history — the contracts were folded in from the retired
`molecule-contracts` repo, whose pre-fold history is preserved there read-only.)

IDL: JSON-Schema **draft 2020-12** (RFC §15 decision).

## Why vendored (and not fetched at validate time)

`scripts/validate-plugin.py`, `scripts/validate-workspace-template.py` and
`scripts/validate-org-template.py` validate the REAL artifact manifests
(`plugin.yaml` / `config.yaml` / `org.yaml`) against these schemas with
`jsonschema`'s `Draft202012Validator`. The validators run inside each artifact
repo's CI **offline** (anonymous `git clone` of `molecule-ci` only — no
authenticated cross-repo fetch of `molecule-ai-sdk (contracts/)`), so the schemas are
vendored here rather than pulled at validate time.

These copies are the **SSOT mirror, not a fork**. They MUST stay byte-identical
to the `molecule-ai-sdk (contracts/)` originals. Two things keep them honest:

1. `scripts/check-schemas-in-sync.sh` re-fetches each schema from
   `molecule-ai-sdk (contracts/)` **main** and `diff`s it against the vendored copy,
   failing if they have drifted. It runs in CI via
   `.gitea/workflows/schema-sync.yml`.
2. The `$id` inside each schema still carries the URL of the retired
   `molecule-contracts` repo — inherited byte-for-byte from the fold-in
   (molecule-ai-sdk deliberately did not rewrite it, and this mirror must
   stay byte-identical to the SSOT, so it is NOT rewritten here either).
   If the SSOT ever updates the `$id`s, the byte-diff gate above goes red
   until this mirror is re-vendored — that is the intended tripwire.

## How to update

When the contracts schemas change, re-vendor (do NOT hand-edit):

```sh
# from a molecule-ci checkout, with molecule-ai-sdk (contracts/) main cloned alongside
for s in plugin-manifest workspace-template org-template; do
  curl -fsS -A "curl/8.4.0" \
    "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/$s/$s.schema.json" \
    -o "schemas/$s.schema.json"
done
# then bump the source-commit SHAs in the table above
bash scripts/check-schemas-in-sync.sh   # must pass
```

If re-vendoring would make a currently-conforming artifact fail, that is a
**schema gap** — widen the schema in `molecule-ai-sdk (contracts/)` first (open a PR
there), then re-vendor. NEVER loosen the validator to paper over it.
