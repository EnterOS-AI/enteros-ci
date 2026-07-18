# Vendored marketplace-artifact JSON-Schemas (SSOT mirror)

These `*.schema.json` files are **byte-for-byte copies** of the SSOT contract
schemas that live in the [`molecule-ai-sdk (contracts/)`](https://git.moleculesai.app/molecule-ai/molecule-ai-sdk)
repository (the marketplace-catalog contract family, RFC
[molecule-core#3285](https://git.moleculesai.app/molecule-ai/molecule-core/issues/3285)).

| Vendored copy | Source path in `molecule-ai-sdk (contracts/)` | Source commit |
| --- | --- | --- |
| `plugin-manifest.schema.json`    | `contracts/plugin-manifest/plugin-manifest.schema.json`       | `bdf41eb0517087acc47c74233755a37425fcd1b7` (SDK PR #119 merge) |
| `workspace-template.schema.json` | `contracts/workspace-template/workspace-template.schema.json` | `a3d70972ee082a8d862fd083ec6f92bbea133185` |
| `org-template.schema.json`       | `contracts/org-template/org-template.schema.json`             | `5588b7ce877c923d7249dc7d272244cfdcb3aca1` |
| `repo-meta.schema.json`          | `contracts/repo-meta/repo-meta.schema.json`                   | `faa0fecf` (SDK PR #116 merged — `node-package` added to knownCapability) |

`molecule-ai-sdk` main at re-vendor time:
`bdf41eb0517087acc47c74233755a37425fcd1b7` (SDK PR #119). The source commits
above are the latest contract-changing commits for each path and are all
contained by that main: plugin-manifest from PR #119, workspace-template from
PR #92, org-template from PR #98, and repo-meta from PR #116.

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
`jsonschema`'s `Draft202012Validator`. Consumer CI anonymously fetches an
immutable, verified `molecule-ci` commit; the validator itself performs no
authenticated cross-repo fetch of `molecule-ai-sdk (contracts/)`. The schemas
are therefore vendored here rather than pulled at validate time.

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
# Pin this to the exact molecule-ai-sdk main verified before the update.
SDK_COMMIT=bdf41eb0517087acc47c74233755a37425fcd1b7
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for s in plugin-manifest workspace-template org-template repo-meta; do
  curl -fsS -A "curl/8.4.0" \
    "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/commit/$SDK_COMMIT/contracts/$s/$s.schema.json" \
    -o "$tmp/$s.schema.json"
  python3 -m json.tool "$tmp/$s.schema.json" >/dev/null
  cp "$tmp/$s.schema.json" "schemas/$s.schema.json"
done
# then bump the source-commit SHAs in the table above
bash scripts/check-schemas-in-sync.sh   # must pass
```

If re-vendoring would make a currently-conforming artifact fail, that is a
**schema gap** — widen the schema in `molecule-ai-sdk (contracts/)` first (open a PR
there), then re-vendor. NEVER loosen the validator to paper over it.
