#!/usr/bin/env python3
"""Validate a Molecule AI plugin repo.

SSOT switch (RFC molecule-core#3285): the field / required-key / version /
runtimes-shape / RuntimeId checks are NO LONGER hand-rolled here — they are
delegated to the marketplace plugin-manifest JSON-Schema (draft 2020-12)
vendored from molecule-ai-sdk at schemas/plugin-manifest.schema.json. That
schema is the real authority for the manifest shape; this script just loads
plugin.yaml, validates it against the schema, and reports the violations in
molecule-ci's own (test-stable) voice.

What stays hand-rolled because the schema CANNOT express it (out-of-band,
filesystem-level checks):

  * plugin.yaml existence at the repo root.
  * Content presence — at least one of SKILL.md / hooks/ / skills/ / rules/
    on disk, OR (for a code-class plugin, e.g. kind: env-mutator) a go.mod +
    a declared entrypoint. This is a filesystem check (does the repo actually
    ship content?), not a manifest-shape check.
  * The SKILL.md markdown-heading formatting nudge.
"""
import json
import os
import sys
from pathlib import Path

import yaml

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print(
        "::error::jsonschema not installed — validate-plugin.py validates "
        "plugin.yaml against the vendored molecule-ai-sdk schema and needs "
        "`pip install jsonschema`. (CI installs it; see the validate-plugin "
        "workflow.)"
    )
    sys.exit(1)


def _find_schema(name: str) -> Path:
    """Locate a vendored schema by walking up from this script to the repo
    root's schemas/ dir. Works whether this file is invoked as
    scripts/validate-plugin.py or .molecule-ci/scripts/validate-plugin.py."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "schemas" / name
        if cand.is_file():
            return cand
    print(f"::error::vendored schema not found: schemas/{name} (looked up from {here})")
    sys.exit(1)


errors: list[str] = []

# 1. plugin.yaml exists (filesystem — schema can't express this).
if not os.path.isfile("plugin.yaml"):
    print("::error::plugin.yaml not found at repo root")
    sys.exit(1)

with open("plugin.yaml") as f:
    plugin = yaml.safe_load(f)

if not isinstance(plugin, dict):
    print("::error::plugin.yaml must be a mapping at the top level")
    sys.exit(1)

# 2-4. Manifest-shape validation against the molecule-ai-sdk SSOT schema.
#      Replaces the former hand-rolled required-field / version-format /
#      runtimes-must-be-a-list checks AND enforces the open, bounded/path-safe
#      RuntimeId contract. Violations are formatted into the
#      pre-existing message strings so the gate stays actionable + stable.
schema = json.loads(_find_schema("plugin-manifest.schema.json").read_text())
for e in sorted(Draft202012Validator(schema).iter_errors(plugin), key=lambda e: list(e.path)):
    if e.validator == "required":
        # Map the schema-required violation to the legacy per-field message for
        # each top-level required prop actually missing.
        for prop in schema.get("required", []):
            if prop not in plugin and f"'{prop}'" in e.message:
                errors.append(f"Missing required field: {prop}")
    elif e.validator == "pattern" and list(e.path) == ["version"]:
        errors.append(f"Invalid version format: {e.instance}")
    elif e.validator == "type" and list(e.path) == ["runtimes"]:
        got = type(e.instance).__name__
        errors.append(f"runtimes must be a list, got {got}")
    else:
        loc = "/".join(str(p) for p in e.path) or "(root)"
        errors.append(f"plugin.yaml schema violation at `{loc}`: {e.message}")

# 5. Content presence — kind-aware. FILESYSTEM check (out-of-band; the schema
#    governs the manifest, not what files the repo ships).
#
#    A plugin ships its content in one of four shapes. Content is proven if ANY
#    holds — a plugin is free to combine them:
#
#      * SKILL-class — declarative: SKILL.md / hooks/ / skills/ / rules/.
#      * DAEMON-class — a `contributes.daemons` entry whose command/args name a
#        file that actually exists in the repo (e.g. molecule-scheduler's
#        `python scheduler.py`). The entry file IS the content.
#      * LAUNCHER-class — a `contributes.mcpServers` entry with name+command.
#        The server is fetched at run time (e.g. molecule-platform's
#        `npx @molecule-ai/mcp-server`), so there is deliberately NO local file
#        to check; the declaration is the content.
#      * GO code-class — a Go module (go.mod) wired through a declared
#        `entrypoint` (e.g. molecule-gh-identity's env-mutator).
#
#    History: this check previously treated EVERY non-skill kind as Go and
#    demanded go.mod + entrypoint. That was written for `kind: env-mutator` and
#    false-positived on the two shipping plugins that are neither skill nor Go —
#    molecule-scheduler (kind: trigger, Python) and molecule-platform
#    (kind: mcp-server, npx). Both failed on every run; because this gate is
#    wired advisory in those repos, the error was never surfaced and the
#    plugin-manifest check was effectively dead for them.
SKILL_KINDS = {"", "skill", "agent-skill", "claude-skill"}
SKILL_CONTENT_PATHS = ["SKILL.md", "hooks", "skills", "rules"]

contributes = plugin.get("contributes") or {}
if not isinstance(contributes, dict):
    contributes = {}


def _daemon_entry_files():
    """Repo files a declared daemon actually executes.

    Only repo-relative, non-flag, non-escaping paths count — an absolute path
    or a `../` traversal is not this repo's content.
    """
    hits = []
    daemons = contributes.get("daemons")
    if not isinstance(daemons, list):
        return hits
    for entry in daemons:
        if not isinstance(entry, dict):
            continue
        candidates = []
        command = entry.get("command")
        if isinstance(command, str):
            candidates.append(command)
        args = entry.get("args")
        if isinstance(args, list):
            candidates.extend(a for a in args if isinstance(a, str))
        for candidate in candidates:
            if not candidate or candidate.startswith("-") or os.path.isabs(candidate):
                continue
            if ".." in candidate.split(os.sep):
                continue
            if os.path.isfile(candidate):
                hits.append(candidate)
    return hits


def _launcher_servers():
    """Well-formed mcpServers entries — a name and a command to launch."""
    servers = contributes.get("mcpServers")
    if not isinstance(servers, list):
        return []
    return [
        s for s in servers
        if isinstance(s, dict)
        and str(s.get("name", "") or "").strip()
        and str(s.get("command", "") or "").strip()
    ]


kind = str(plugin.get("kind", "") or "").strip().lower()
found = [p for p in SKILL_CONTENT_PATHS if os.path.exists(p)]
daemon_files = _daemon_entry_files()
launchers = _launcher_servers()
has_go = os.path.isfile("go.mod")
has_entrypoint = bool(str(plugin.get("entrypoint", "") or "").strip())

if found or daemon_files or launchers or (has_go and has_entrypoint):
    # Content proven by at least one of the four shapes.
    pass
elif kind not in SKILL_KINDS:
    if has_go or has_entrypoint:
        # Half a Go plugin — keep the precise diagnostic rather than the
        # generic one, so the author is told exactly which half is missing.
        missing = []
        if not has_go:
            missing.append("go.mod")
        if not has_entrypoint:
            missing.append("entrypoint")
        errors.append(
            f"Code-class plugin (kind: {kind}) must ship its content as "
            f"go.mod + an entrypoint; missing: {', '.join(missing)}"
        )
    else:
        errors.append(
            f"Plugin (kind: {kind}) ships no recognisable content. Provide one of: "
            f"skill content (SKILL.md, hooks/, skills/, rules/); a contributes.daemons "
            f"entry whose command/args name a file in this repo; a contributes.mcpServers "
            f"entry with name + command; or go.mod + a declared entrypoint."
        )
else:
    errors.append("Plugin must contain at least one of: SKILL.md, hooks/, skills/, rules/")

# 6. SKILL.md formatting check (out-of-band nudge).
if os.path.isfile("SKILL.md"):
    with open("SKILL.md") as f:
        first_line = f.readline().strip()
    if first_line and not first_line.startswith("#"):
        print("::warning::SKILL.md should start with a markdown heading (e.g., # Plugin Name)")

if errors:
    for e in errors:
        print(f"::error::{e}")
    sys.exit(1)

print(f"✓ plugin.yaml valid: {plugin['name']} v{plugin['version']}")
if found:
    print(f"  Content: {', '.join(found)}")
elif daemon_files:
    print(f"  Content: daemon entry {', '.join(sorted(set(daemon_files)))} [kind: {kind}]")
elif launchers:
    names = ', '.join(str(s.get('name')) for s in launchers)
    print(f"  Content: mcpServers launcher ({names}) [kind: {kind}]")
elif kind not in SKILL_KINDS:
    print(f"  Content: go.mod + entrypoint ({plugin.get('entrypoint')}) [kind: {kind}]")
runtimes = plugin.get("runtimes")
if runtimes:
    print(f"  Runtimes: {', '.join(runtimes)}")
