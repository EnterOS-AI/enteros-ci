#!/usr/bin/env bash
# check-scripts-in-sync.sh — fail if the frozen public vendored-script copy in
# `.molecule-ci/scripts/` has drifted from its canonical SSOT in `scripts/`.
#
# WHY TWO COPIES EXIST
# --------------------
# `scripts/` is the CANONICAL location for every validator/lint script
# (molecule-ci's own ci.yml runs `scripts/...`, and the future meta-CI router —
# task #57, not yet built in this repo — will run `{CI}/scripts/...`).
# `.molecule-ci/scripts/` is a byte-identical MIRROR that external consumers
# pin: several org-template and plugin repos clone molecule-ci and invoke
#     python3 .../.molecule-ci/scripts/validate-*.py
# so the mirror directory is a public interface we cannot simply delete without
# breaking those repos' CI.
#
# Two byte-identical copies with no sync guard WILL diverge silently — and had
# already begun to (`.molecule-ci/scripts/requirements.txt` had lost `pytest`,
# and lint_bp_context_emit_match.py existed only under `.molecule-ci/scripts/`).
#
# HOW THE INVARIANT IS DEFINED AND ENFORCED
# -----------------------------------------
# The authoritative vendored surface is the explicit list in
# `.molecule-ci/scripts/MIRROR.manifest`. The generator `scripts/sync-scripts.sh`
# regenerates the mirror FROM canonical per that manifest (fix-once: run the
# generator, don't hand-copy). This guard is that same generator in `--check`
# mode: it verifies the committed mirror is EXACTLY what the generator would
# produce, so it catches BOTH directions of drift —
#   * a mirror file that diverged from (or is missing in) canonical, AND
#   * a manifest entry that was never copied into the mirror, AND
#   * a mirror file not declared in the manifest (stale / hand-added).
# Fix any drift with: `bash scripts/sync-scripts.sh` (then commit the mirror).
#
# Exit 0 = in sync. Exit 1 = drift. No network fetch — this is a purely local
# invariant, so there is no soft-skip: a red here is always a real, actionable
# drift.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The generator is the single source of the byte-identical invariant; running it
# in --check mode keeps this guard and the reconcile path from ever disagreeing.
exec bash "$REPO_ROOT/scripts/sync-scripts.sh" --check
