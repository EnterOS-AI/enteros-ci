#!/usr/bin/env python3
"""Validate a Molecule AI org template repo."""
import os
import sys
import yaml

# Support !include and other custom YAML tags used by org templates.
# These resolve at platform load time, not at validation time — we just
# need to parse past them without crashing.
class PermissiveLoader(yaml.SafeLoader):
    pass

def _generic_constructor(loader, tag_suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)

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
    # !include tags resolve to strings at parse time; skip non-dicts
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
        if not isinstance(n, dict):
            continue
        c += 1
        c += count_ws(n.get("children", []))
    return c

total = count_ws(org.get("workspaces", []))
print(f"✓ org.yaml valid: {org['name']} ({total} workspaces)")
