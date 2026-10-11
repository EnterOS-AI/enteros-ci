# Vendored SDK JSON-Schemas (SSOT mirror)

These `*.schema.json` files are **byte-for-byte copies** of the SSOT contract
schemas that live in the [`molecule-ai-sdk (contracts/)`](https://git.moleculesai.app/molecule-ai/molecule-ai-sdk)
repository (the marketplace-catalog contract family, RFC
[molecule-core#3285](https://git.moleculesai.app/molecule-ai/molecule-core/issues/3285)).

| Vendored copy | Source path in `molecule-ai-sdk (contracts/)` | Source commit |
| --- | --- | --- |
| `plugin-manifest.schema.json`    | `contracts/plugin-manifest/plugin-manifest.schema.json`       | `8538b0e05261a33e7c88139a415fc2c1ec4cfd4a` (SDK PR #201 — `digestProviders` are not native-only, contract-version 0.7.0) |
| `workspace-template.schema.json` | `contracts/workspace-template/workspace-template.schema.json` | `2c4457da12b9978b7343da684ffe901d94ee3eac` (SDK PR #180 merge — schedule accepts `cron` or `cron_expr`) |
| `org-template.schema.json`       | `contracts/org-template/org-template.schema.json`             | `2c4457da12b9978b7343da684ffe901d94ee3eac` (SDK PR #179 merge — empty-declaration `anyOf` constraint) |
| `repo-meta.schema.json`          | `contracts/repo-meta/repo-meta.schema.json`                   | `faa0fecf` (SDK PR #116 merged — `node-package` added to knownCapability) |
| `schedule.schema.json`           | `contracts/schedule/schedule.schema.json`                     | `2eba5d9cd5ad3a936e659f6aa02e7c3202969c1d` (schedule-grid SSOT; stable lowercase-kebab entry names) |

The complete mirror snapshot is pinned in `schemas/SDK_SOURCE_COMMIT`. It is
currently SDK main commit
`1f6af8be118b33700c367e9ebce64f9b085a7716`; every row is fetched from that
same immutable commit even when the table records an older, last
contract-changing commit for the individual file.

> **`plugin-state` is NOT vendored here, deliberately.** SDK PR #182 also added
> `contracts/plugin-state/`, but that is a *delivery* contract — reserved daemon
> env vars, the per-plugin directory template and the degradation policy — whose
> consumers are the controlplane provisioner and the workspace runtime. Nothing
> in molecule-ci reads it: the validators here validate published *artifacts*
> (`plugin.yaml`), and the artifact-side surface of #182 is entirely
> `contributes.state` inside `plugin-manifest.schema.json`, which IS vendored.
> Vendoring an unused contract would add a drift-gate row with no consumer to
> keep it honest.

Note the sync gate enforces THREE things, not two: vendored == source pin,
AND source pin == molecule-ai-sdk `main`. So a merge to the SDK reds this repo
immediately — the pin cannot lag main, even by a description-only change. Re-vendor
in the same change that lands the SDK edit, or expect a red here.

> **`repo-meta.schema.json` is NOT a marketplace-artifact schema.** The other three
> capture heterogeneous *published artifacts* and are `additionalProperties:true`.
> `repo-meta` is the STRICT (`additionalProperties:false`) per-repo *routing* manifest
> the meta-CI router (`scripts/meta-ci.py`, task internal#57) reads to derive CI
> capability-bundles. It is vendored here for the same reason — the router validates
> `repo-meta.yaml` OFFLINE against this copy — and kept byte-honest by the same
> `check-schemas-in-sync.sh` drift gate.

> **`schedule.schema.json` is also not a marketplace-artifact schema.** The
> schedule wiring validator consumes its executable entry-name constraints to
> find plugin-config schedules the runtime seeder will skip. It enforces that
> subset (E0, entry not counted) only with `--enforce-sdk-schedule-names`;
> by default it reports a W1 warning and still counts the entry, because
> molecule-dev and reno-stars run the validator from unpinned molecule-ci
> `main` and renaming their schedules would start schedules that are dormant
> today. Keeping that subset tied to this byte-pinned copy prevents a
> duplicated Python regex from drifting away from the SDK and runtime contract.

IDL: JSON-Schema **draft 2020-12** (RFC §15 decision).

## Why vendored (and not fetched at validate time)

`scripts/validate-plugin.py`, `scripts/validate-workspace-template.py` and
`scripts/validate-org-template.py` validate the REAL artifact manifests
(`plugin.yaml` / `config.yaml` / `org.yaml`) against these schemas with
`jsonschema`'s `Draft202012Validator`; `scripts/validate_schedule_wiring.py`
loads the schedule entry-name subset directly. Consumer CI anonymously fetches an
immutable, verified `molecule-ci` commit; the validator itself performs no
authenticated cross-repo fetch of `molecule-ai-sdk (contracts/)`. The schemas
are therefore vendored here rather than pulled at validate time.

These copies are the **SSOT mirror, not a fork**. They MUST stay byte-identical
to the `molecule-ai-sdk (contracts/)` originals. Two things keep them honest:

1. `scripts/check-schemas-in-sync.sh` fetches the one immutable SDK commit in
   `schemas/SDK_SOURCE_COMMIT`, verifies the resolved commit, and byte-diffs all
   five schemas from that same snapshot. It also fetches current SDK `main` and
   requires the five canonical contracts there to match the pinned snapshot, so
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
SDK_COMMIT=d5046dbb872142dfd1d292827ade9a4bf33ca19d
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
safe_home="$tmp/anonymous-home"
mkdir -p "$safe_home/xdg"
chmod 0700 "$safe_home" "$safe_home/xdg"
safe_git() {
  env -u GIT_CONFIG_COUNT -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG \
    HOME="$safe_home" CURL_HOME="$safe_home" \
    XDG_CONFIG_HOME="$safe_home/xdg" \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null GIT_ASKPASS=/bin/false \
    SSH_ASKPASS=/bin/false GIT_TERMINAL_PROMPT=0 \
    git -c credential.helper= "$@"
}
safe_git -C "$tmp" init -q molecule-ai-sdk
safe_git -C "$tmp/molecule-ai-sdk" remote add origin \
  https://git.moleculesai.app/molecule-ai/molecule-ai-sdk.git
safe_git -C "$tmp/molecule-ai-sdk" -c http.userAgent=curl/8.4.0 \
  fetch --depth=1 origin "$SDK_COMMIT"
test "$(safe_git -C "$tmp/molecule-ai-sdk" rev-parse FETCH_HEAD)" = "$SDK_COMMIT"
for s in plugin-manifest workspace-template org-template repo-meta schedule; do
  safe_git -C "$tmp/molecule-ai-sdk" show \
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
