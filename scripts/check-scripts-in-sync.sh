#!/usr/bin/env bash
# check-scripts-in-sync.sh — fail if the frozen public vendored-script copy in
# `.molecule-ci/scripts/` has drifted from its canonical SSOT in `scripts/`.
#
# WHY TWO COPIES EXIST
# --------------------
# `scripts/` is the CANONICAL location for every validator/lint script (the
# meta-CI router's bundles.yaml runs `{CI}/scripts/...`, molecule-ci's own
# ci.yml runs `scripts/...`, and the docs declare it). `.molecule-ci/scripts/`
# is a byte-identical MIRROR that external consumers pin: several org-template
# and plugin repos clone molecule-ci into `.molecule-ci-canonical` and invoke
#     python3 .molecule-ci-canonical/.molecule-ci/scripts/validate-*.py
# so the mirror directory is a public interface we cannot simply delete without
# breaking those repos' CI. (See docs/meta-ci-router.md "Script location
# duplication".)
#
# Two byte-identical copies with no sync guard WILL diverge silently — and had
# already begun to (`.molecule-ci/scripts/requirements.txt` had lost `pytest`,
# and lint_bp_context_emit_match.py existed only under `.molecule-ci/scripts/`).
# This gate is what keeps the mirror honest: for EVERY file present under
# `.molecule-ci/scripts/`, the same-named file must exist in `scripts/` and be
# byte-identical. `scripts/` is the source of truth; fix drift by re-copying
# FROM `scripts/` TO `.molecule-ci/scripts/`, never the reverse.
#
# Exit 0 = in sync. Exit 1 = drift (a mirror file is missing from canonical, or
# differs). No network fetch — this is a purely local invariant, so there is no
# soft-skip: a red here is always a real, actionable drift.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CANON_DIR="$REPO_ROOT/scripts"
MIRROR_DIR="$REPO_ROOT/.molecule-ci/scripts"

if [ ! -d "$MIRROR_DIR" ]; then
  echo "::error::mirror directory $MIRROR_DIR is missing — external consumers pin it; it must exist."
  exit 1
fi

drift=0
checked=0
# Iterate every file the mirror ships. Each MUST have a byte-identical canonical
# twin in scripts/. (We do not require the reverse: scripts/ legitimately holds
# test_*.py, fixtures/, shell helpers, and other files the vendored surface does
# not ship.)
while IFS= read -r -d '' mirror_file; do
  base="$(basename "$mirror_file")"
  canon_file="$CANON_DIR/$base"
  checked=$((checked + 1))
  if [ ! -f "$canon_file" ]; then
    echo "::error::.molecule-ci/scripts/$base has NO canonical twin in scripts/ — add it to scripts/ (the SSOT) so the mirror derives from it."
    drift=1
    continue
  fi
  if cmp -s "$canon_file" "$mirror_file"; then
    echo "OK   .molecule-ci/scripts/$base == scripts/$base"
  else
    echo "::error::DRIFT .molecule-ci/scripts/$base != scripts/$base"
    diff -u "$canon_file" "$mirror_file" || true
    drift=1
  fi
done < <(find "$MIRROR_DIR" -type f \
  -not -path '*/__pycache__/*' -not -name '*.pyc' -print0)

if [ "$checked" -eq 0 ]; then
  echo "::error::no files found under .molecule-ci/scripts/ — expected the vendored mirror to be non-empty."
  exit 1
fi

if [ "$drift" -ne 0 ]; then
  echo "::error::.molecule-ci/scripts/ has drifted from the canonical scripts/ SSOT."
  echo "Reconcile by copying FROM scripts/ TO .molecule-ci/scripts/ (scripts/ is canonical):"
  echo "  for f in .molecule-ci/scripts/*; do cp \"scripts/\$(basename \"\$f\")\" \"\$f\"; done"
  exit 1
fi

echo "All $checked vendored .molecule-ci/scripts/ files are in sync with the canonical scripts/ SSOT."
exit 0
