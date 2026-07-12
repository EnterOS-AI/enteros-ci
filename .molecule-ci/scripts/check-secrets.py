#!/usr/bin/env python3
"""
Check for leaked credentials in the repo.
Uses context-aware matching to avoid false positives in documentation/examples.
"""
import os
import re
import sys
from pathlib import Path

# Patterns that match real credentials but also common doc examples.
# We match the full assignment/value context to distinguish real from example.
PATTERNS = [
    # sk-ant- in quoted export or assignment context (real key: 64 hex chars)
    re.compile(r'''["']sk-ant-[a-zA-Z0-9]{50,}["']'''),
    # ghp_ GitHub token (37+ chars after prefix)
    re.compile(r'''["']ghp_[a-zA-Z0-9]{36,}["']'''),
    # AWS access key IDs
    re.compile(r'''["']AKIA[A-Z0-9]{16}["']'''),
    # AWS secret access keys (40-char)
    re.compile(r'''["'][a-zA-Z0-9/+=]{40}["']'''),
    # Stripe test keys
    re.compile(r'''["']sk_test_[a-zA-Z0-9]{24,}["']'''),
    # Generic Bearer tokens
    re.compile(r'''["']Bearer\s+[a-zA-Z0-9_.-]{20,}["']'''),
    # Generic PAT tokens (ghp_)
    re.compile(r'''ghp_[a-zA-Z0-9]{36,}'''),
    # Generic sk-ant- (standalone, non-dotted, real length)
    re.compile(r'''sk-ant-[a-zA-Z0-9]{50,}'''),
]

# Extensions to scan
EXTENSIONS = {'.yaml', '.yml', '.md', '.py', '.sh'}

# Directories to skip entirely
SKIP_DIRS = {
    '.molecule-ci',
    '.molecule-ci-canonical',
    '.git',
    'node_modules',
    '__pycache__',
}


def is_false_positive(line: str, match: str) -> bool:
    """Heuristic: lines with ... or <example> or # comment-only are docs examples."""
    # If the match is followed by "..." or surrounded by "<" ">" it's an example
    ctx = line.lower()
    if '...' in ctx:
        return True
    if '<example' in ctx or '</example' in ctx:
        return True
    if '#' in line and line.strip().startswith('#'):
        # Pure comment line — likely a doc example
        return True
    return False


def check_file(path: Path) -> list[str]:
    """Return list of warnings for this file. Empty = clean."""
    warnings = []
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except Exception:
        return warnings

    for lineno, line in enumerate(lines, 1):
        for rule_number, pattern in enumerate(PATTERNS, 1):
            for match in pattern.finditer(line):
                if not is_false_positive(line, match.group(0)):
                    warnings.append(
                        f"  {path}:{lineno}: credential-shaped value "
                        f"(rule {rule_number})"
                    )
    return warnings


def main():
    root = Path(os.environ.get('GITHUB_WORKSPACE', '.'))
    all_warnings = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skipped dirs in-place
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for filename in filenames:
            if Path(filename).suffix not in EXTENSIONS:
                continue
            filepath = Path(dirpath) / filename
            all_warnings.extend(check_file(filepath))

    if all_warnings:
        print("::error::Potential secret found in committed files:")
        for w in all_warnings:
            print(f"  {w}")
        sys.exit(1)
    else:
        print("::notice::No secrets detected")


if __name__ == '__main__':
    main()
