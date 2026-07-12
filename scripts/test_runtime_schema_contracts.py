"""RuntimeId behavior shared by the three vendored SDK schemas."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = (
    "plugin-manifest.schema.json",
    "workspace-template.schema.json",
    "org-template.schema.json",
)


@pytest.mark.parametrize("name", SCHEMAS)
def test_runtime_id_is_open_bounded_and_path_safe(name: str) -> None:
    schema = json.loads((REPO_ROOT / "schemas" / name).read_text())
    assert schema["$id"].startswith(
        "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/"
    )
    validator = Draft202012Validator(schema["$defs"]["runtimeId"])

    for value in ("claude-code", "claude_code", "acme-agent", "acme_agent", "a" * 64):
        assert validator.is_valid(value), f"{name} rejected {value!r}"
    for value in (
        "",
        "../acme",
        "acme/agent",
        "acme agent",
        "acme\n",
        "acme\r",
        "acme--agent",
        "a" * 65,
    ):
        assert not validator.is_valid(value), f"{name} accepted {value!r}"
