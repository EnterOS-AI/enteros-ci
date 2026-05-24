#!/usr/bin/env python3
"""Validate a Molecule AI org template repo."""
import os
import sys
import yaml

# Support custom YAML tags used by org templates. Two shapes:
#
#   - `!include teams/pm.yaml`  → scalar string referencing another YAML
#     file in the same repo. Platform inlines at load time.
#
#   - `!external\n  repo: ...\n  ref: ...\n  path: ...`  → mapping
#     referencing a workspace tree to fetch from another repo. Platform
#     fetches into a content-addressable cache at load time
#     (internal#77 / molecule-core#105).
#
# Both shapes resolve at platform load time, not at validation time.
# The validator treats them as opaque references — it does NOT chase
# them down. We mark each parsed value with a sentinel subtype so the
# `validate_workspace` walk knows to skip them rather than tripping
# the "missing 'name'" branch.
class IncludeRef(str):
    """`!include path/to.yaml` — opaque reference, skipped by validator."""

class ExternalRef(dict):
    """`!external` mapping — opaque reference, skipped by validator."""

class PermissiveLoader(yaml.SafeLoader):
    pass

def _include_constructor(loader, node):
    return IncludeRef(loader.construct_scalar(node))

def _external_constructor(loader, node):
    return ExternalRef(loader.construct_mapping(node))

def _generic_constructor(loader, tag_suffix, node):
    # Fallback for unknown tags. Preserve the parsed shape so legacy
    # docs that lean on tags we have not modeled yet still parse.
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)

PermissiveLoader.add_constructor("!include", _include_constructor)
PermissiveLoader.add_constructor("!external", _external_constructor)
PermissiveLoader.add_multi_constructor("!", _generic_constructor)

errors = []

if not os.path.isfile("org.yaml"):
    print("::error::org.yaml not found at repo root")
    sys.exit(1)

with open("org.yaml") as f:
    org = yaml.load(f, Loader=PermissiveLoader)

if not org.get("name"):
    errors.append("Missing required field: name")

if not org.get("workspaces") and not org.get("defaults"):
    errors.append("org.yaml must have at least 'workspaces' or 'defaults'")

def validate_workspace(ws, path=""):
    # `!include path/to.yaml` parses as IncludeRef (str subclass).
    # `!external {repo, ref, path}` parses as ExternalRef (dict subclass).
    # Both are opaque references — skip without chasing.
    if isinstance(ws, (IncludeRef, ExternalRef)):
        return []
    # Legacy unknown-tag scalars (handled by _generic_constructor) stay
    # as plain strings; they are not workspace dicts either.
    if not isinstance(ws, dict):
        return []
    ws_errors = []
    name = ws.get("name", "<unnamed>")
    full = f"{path}/{name}" if path else name
    if not ws.get("name"):
        ws_errors.append(f"Workspace at {full}: missing 'name'")
    plugins = ws.get("plugins", [])
    if plugins and not isinstance(plugins, list):
        ws_errors.append(f"{full}: 'plugins' must be a list")
    for child in ws.get("children", []):
        ws_errors.extend(validate_workspace(child, full))
    return ws_errors

for ws in org.get("workspaces", []):
    errors.extend(validate_workspace(ws))

if errors:
    for e in errors:
        print(f"::error::{e}")
    sys.exit(1)

def count_ws(nodes):
    c = 0
    for n in nodes:
        # Skip opaque references — we do not know how many workspaces
        # they expand to without resolving them, and resolution is the
        # platform's job, not the validator's.
        if isinstance(n, (IncludeRef, ExternalRef)):
            continue
        if not isinstance(n, dict):
            continue
        c += 1
        c += count_ws(n.get("children", []))
    return c

total = count_ws(org.get("workspaces", []))
print(f"✓ org.yaml valid: {org['name']} ({total} direct workspaces; external refs not counted)")
