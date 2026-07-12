#!/usr/bin/env python3
"""Validate a Molecule AI org template repo.

SSOT switch (RFC molecule-core#3285): the required-key / "workspaces-or-defaults"
/ per-workspace-name / plugins-must-be-a-list checks are NO LONGER hand-rolled —
they are delegated to the marketplace org-template JSON-Schema (draft 2020-12)
vendored from molecule-ai-sdk at schemas/org-template.schema.json. That schema
is the real authority for the org.yaml shape (including the recursive workspace
node tree and the THREE workspace-item forms: resolved inline node, unresolved
`!external` ref object, unresolved `!include` path string).

What stays hand-rolled because the schema CANNOT express it (out-of-band):

  * org.yaml existence at the repo root.
  * The informational direct-workspace count (skips unresolved !include /
    !external refs — resolving them is the platform's job, not the validator's).

The custom YAML tags (`!include`, `!external`) resolve at platform LOAD time, not
at validation time. We parse PAST them into the exact shapes the schema models:
`!include path/to.yaml` -> a plain string; `!external {repo,ref,path}` -> a plain
mapping. Both are opaque pre-resolution references the validator does not chase.
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
        "::error::jsonschema not installed — validate-org-template.py validates "
        "org.yaml against the vendored molecule-ai-sdk schema and needs "
        "`pip install jsonschema`. (CI installs it; see the validate-org-template "
        "workflow.)"
    )
    sys.exit(1)


def _find_schema(name: str) -> Path:
    """Locate a vendored schema by walking up from this script to the repo
    root's schemas/ dir. Works whether invoked as scripts/validate-org-template.py
    or .molecule-ci/scripts/validate-org-template.py."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "schemas" / name
        if cand.is_file():
            return cand
    print(f"::error::vendored schema not found: schemas/{name} (looked up from {here})")
    sys.exit(1)


# Support custom YAML tags used by org templates. Two shapes:
#
#   - `!include teams/pm.yaml`  -> scalar string referencing another YAML file
#     in the same repo. Platform inlines at load time.
#   - `!external\n  repo: ...\n  ref: ...\n  path: ...`  -> mapping referencing a
#     workspace tree to fetch from another repo (internal#77 / molecule-core#105).
#
# Both resolve at platform load time, not at validation time. We parse them into
# str / dict subclasses so (a) the schema validates them as string / object via
# the unresolved-ref oneOf branches and (b) the direct-workspace count can skip
# them without chasing.
class IncludeRef(str):
    """`!include path/to.yaml` — opaque reference, skipped by the count."""

class ExternalRef(dict):
    """`!external` mapping — opaque reference, skipped by the count."""

class PermissiveLoader(yaml.SafeLoader):
    pass

def _include_constructor(loader, node):
    return IncludeRef(loader.construct_scalar(node))

def _external_constructor(loader, node):
    return ExternalRef(loader.construct_mapping(node))

def _generic_constructor(loader, tag_suffix, node):
    # Fallback for unknown tags. Preserve the parsed shape so legacy docs that
    # lean on tags we have not modeled yet still parse.
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)

PermissiveLoader.add_constructor("!include", _include_constructor)
PermissiveLoader.add_constructor("!external", _external_constructor)
PermissiveLoader.add_multi_constructor("!", _generic_constructor)

errors: list[str] = []

# 1. org.yaml exists (filesystem — schema can't express this).
if not os.path.isfile("org.yaml"):
    print("::error::org.yaml not found at repo root")
    sys.exit(1)

with open("org.yaml") as f:
    org = yaml.load(f, Loader=PermissiveLoader)

if not isinstance(org, dict):
    print("::error::org.yaml must be a mapping at the top level")
    sys.exit(1)

# 2. Shape validation against the molecule-ai-sdk SSOT schema. Replaces the
#    former hand-rolled required-name / workspaces-or-defaults / per-workspace
#    name / plugins-is-a-list checks (and the recursive children walk).
schema = json.loads(_find_schema("org-template.schema.json").read_text())
for e in sorted(Draft202012Validator(schema).iter_errors(org), key=lambda e: list(e.path)):
    loc = "/".join(str(p) for p in e.path) or "(root)"
    errors.append(f"org.yaml schema violation at `{loc}`: {e.message}")

if errors:
    for e in errors:
        print(f"::error::{e}")
    sys.exit(1)

# 3. Informational direct-workspace count (out-of-band). Skips opaque refs — we
#    do not know how many workspaces they expand to without resolving them, and
#    resolution is the platform's job, not the validator's.
def count_ws(nodes):
    c = 0
    for n in nodes:
        if isinstance(n, (IncludeRef, ExternalRef)) or not isinstance(n, dict):
            continue
        c += 1
        c += count_ws(n.get("children", []))
    return c

ws = org.get("workspaces", [])
total = count_ws(ws) if isinstance(ws, list) else len(ws or {})
print(f"✓ org.yaml valid: {org['name']} ({total} direct workspaces; external refs not counted)")
