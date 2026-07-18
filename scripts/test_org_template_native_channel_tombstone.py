"""Regression coverage for the SDK 0.6 org-channel contract removal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "org-template.schema.json"


def validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "document",
    [
        {"name": "retired", "defaults": {"channels": []}},
        {"name": "retired", "workspaces": [{"name": "root", "channels": None}]},
        {
            "name": "retired",
            "workspaces": [
                {
                    "name": "root",
                    "children": [
                        {
                            "name": "child",
                            "channels": [{"type": "telegram", "enabled": False}],
                        }
                    ],
                }
            ],
        },
        {
            "name": "retired",
            "workspaces": [
                {
                    "repo": "molecule-ai/example",
                    "ref": "v1",
                    "path": "workspace.yaml",
                    "channels": [],
                }
            ],
        },
        {
            "name": "retired",
            "workspaces": {"root": {"name": "root", "channels": []}},
        },
    ],
)
def test_rejects_retired_native_channel_field_everywhere(document: dict) -> None:
    assert list(validator().iter_errors(document)), document


def test_preserves_plugins_and_category_routing_channels_business_key() -> None:
    document = {
        "name": "plugin channels",
        "defaults": {
            "plugins": ["telegram-channel"],
            "category_routing": {"channels": ["Community Manager"]},
        },
        "workspaces": [
            {
                "name": "Community Manager",
                "plugins": ["discord-channel"],
                "category_routing": {"channels": ["Support Lead"]},
            }
        ],
    }
    assert list(validator().iter_errors(document)) == []


def test_native_channel_definition_is_removed_not_merely_unused() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert "channel" not in schema["$defs"]
    assert "channels" not in schema["$defs"]["workspaceNode"]["properties"]
    assert schema["$defs"]["workspaceNode"]["not"] == {"required": ["channels"]}
    assert schema["$defs"]["unresolvedExternalRef"]["not"] == {
        "required": ["channels"]
    }
