#!/usr/bin/env bash
# sync-scripts.sh — regenerate the public `.molecule-ci/scripts/` mirror FROM
# the canonical `scripts/` SSOT, per `.molecule-ci/scripts/MIRROR.manifest`.
#
# `scripts/` is canonical. `.molecule-ci/scripts/` is a byte-identical mirror
# that external org-template/plugin repos pin, so it must stay in lockstep with
# canonical but cannot be deleted. This is the ONE command that reconciles drift:
# it copies every basename listed in the manifest FROM scripts/ TO
# .molecule-ci/scripts/, so a fix is "run this generator", never "remember to
# hand-copy N files one by one".
#
# The companion guard `scripts/check-scripts-in-sync.sh` (and its pytest twin
# scripts/test_scripts_in_sync.py) verify that the committed mirror is EXACTLY
# what this generator would produce — enforcing the manifest set in both
# directions (no mirror file missing from canonical, and no manifest entry
# missing from the mirror).
#
# Usage:
#   bash scripts/sync-scripts.sh          # regenerate the mirror in place
#   bash scripts/sync-scripts.sh --check  # verify only; exit 1 on any drift
#                                         # (does not write) — same invariant the
#                                         # guard enforces, reused by that guard.
#
# Exit 0 = mirror matches the manifest+canonical. In --check mode, exit 1 = drift.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CANON_DIR="$REPO_ROOT/scripts"
MIRROR_DIR="$REPO_ROOT/.molecule-ci/scripts"
MANIFEST="$MIRROR_DIR/MIRROR.manifest"

CHECK_ONLY=0
if [ "${1:-}" = "--check" ]; then
  CHECK_ONLY=1
elif [ -n "${1:-}" ]; then
  echo "usage: $0 [--check]" >&2
  exit 2
fi

if [ ! -f "$MANIFEST" ]; then
  echo "::error::manifest $MANIFEST is missing — it lists the vendored script surface."
  exit 1
fi

# Read the manifest: one basename per line, ignore blanks and # comments.
mapfile -t entries < <(sed -e 's/#.*//' -e 's/[[:space:]]*$//' "$MANIFEST" | grep -v '^[[:space:]]*$')

if [ "${#entries[@]}" -eq 0 ]; then
  echo "::error::manifest $MANIFEST lists no files — expected a non-empty vendored surface."
  exit 1
fi

mkdir -p "$MIRROR_DIR"
drift=0

for name in "${entries[@]}"; do
  # Reject path separators — the manifest is basenames within scripts/ only.
  case "$name" in
    */*|..|.) echo "::error::manifest entry '$name' must be a bare filename in scripts/."; drift=1; continue ;;
  esac
  src="$CANON_DIR/$name"
  dst="$MIRROR_DIR/$name"
  if [ ! -f "$src" ]; then
    echo "::error::manifest lists '$name' but scripts/$name does not exist (canonical is the SSOT)."
    drift=1
    continue
  fi
  if [ "$CHECK_ONLY" -eq 1 ]; then
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
      echo "OK   .molecule-ci/scripts/$name == scripts/$name"
    else
      echo "::error::DRIFT .molecule-ci/scripts/$name != scripts/$name (run: bash scripts/sync-scripts.sh)"
      drift=1
    fi
  else
    cp "$src" "$dst"
    echo "synced scripts/$name -> .molecule-ci/scripts/$name"
  fi
done

# Detect mirror files that are NOT declared in the manifest (stale vendored
# files, or a file added to the mirror by hand without a manifest entry). The
# manifest + MIRROR.manifest itself are the only non-script members allowed.
while IFS= read -r -d '' mfile; do
  base="$(basename "$mfile")"
  [ "$base" = "MIRROR.manifest" ] && continue
  found=0
  for name in "${entries[@]}"; do
    [ "$name" = "$base" ] && { found=1; break; }
  done
  if [ "$found" -eq 0 ]; then
    echo "::error::.molecule-ci/scripts/$base is present but NOT listed in MIRROR.manifest — add it to the manifest or remove the file."
    drift=1
  fi
done < <(find "$MIRROR_DIR" -type f -not -path '*/__pycache__/*' -not -name '*.pyc' -print0)

if [ "$drift" -ne 0 ]; then
  if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "::error::.molecule-ci/scripts/ is out of sync with the canonical scripts/ SSOT + manifest."
  fi
  exit 1
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "All ${#entries[@]} vendored .molecule-ci/scripts/ files are in sync with canonical scripts/ + manifest."
else
  echo "Regenerated ${#entries[@]} vendored .molecule-ci/scripts/ files from canonical scripts/."
fi
exit 0
