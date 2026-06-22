#!/usr/bin/env python3
"""Validate a Molecule AI plugin repo."""
import os
import sys
import yaml

errors = []

# 1. plugin.yaml exists
if not os.path.isfile("plugin.yaml"):
    print("::error::plugin.yaml not found at repo root")
    sys.exit(1)

with open("plugin.yaml") as f:
    plugin = yaml.safe_load(f)

# 2. Required fields
for field in ["name", "version", "description"]:
    if not plugin.get(field):
        errors.append(f"Missing required field: {field}")

# 3. Version format
v = str(plugin.get("version", ""))
if v and not all(c in "0123456789." for c in v):
    errors.append(f"Invalid version format: {v}")

# 4. Runtimes type
runtimes = plugin.get("runtimes")
if runtimes is not None and not isinstance(runtimes, list):
    errors.append(f"runtimes must be a list, got {type(runtimes).__name__}")

# 5. Has content — kind-aware.
#
# Two plugin content classes exist in the ecosystem:
#
#   * Skill-class plugins (the default, kind unset or one of the skill
#     kinds below): their content is declarative — SKILL.md / hooks/ /
#     skills/ / rules/. At least one MUST be present or the plugin is
#     empty.
#
#   * Code-class plugins (e.g. kind: env-mutator): their content is
#     compiled source — a Go module (go.mod) wired through a declared
#     `entrypoint` (e.g. pluginloader.BuildRegistry) — not any of the
#     skill markers. Requiring SKILL.md/hooks/skills/rules of these is a
#     false positive (the validator previously red-flagged the
#     legitimate molecule-gh-identity env-mutator plugin, which has no
#     skill markers by design). For these, the Go content + entrypoint
#     IS the content.
#
# We recognize a code-class plugin by an explicit `kind` that is not a
# skill kind. Such a plugin satisfies the content requirement when it
# ships compiled-source content (a go.mod) and declares an entrypoint —
# both required so an empty repo can't escape the check just by setting
# `kind:`.
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

# 6. SKILL.md formatting check
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
if runtimes:
    print(f"  Runtimes: {', '.join(runtimes)}")
