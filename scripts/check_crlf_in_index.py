#!/usr/bin/env python3
"""Fail if any tracked blob carries CRLF in the INDEX.

WHY THIS EXISTS
---------------
Several repositories in this org gate on BYTE-EXACT comparisons: the plugin
scaffold drift gate re-renders from the SDK and byte-compares the committed
daemon; the channel repos `cmp` their vendored channel_sdk.py against the SDK
copy; migrations hash atlas.sum. Those gates assume the committed bytes ARE the
canonical bytes.

A contributor with `core.autocrlf=true` (the Windows default, and the setting
this org's own box shipped with) gets CRLF in the working tree. Any tool that
rewrites a file byte-for-byte instead of going back through git's clean filter —
an editor, or a script reading with newline='' — then commits CRLF. Observed
2026-07-30: a single edit rewrote all 505 lines of the SDK's
templates/trigger/scheduler.py as CRLF, which would have failed the drift gate
in every consumer repo.

`.gitattributes` (`text eol=lf`) is the per-repo fix, but it only protects paths
someone REMEMBERED to pin, and a half-pinned byte comparison is worse than none:
in molecule-ai-sdk, `gen/**` was pinned while the file it is compared against was
not, so `tests/test_runtime_id_contract.py` fails DETERMINISTICALLY on a clean
checkout with no edits at all.

This check needs no such list. It asserts the invariant directly, at the source,
on the PR that introduces the contamination.

WHY THE INDEX AND NOT THE WORKTREE
----------------------------------
`git ls-files --eol` reports index (`i/`) and worktree (`w/`) endings
separately. The worktree reading is a property of the CHECKING machine's config
— on a box with autocrlf=true every text file legitimately reads `w/crlf`.
Only `i/` describes what was actually committed, so only `i/` is portable.

INTENTIONAL CRLF
----------------
Some files must be CRLF (`.bat`/`.cmd`, fixtures that exercise CRLF handling).
Rather than carry a second allowlist that drifts out of sync, this defers to
`.gitattributes`: a path declared `eol=crlf`, `-text`, or `binary` is a
DECLARED intent and is skipped. Declaring intent is the way to opt out, which
keeps the exemption reviewable in the same file that governs the bytes.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

# Printed on success so a workflow can prove the check actually EXECUTED. Gitea
# can report a job green with zero steps, so "no failure" is not evidence of a
# run — the selftest greps for this line (same convention as minimal_validate).
SENTINEL = "crlf-guard: executed"

# Index states that mean "this blob literally contains CR bytes".
BAD_INDEX_EOL = {"crlf", "mixed"}

# Attributes that DECLARE a file is not LF text; such a file is exempt.
EXEMPTING_ATTRS = ("eol=crlf", "-text", "binary")


def git_ls_files_eol(repo: str) -> list[tuple[str, str, str]]:
    """Return (index_eol, attrs, path) for every tracked file.

    `git ls-files --eol -z` emits records as:
        i/<eol> w/<eol> attr/<attrs><TAB><path>
    NUL-separated, so paths with spaces or newlines survive intact.
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
        index_eol, attrs = "", ""
        for field in meta.split():
            if field.startswith("i/"):
                index_eol = field[2:]
            elif field.startswith("attr/"):
                attrs = field[5:]
        rows.append((index_eol, attrs, path))
    return rows


def is_exempt(attrs: str) -> bool:
    return any(token in attrs for token in EXEMPTING_ATTRS)


def find_offenders(repo: str) -> list[str]:
    return sorted(
        path
        for index_eol, attrs, path in git_ls_files_eol(repo)
        if index_eol in BAD_INDEX_EOL and not is_exempt(attrs)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", default=".", help="repository root")
    args = parser.parse_args()

    try:
        offenders = find_offenders(args.repo)
    except subprocess.CalledProcessError as exc:
        # Fail CLOSED. An unreadable index must never be reported as clean.
        sys.stderr.write(
            f"::error::crlf-guard: could not read the git index in {args.repo!r} "
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
            "(.bat/.cmd, a CRLF fixture), declare that intent instead: "
            "'<path> eol=crlf' or '<path> -text'.\n"
        )
        return 1

    print(SENTINEL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
