#!/usr/bin/env python3
"""Fail if any tracked blob carries CRLF in the INDEX.

WHY THIS EXISTS
---------------
Several repositories in this org gate on BYTE-EXACT comparisons: the channel
repos `cmp` their vendored channel_sdk.py against the SDK canonical; the SDK's
own tests/test_runtime_id_contract.py asserts `read_bytes() == read_bytes()`;
migrations hashes atlas.sum; operator-config re-renders manifests and `diff -u`s
them. Those gates assume the committed bytes ARE the canonical bytes.

A contributor with `core.autocrlf=true` (the Windows default, and the setting
this org's own box shipped with) gets CRLF in the working tree. Any tool that
rewrites a file byte-for-byte instead of going back through git's clean filter
then commits CRLF. Observed 2026-07-30: a single edit rewrote all 505 lines of
the SDK's templates/trigger/scheduler.py as CRLF.

Scope note, so this file does not overclaim: NOT every drift gate would have
caught that. molecule-ai-plugin-scheduler's check_scaffold_drift.py compares
with `read_text()`, whose universal-newline translation makes a CRLF file
compare EQUAL — it is newline-tolerant by accident. The byte-exact gates listed
above are the ones that break, and they are why this guard exists.

`.gitattributes` (`text eol=lf`) is the per-repo fix, but it only protects paths
someone REMEMBERED to pin, and a half-pinned byte comparison is worse than none:
in molecule-ai-sdk, `gen/python/runtime_ids_gen.py` is pinned while
`molecule_plugin/_runtime_ids.py`, the file it is compared against, is not — so
that contract test fails DETERMINISTICALLY on a clean checkout. This check needs
no such list.

WHY THE INDEX AND NOT THE WORKTREE
----------------------------------
`git ls-files --eol` reports index (`i/`) and worktree (`w/`) endings
separately. The worktree reading is a property of the CHECKING machine's config
— on a box with autocrlf=true every text file legitimately reads `w/crlf`.
Only `i/` describes what was actually committed, so only `i/` is portable.

DECLARING INTENTIONAL CRLF: USE `-text`, NEVER `eol=crlf`
---------------------------------------------------------
`-text` (equivalently `binary`, which git expands to `-text`) is the ONLY way to
keep CRLF in the index, and therefore the only opt-out this guard honours.

`eol=crlf` looks like the obvious spelling and is a trap: it implies `text`, so
`git add` runs the clean filter and stores the blob as **LF**. A fixture
declared `eol=crlf` is silently renormalized — the CRLF bytes it exists to carry
are destroyed on the way into the index. The guard then passes, but only because
the content it was protecting is gone. Recommending `eol=crlf` would mean this
gate's own remediation introduced byte-level drift into a byte-exactness gate,
so it is deliberately not offered.

KNOWN LIMITS (documented rather than hidden)
--------------------------------------------
1. This asserts git's notion of a text blob, not a raw byte scan. If a blob
   contains a NUL or a lone CR in its first 8000 bytes, git classifies it
   `i/-text` and CRLF inside it is NOT reported — the same heuristic every other
   git eol behaviour keys on, so a file git treats as binary is one whose endings
   git was never going to normalize anyway. CRLF appearing only after byte 8000
   IS caught (`i/mixed`).
2. `-text` is frequently set for diff-noise suppression or LFS rather than to
   declare intentional CRLF. Wherever such a rule already exists this guard is
   silent on those paths by construction. Audit existing `-text` rules when
   adopting.
3. A runner-level `core.attributesFile` containing `* -text` would silence the
   guard entirely. The consumer template neutralizes ambient git config for
   exactly this reason.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

# Printed on success so a workflow can prove the check actually EXECUTED. Gitea
# can report a job green with zero steps, so "no failure" is not evidence of a
# run — the workflows grep for this line. Format matches the house convention
# (`minimal-validate:sentinel:executed`, `secret-scan:sentinel:executed`).
SENTINEL = "crlf-guard:sentinel:executed"

# Index states that mean "this blob literally contains CR bytes".
BAD_INDEX_EOL = {"crlf", "mixed"}

# The ONLY attribute that keeps CRLF in the index (see module docstring).
# `binary` is deliberately absent: git expands it to `-text` before it ever
# reaches us, so listing it would be dead code that reads as coverage.
EXEMPTING_ATTRS = ("-text",)


def repo_toplevel(path: str) -> str:
    """Resolve the working-tree root.

    `git ls-files` is PATH-SCOPED: run from a subdirectory it reports only that
    subtree, so a contaminated file elsewhere would be missed and the sentinel
    printed anyway — a fail-open dressed up as proof of execution.
    """
    out = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
    ).stdout
    return out.decode("utf-8", "surrogateescape").strip()


def git_ls_files_eol(repo: str) -> list[tuple[str, str, str]]:
    """Return (index_eol, attrs, path) for every tracked file.

    `git ls-files --eol -z` emits records as:
        i/<eol> w/<eol> attr/<attrs><TAB><path>
    NUL-separated, so paths with spaces or newlines survive intact.

    `<attrs>` may itself contain spaces (`attr/text eol=crlf`), so the attribute
    field is everything after the `attr/` marker. Splitting on whitespace and
    keeping only the token that starts with `attr/` silently drops every
    attribute but the first.
    """
    out = subprocess.run(
        ["git", "-C", repo, "ls-files", "--eol", "-z"],
        capture_output=True,
        check=True,
    ).stdout
    rows: list[tuple[str, str, str]] = []
    for record in out.split(b"\0"):
        if not record.strip():
            continue
        text = record.decode("utf-8", "surrogateescape")
        meta, _, path = text.partition("\t")
        if not path:
            continue
        index_eol = ""
        for field in meta.split():
            if field.startswith("i/"):
                index_eol = field[2:]
                break
        attrs = meta.partition("attr/")[2].strip()
        rows.append((index_eol, attrs, path))
    return rows


def is_exempt(attrs: str) -> bool:
    """True when the path DECLARES it is not LF text.

    Whole-token matching: a substring test would let an attribute such as
    `-textconv`, or a filter whose name contains `-text`, exempt a file by
    accident.
    """
    return any(token in EXEMPTING_ATTRS for token in attrs.split())


def find_offenders(repo: str) -> list[str]:
    """Offending paths anywhere in the repo containing `repo`.

    Resolves the toplevel HERE rather than in main() so the fail-open is
    impossible at the API level too: a caller passing a subdirectory must not
    silently get a clean result for a contaminated repository.
    """
    return sorted(
        path
        for index_eol, attrs, path in git_ls_files_eol(repo_toplevel(repo))
        if index_eol in BAD_INDEX_EOL and not is_exempt(attrs)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", default=".", help="path inside the repo")
    args = parser.parse_args()

    try:
        offenders = find_offenders(args.repo)
    except subprocess.CalledProcessError as exc:
        # Fail CLOSED. An unreadable index must never be reported as clean.
        sys.stderr.write(
            f"::error::crlf-guard: could not read the git index at {args.repo!r} "
            f"(git exited {exc.returncode}); refusing to report clean\n"
        )
        return 2

    if offenders:
        sys.stderr.write(
            "::error::crlf-guard: %d tracked file(s) contain CRLF in the index. "
            "Byte-exact drift gates compare committed bytes, so this breaks them "
            "in every consumer repo.\n" % len(offenders)
        )
        for path in offenders:
            sys.stderr.write(f"::error::  {path}\n")
        sys.stderr.write(
            "::error::Fix: add a '<path> text eol=lf' rule to .gitattributes, run "
            "'git add --renormalize .', and commit. If a file is INTENTIONALLY CRLF "
            "(.bat/.cmd, a CRLF fixture), declare '<path> -text' — do NOT use "
            "'eol=crlf', which implies 'text' and makes git renormalize the blob to "
            "LF on add, destroying the very bytes you meant to keep.\n"
        )
        return 1

    print(SENTINEL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
