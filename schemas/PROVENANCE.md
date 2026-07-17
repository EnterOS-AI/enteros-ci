# Vendored marketplace-artifact JSON-Schemas (SSOT mirror)

These `*.schema.json` files are **byte-for-byte copies** of the SSOT contract
schemas that live in the [`molecule-ai-sdk (contracts/)`](https://git.moleculesai.app/molecule-ai/molecule-ai-sdk)
repository (the marketplace-catalog contract family, RFC
[molecule-core#3285](https://git.moleculesai.app/molecule-ai/molecule-core/issues/3285)).

| Vendored copy | Source path in `molecule-ai-sdk (contracts/)` | Source commit |
| --- | --- | --- |
| `plugin-manifest.schema.json`    | `contracts/plugin-manifest/plugin-manifest.schema.json`       | `68f89520e508d6581fa522ac62b0074bd888dd96` (SDK PR #109) |
| `workspace-template.schema.json` | `contracts/workspace-template/workspace-template.schema.json` | `a3d70972ee082a8d862fd083ec6f92bbea133185` |
| `org-template.schema.json`       | `contracts/org-template/org-template.schema.json`             | `5588b7ce877c923d7249dc7d272244cfdcb3aca1` |
| `repo-meta.schema.json`          | `contracts/repo-meta/repo-meta.schema.json`                   | `faa0fecf` (SDK PR #116 merged — `node-package` added to knownCapability) |

`molecule-ai-sdk` main at re-vendor time: `0ff6e1bf09c2be6d08b56a53e88cffd7354ef9b0`
(SDK PR #98) for the three marketplace-artifact schemas;
`d60c7acf53dae697d1c061505e5ba9254ae474db` for `repo-meta.schema.json` (that main
contains `0d275cc`, the reviewed head of SDK PR #85). The source commits above are
reviewed PR heads contained by their re-vendor main: plugin-manifest re-vendored
from SDK PR #109 (contributes.digestProviders — this copy had drifted stale before
this re-vendor), workspace-template from SDK PR #92, org-template from SDK PR #98,
repo-meta from SDK PR #85.

> **`repo-meta.schema.json` is NOT a marketplace-artifact schema.** The other three
> capture heterogeneous *published artifacts* and are `additionalProperties:true`.
> `repo-meta` is the STRICT (`additionalProperties:false`) per-repo *routing* manifest
> the meta-CI router (`scripts/meta-ci.py`, task internal#57) reads to derive CI
> capability-bundles. It is vendored here for the same reason — the router validates
> `repo-meta.yaml` OFFLINE against this copy — and kept byte-honest by the same
> `check-schemas-in-sync.sh` drift gate.

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
2. Each `$id` points to its canonical path in `molecule-ai-sdk`. The value is
   copied byte-for-byte with the rest of the schema; a one-sided `$id` edit
   therefore reds the drift gate like any other contract change.

## How to update

When the contracts schemas change, re-vendor (do NOT hand-edit):

```sh
# from a molecule-ci checkout, with molecule-ai-sdk main cloned alongside
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
