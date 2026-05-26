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

# 5. Has content
content_paths = ["SKILL.md", "hooks", "skills", "rules"]
found = [p for p in content_paths if os.path.exists(p)]
if not found:
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
if runtimes:
    print(f"  Runtimes: {', '.join(runtimes)}")
