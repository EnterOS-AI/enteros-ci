#!/usr/bin/env python3
"""Validate a Molecule AI plugin repo.

SSOT switch (RFC molecule-core#3285): the field / required-key / version /
runtimes-shape / runtime-enum checks are NO LONGER hand-rolled here — they are
delegated to the marketplace plugin-manifest JSON-Schema (draft 2020-12)
vendored from molecule-contracts at schemas/plugin-manifest.schema.json. That
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
        "plugin.yaml against the vendored molecule-contracts schema and needs "
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

# 2-4. Manifest-shape validation against the molecule-contracts SSOT schema.
#      Replaces the former hand-rolled required-field / version-format /
#      runtimes-must-be-a-list checks AND adds the canonical runtimes enum the
#      hand-rolled validator never enforced. Violations are formatted into the
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
    elif e.validator == "enum" and len(e.path) == 2 and e.path[0] == "runtimes":
        errors.append(
            f"runtimes[{e.path[1]}]: `{e.instance}` is not a canonical runtime — "
            f"allowed (molecule-contracts plugin-manifest enum): {e.validator_value}"
        )
    else:
        loc = "/".join(str(p) for p in e.path) or "(root)"
        errors.append(f"plugin.yaml schema violation at `{loc}`: {e.message}")

# 5. Content presence — kind-aware. FILESYSTEM check (out-of-band; the schema
#    governs the manifest, not what files the repo ships).
#
#   * Skill-class plugins (kind unset or a skill kind): content is declarative
#     — SKILL.md / hooks/ / skills/ / rules/. At least one MUST be present.
#   * Code-class plugins (e.g. kind: env-mutator): content is compiled source —
#     a Go module (go.mod) wired through a declared `entrypoint`. Requiring the
#     skill markers of these is a false positive (it red-flagged the legitimate
#     molecule-gh-identity env-mutator plugin). For these, go.mod + entrypoint
#     IS the content.
SKILL_KINDS = {"", "skill", "agent-skill", "claude-skill"}
SKILL_CONTENT_PATHS = ["SKILL.md", "hooks", "skills", "rules"]

kind = str(plugin.get("kind", "") or "").strip().lower()
found = [p for p in SKILL_CONTENT_PATHS if os.path.exists(p)]

if found:
    # Skill-class content present — always accepted (any plugin may ship it).
    pass
elif kind not in SKILL_KINDS:
    # Code-class plugin (e.g. env-mutator). Content = Go module + entrypoint.
    has_go = os.path.isfile("go.mod")
    has_entrypoint = bool(str(plugin.get("entrypoint", "") or "").strip())
    if not has_go or not has_entrypoint:
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
elif kind not in SKILL_KINDS:
    print(f"  Content: go.mod + entrypoint ({plugin.get('entrypoint')}) [kind: {kind}]")
runtimes = plugin.get("runtimes")
if runtimes:
    print(f"  Runtimes: {', '.join(runtimes)}")
