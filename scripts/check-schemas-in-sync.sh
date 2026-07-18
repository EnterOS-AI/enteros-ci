#!/usr/bin/env bash
# check-schemas-in-sync.sh — fail if the vendored marketplace-artifact
# JSON-Schemas in schemas/ have drifted from one exact molecule-ai-sdk main
# snapshot.
#
# The schemas under schemas/ are a byte-for-byte SSOT mirror of the
# molecule-ai-sdk contracts/ originals (see schemas/PROVENANCE.md). The validators
# (validate-plugin / validate-workspace-template / validate-org-template) run
# OFFLINE against the vendored copies, so this gate is what keeps the mirror
# honest: it clones main once, resolves that checkout to an immutable commit,
# and diffs every schema against that coherent snapshot.
#
# Mirrors molecule-ai-workspace-runtime#196 (vendored workspace-comms schemas +
# drift gate). molecule-ai-sdk is public, so the fetch is anonymous — no
# token needed (same posture as the anonymous molecule-ci clone the validate-*
# workflows already use).
#
# Exit 0 = in sync. Exit 1 = drift (vendored copy != resolved SDK snapshot).
# Exit 2 = source checkout/verification unavailable. Both non-zero outcomes
# fail CI because an unverifiable SSOT mirror must never be accepted.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEMA_DIR="$REPO_ROOT/schemas"
SDK_REPO="https://git.moleculesai.app/molecule-ai/molecule-ai-sdk.git"
UA="curl/8.4.0"

# vendored-copy basename  ->  path within molecule-ai-sdk (contracts/)
declare -A MAP=(
  [plugin-manifest.schema.json]="contracts/plugin-manifest/plugin-manifest.schema.json"
  [workspace-template.schema.json]="contracts/workspace-template/workspace-template.schema.json"
  [org-template.schema.json]="contracts/org-template/org-template.schema.json"
  [repo-meta.schema.json]="contracts/repo-meta/repo-meta.schema.json"
)

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

sdk="$tmp/molecule-ai-sdk"
if ! git -c http.userAgent="$UA" clone \
  --quiet --depth 1 --branch main --single-branch --no-tags \
  "$SDK_REPO" "$sdk"; then
  echo "::error::could not clone molecule-ai-sdk main; schema SSOT cannot be verified"
  exit 2
fi
if ! SDK_COMMIT="$(git -C "$sdk" rev-parse --verify HEAD)" \
  || [[ ! "$SDK_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::molecule-ai-sdk clone did not resolve a canonical commit"
  exit 2
fi
echo "Schema SSOT snapshot: molecule-ai-sdk@$SDK_COMMIT"

drift=0
for local_name in "${!MAP[@]}"; do
  remote_path="${MAP[$local_name]}"
  local_file="$SCHEMA_DIR/$local_name"
  source_file="$sdk/$remote_path"
  if [ ! -f "$local_file" ]; then
    echo "::error::vendored schema missing: schemas/$local_name"
    drift=1
    continue
  fi
  if [ ! -f "$source_file" ]; then
    echo "::error::source schema missing at molecule-ai-sdk@$SDK_COMMIT:$remote_path"
    drift=1
    continue
  fi
  if diff -u "$local_file" "$source_file" > "$tmp/$local_name.diff"; then
    echo "OK   schemas/$local_name == molecule-ai-sdk@$SDK_COMMIT:$remote_path"
  else
    echo "::error::DRIFT schemas/$local_name has drifted from molecule-ai-sdk@$SDK_COMMIT:$remote_path"
    cat "$tmp/$local_name.diff"
    drift=1
  fi
done

if [ "$drift" -ne 0 ]; then
  echo "::error::Vendored schemas are out of sync with molecule-ai-sdk@$SDK_COMMIT."
  echo "Re-vendor per schemas/PROVENANCE.md and bump the source-commit SHAs."
  exit 1
fi
echo "All vendored schemas are in sync with molecule-ai-sdk@$SDK_COMMIT."
exit 0
