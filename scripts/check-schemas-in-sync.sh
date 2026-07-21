#!/usr/bin/env bash
# check-schemas-in-sync.sh — fail if the vendored marketplace-artifact
# JSON-Schemas in schemas/ have drifted from one immutable molecule-ai-sdk
# contracts snapshot.
#
# The schemas under schemas/ are a byte-for-byte SSOT mirror of the
# molecule-ai-sdk contracts/ originals (see schemas/PROVENANCE.md). The validators
# (validate-plugin / validate-workspace-template / validate-org-template) run
# OFFLINE against the vendored copies, so this gate is what keeps the mirror
# honest: it re-fetches each schema from the exact commit recorded in
# schemas/SDK_SOURCE_COMMIT and diffs it.
#
# Mirrors molecule-ai-workspace-runtime#196 (vendored workspace-comms schemas +
# drift gate). molecule-ai-sdk is public, so the fetch is anonymous — no
# token needed (same posture as the anonymous molecule-ci clone the validate-*
# workflows already use).
#
# Exit 0 = in sync. Exit 1 = drift or an invalid source pin. Exit 2 = source
# could not be fetched. Both non-zero outcomes are terminal in required CI:
# unverified parity must never become a green status.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEMA_DIR="$REPO_ROOT/schemas"
SOURCE_COMMIT_FILE="$SCHEMA_DIR/SDK_SOURCE_COMMIT"
SDK_URL="https://git.moleculesai.app/molecule-ai/molecule-ai-sdk.git"

# Ignore host-level Git rewrites and credential helpers on self-hosted runners.
# This check is intentionally anonymous and must always address the canonical
# SDK remote above, never a cached mirror selected by ambient Git config.
safe_git() {
  env -u GIT_CONFIG_COUNT \
    -u GIT_CONFIG_PARAMETERS \
    -u GIT_CONFIG \
    HOME="$SAFE_GIT_HOME" \
    CURL_HOME="$SAFE_GIT_HOME" \
    XDG_CONFIG_HOME="$SAFE_GIT_HOME/xdg" \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null \
    GIT_ASKPASS=/bin/false \
    SSH_ASKPASS=/bin/false \
    GIT_TERMINAL_PROMPT=0 \
    git -c credential.helper= "$@"
}

if [ ! -f "$SOURCE_COMMIT_FILE" ]; then
  echo "::error::missing schemas/SDK_SOURCE_COMMIT" >&2
  exit 1
fi
SDK_COMMIT="$(tr -d '[:space:]' < "$SOURCE_COMMIT_FILE")"
case "$SDK_COMMIT" in
  *[!0-9a-f]*|'')
    echo "::error::schemas/SDK_SOURCE_COMMIT must contain one lowercase 40-character commit SHA" >&2
    exit 1
    ;;
esac
if [ "${#SDK_COMMIT}" -ne 40 ]; then
  echo "::error::schemas/SDK_SOURCE_COMMIT must contain one lowercase 40-character commit SHA" >&2
  exit 1
fi

# vendored-copy basename  ->  path within molecule-ai-sdk (contracts/)
declare -A MAP=(
  [plugin-manifest.schema.json]="contracts/plugin-manifest/plugin-manifest.schema.json"
  [workspace-template.schema.json]="contracts/workspace-template/workspace-template.schema.json"
  [org-template.schema.json]="contracts/org-template/org-template.schema.json"
  [repo-meta.schema.json]="contracts/repo-meta/repo-meta.schema.json"
)

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
SAFE_GIT_HOME="$tmp/anonymous-home"
mkdir -p "$SAFE_GIT_HOME/xdg"
chmod 0700 "$SAFE_GIT_HOME" "$SAFE_GIT_HOME/xdg"

drift=0
sdk_checkout="$tmp/molecule-ai-sdk"
if ! safe_git -C "$tmp" init -q molecule-ai-sdk || \
    ! safe_git -C "$sdk_checkout" remote add origin "$SDK_URL"; then
  echo "::error::could not initialize the immutable SDK schema fetch" >&2
  exit 2
fi

fetched=0
for attempt in 1 2 3; do
  if safe_git -C "$sdk_checkout" \
      -c http.userAgent=curl/8.4.0 \
      fetch --depth=1 origin "$SDK_COMMIT"; then
    fetched=1
    break
  fi
  echo "::warning::SDK schema fetch attempt $attempt failed" >&2
done
if [ "$fetched" -ne 1 ]; then
  echo "::error::could not fetch molecule-ai-sdk commit $SDK_COMMIT; failing closed" >&2
  exit 2
fi

fetched_commit="$(safe_git -C "$sdk_checkout" rev-parse FETCH_HEAD 2>/dev/null || true)"
if [ "$fetched_commit" != "$SDK_COMMIT" ]; then
  echo "::error::SDK fetch resolved $fetched_commit, expected $SDK_COMMIT" >&2
  exit 1
fi

# The immutable pin makes one update deterministic; comparing that snapshot to
# current SDK main keeps the mirror from silently freezing forever. A source
# commit whose mirrored contract bytes are not on main yet, or a later SDK
# contract change that has not been re-vendored, must keep this check red.
main_fetched=0
for attempt in 1 2 3; do
  if safe_git -C "$sdk_checkout" \
      -c http.userAgent=curl/8.4.0 \
      fetch --depth=1 origin main; then
    main_fetched=1
    break
  fi
  echo "::warning::SDK main fetch attempt $attempt failed" >&2
done
if [ "$main_fetched" -ne 1 ]; then
  echo "::error::could not fetch molecule-ai-sdk main; failing closed" >&2
  exit 2
fi
SDK_MAIN_COMMIT="$(safe_git -C "$sdk_checkout" rev-parse FETCH_HEAD 2>/dev/null || true)"
if ! [[ "$SDK_MAIN_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::SDK main resolved to invalid commit $SDK_MAIN_COMMIT" >&2
  exit 1
fi

for local_name in "${!MAP[@]}"; do
  remote_path="${MAP[$local_name]}"
  local_file="$SCHEMA_DIR/$local_name"
  if [ ! -f "$local_file" ]; then
    echo "::error::vendored schema missing: schemas/$local_name"
    drift=1
    continue
  fi
  source_file="$tmp/source-$local_name"
  main_file="$tmp/main-$local_name"
  if ! safe_git -C "$sdk_checkout" show "$SDK_COMMIT:$remote_path" > "$source_file"; then
    echo "::error::$remote_path is absent from molecule-ai-sdk commit $SDK_COMMIT" >&2
    drift=1
    continue
  fi
  if ! safe_git -C "$sdk_checkout" show "$SDK_MAIN_COMMIT:$remote_path" > "$main_file"; then
    echo "::error::$remote_path is absent from molecule-ai-sdk main at $SDK_MAIN_COMMIT" >&2
    drift=1
    continue
  fi
  if diff -u "$local_file" "$source_file" > "$tmp/$local_name.diff"; then
    echo "OK   schemas/$local_name == molecule-ai-sdk:$remote_path"
  else
    echo "::error::DRIFT schemas/$local_name has drifted from molecule-ai-sdk@$SDK_COMMIT:$remote_path"
    cat "$tmp/$local_name.diff"
    drift=1
  fi
  if ! diff -u "$source_file" "$main_file" > "$tmp/$local_name-main.diff"; then
    echo "::error::SOURCE PIN schemas/$local_name at $SDK_COMMIT does not match molecule-ai-sdk main at $SDK_MAIN_COMMIT"
    cat "$tmp/$local_name-main.diff"
    drift=1
  fi
done

if [ "$drift" -ne 0 ]; then
  echo "::error::Vendored schemas, their immutable source pin, and molecule-ai-sdk main are not in lockstep."
  echo "Re-vendor per schemas/PROVENANCE.md and update schemas/SDK_SOURCE_COMMIT."
  exit 1
fi
echo "All vendored schemas match molecule-ai-sdk contracts at $SDK_COMMIT and current main $SDK_MAIN_COMMIT."
exit 0
