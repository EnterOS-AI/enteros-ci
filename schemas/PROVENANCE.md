# Vendored marketplace-artifact JSON-Schemas (SSOT mirror)

These `*.schema.json` files are **byte-for-byte copies** of the SSOT contract
schemas that live in the [`molecule-ai-sdk (contracts/)`](https://git.moleculesai.app/molecule-ai/molecule-ai-sdk)
repository (the marketplace-catalog contract family, RFC
[molecule-core#3285](https://git.moleculesai.app/molecule-ai/molecule-core/issues/3285)).

| Vendored copy | Source path in `molecule-ai-sdk (contracts/)` | Source commit |
| --- | --- | --- |
| `plugin-manifest.schema.json`    | `contracts/plugin-manifest/plugin-manifest.schema.json`       | `fb83b093b742724ae7b3714927522583b2bf983c` (SDK PR #121 merge) |
| `workspace-template.schema.json` | `contracts/workspace-template/workspace-template.schema.json` | `a3d70972ee082a8d862fd083ec6f92bbea133185` |
| `org-template.schema.json`       | `contracts/org-template/org-template.schema.json`             | `191f677a76a2973556554b580c4563bd2a572943` (SDK PR #120 native-channel root tombstone) |
| `repo-meta.schema.json`          | `contracts/repo-meta/repo-meta.schema.json`                   | `faa0fecf` (SDK PR #116 merged — `node-package` added to knownCapability) |

The complete mirror snapshot is pinned in `schemas/SDK_SOURCE_COMMIT`. It is
currently SDK PR #120 head
`cf89e25144478ec131264b32fff31c8cbed520f3`; every row is fetched from that
same immutable commit even when the table records an older, last
contract-changing commit for the individual file. This molecule-ci change must
remain unmerged until that rebased SDK head is merged. Until then the required
sync gate intentionally reports the unmerged org-template contraction; the
other three mirrored schemas already match current SDK main. After SDK #120
merges, replace the pin with its merge commit and re-vendor all four schemas
from that exact commit before making this PR mergeable.

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

1. `scripts/check-schemas-in-sync.sh` fetches the one immutable SDK commit in
   `schemas/SDK_SOURCE_COMMIT`, verifies the resolved commit, and byte-diffs all
   four schemas from that same snapshot. It also fetches current SDK `main` and
   requires the four canonical contracts there to match the pinned snapshot, so
   the mirror cannot silently freeze on an old commit. A source pin whose
   mirrored contracts differ from main, an invalid pin, fetch failure, missing
   source, or parity mismatch fails closed in required CI via
   `.gitea/workflows/schema-sync.yml`.
   That workflow runs on every molecule-ci pull request and main push, and is a
   `workflow_dispatch` receiver for the SDK's contract-path, push-to-main
   notifier ([molecule-ai-sdk#138](https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/pulls/138)).
2. Each `$id` points to its canonical path in `molecule-ai-sdk`. The value is
   copied byte-for-byte with the rest of the schema; a one-sided `$id` edit
   therefore reds the drift gate like any other contract change.

## How to update

When the contracts schemas change, re-vendor (do NOT hand-edit):

```sh
# Pin this to the exact molecule-ai-sdk commit verified before the update.
SDK_COMMIT=cf89e25144478ec131264b32fff31c8cbed520f3
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git -C "$tmp" init -q molecule-ai-sdk
git -C "$tmp/molecule-ai-sdk" remote add origin \
  https://git.moleculesai.app/molecule-ai/molecule-ai-sdk.git
GIT_TERMINAL_PROMPT=0 git -C "$tmp/molecule-ai-sdk" \
  -c credential.helper= -c http.userAgent=curl/8.4.0 \
  fetch --depth=1 origin "$SDK_COMMIT"
test "$(git -C "$tmp/molecule-ai-sdk" rev-parse FETCH_HEAD)" = "$SDK_COMMIT"
for s in plugin-manifest workspace-template org-template repo-meta; do
  git -C "$tmp/molecule-ai-sdk" show \
    "FETCH_HEAD:contracts/$s/$s.schema.json" > "$tmp/$s.schema.json"
  python3 -m json.tool "$tmp/$s.schema.json" >/dev/null
  cp "$tmp/$s.schema.json" "schemas/$s.schema.json"
done
# Record SDK_COMMIT in schemas/SDK_SOURCE_COMMIT and update the table's
# last contract-changing commit for each changed path.
bash scripts/check-schemas-in-sync.sh   # must pass
```

If re-vendoring would make a currently-conforming artifact fail, that is a
**schema gap** — widen the schema in `molecule-ai-sdk (contracts/)` first (open a PR
there), then re-vendor. NEVER loosen the validator to paper over it.
