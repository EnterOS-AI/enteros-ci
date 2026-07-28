"""Contract for validate_schedule_wiring.

The property under test is NOT "does a schedule parse" — the JSON-schema gate
already covers shape, and shape was never what broke. It is "would this block
ever be READ by anything on the box". 35 of 37 fleet schedules were shape-valid
and unreachable simultaneously, which is exactly why a shape gate went green
through it.

Each refusal has a paired positive case, so a test failing tells you which
direction broke.
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "validate_schedule_wiring", _HERE / "validate_schedule_wiring.py"
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

scan = _mod.scan
is_scheduler = _mod.is_scheduler
declares_scheduler = _mod.declares_scheduler
plugin_source = _mod.plugin_source

SCHED = "gitea://molecule-ai/molecule-ai-plugin-scheduler#v0.2.0"


def write(tmp_path: Path, body: str, name: str = "org.yaml") -> list[Path]:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return [p]


def codes(findings) -> list[str]:
    return [f.code for f in findings]


# --- E1: schedules with no scheduler installed (the 35-of-37 failure) --------

def test_E1_node_schedules_without_scheduler_plugin_is_refused(tmp_path):
    f = scan(write(tmp_path, f"""
        workspaces:
          - name: Coordinator
            plugins:
              - gitea://molecule-ai/molecule-ai-plugin-seo
            schedules:
              - name: Heartbeat
                cron_expr: "*/30 * * * *"
                prompt: do the thing
    """))
    assert "E1" in codes(f)


def test_E1_clears_when_the_scheduler_is_installed(tmp_path):
    f = scan(write(tmp_path, f"""
        workspaces:
          - name: Coordinator
            plugins:
              - {SCHED}
            schedules:
              - name: Heartbeat
                cron_expr: "*/30 * * * *"
                prompt: do the thing
    """))
    assert "E1" not in codes(f)


def test_E1_an_opted_out_scheduler_does_not_count_as_installed(tmp_path):
    """`!plugin` DECLINES it. Counting the opt-out as an install would bless
    exactly the config that cannot fire."""
    f = scan(write(tmp_path, """
        workspaces:
          - name: Coordinator
            plugins:
              - "!molecule-ai-plugin-scheduler"
            schedules:
              - name: Heartbeat
                cron_expr: "*/30 * * * *"
                prompt: x
    """))
    assert "E1" in codes(f)


# --- E2: schedules hung off a non-scheduler plugin ---------------------------

def test_E2_config_schedules_on_unrelated_plugin_is_refused(tmp_path):
    f = scan(write(tmp_path, f"""
        workspaces:
          - name: Marketing
            plugins:
              - {SCHED}
              - source: gitea://molecule-ai/molecule-ai-plugin-seo
                config:
                  schedules:
                    - name: SEO Builder
                      cron_expr: "17 6 * * *"
                      prompt: run seo
    """))
    assert "E2" in codes(f)


def test_E2_config_schedules_on_the_scheduler_is_fine(tmp_path):
    f = scan(write(tmp_path, f"""
        workspaces:
          - name: Marketing
            plugins:
              - source: {SCHED}
                config:
                  schedules:
                    - name: SEO Builder
                      cron_expr: "17 6 * * *"
                      prompt: run seo
    """))
    assert codes(f) == []


# --- E3: both locations at once (the flag-day hazard) ------------------------

def test_E3_both_locations_is_refused(tmp_path):
    f = scan(write(tmp_path, f"""
        workspaces:
          - name: Coordinator
            plugins:
              - source: {SCHED}
                config:
                  schedules:
                    - name: New
                      cron_expr: "0 * * * *"
                      prompt: new
            schedules:
              - name: Old
                cron_expr: "0 * * * *"
                prompt: old
    """))
    assert "E3" in codes(f)


# --- E4: cron arity ----------------------------------------------------------

def test_E4_six_field_cron_is_refused(tmp_path):
    f = scan(write(tmp_path, f"""
        workspaces:
          - name: C
            plugins: [{SCHED}]
            schedules:
              - name: Secondsy
                cron_expr: "0 */30 * * * *"
                prompt: x
    """))
    assert "E4" in codes(f)


def test_E4_five_field_cron_passes(tmp_path):
    f = scan(write(tmp_path, f"""
        workspaces:
          - name: C
            plugins: [{SCHED}]
            schedules:
              - name: Fine
                cron_expr: "*/30 * * * *"
                prompt: x
    """))
    assert "E4" not in codes(f)


# --- E5: no body -------------------------------------------------------------

def test_E5_schedule_without_prompt_or_prompt_file_is_refused(tmp_path):
    f = scan(write(tmp_path, f"""
        workspaces:
          - name: C
            plugins: [{SCHED}]
            schedules:
              - name: Empty
                cron_expr: "0 * * * *"
    """))
    assert "E5" in codes(f)


def test_E5_prompt_file_satisfies_the_body_requirement(tmp_path):
    f = scan(write(tmp_path, f"""
        workspaces:
          - name: C
            plugins: [{SCHED}]
            schedules:
              - name: Filed
                cron_expr: "0 * * * *"
                prompt_file: /configs/skills/x.md
    """))
    assert "E5" not in codes(f)


# --- inertness: must not fire on templates that declare no schedules ---------

def test_a_template_with_no_schedules_is_silent(tmp_path):
    f = scan(write(tmp_path, """
        workspaces:
          - name: Plain
            plugins:
              - gitea://molecule-ai/molecule-ai-plugin-seo
    """))
    assert f == []


def test_malformed_yaml_is_left_to_the_schema_gate(tmp_path):
    f = scan(write(tmp_path, "workspaces: [oops\n  - broken"))
    assert f == []


# --- shapes that must be understood -----------------------------------------

def test_teams_nested_workspaces_are_scanned(tmp_path):
    """molecule-dev keeps nodes under teams[].workspaces[] — missing this shape
    would silently skip 9 of the fleet's files."""
    f = scan(write(tmp_path, """
        teams:
          - name: marketing
            workspaces:
              - name: SEO
                plugins: []
                schedules:
                  - name: H
                    cron_expr: "0 * * * *"
                    prompt: x
    """))
    assert "E1" in codes(f)


def test_workspace_template_document_is_treated_as_the_node(tmp_path):
    f = scan(write(tmp_path, """
        name: seo-agent
        plugins: []
        schedules:
          - name: H
            cron_expr: "0 * * * *"
            prompt: x
    """, name="workspace.yaml"))
    assert "E1" in codes(f)


def test_object_form_plugin_source_is_read(tmp_path):
    assert plugin_source({"source": SCHED, "config": {}}) == SCHED
    assert plugin_source(SCHED) == SCHED
    assert plugin_source(123) == ""


def test_scheduler_matched_by_either_identity(tmp_path):
    assert is_scheduler("gitea://x/molecule-ai-plugin-scheduler#v1")
    assert is_scheduler("molecule-scheduler")
    assert not is_scheduler("molecule-ai-plugin-seo")
    assert declares_scheduler([{"source": SCHED}])
    assert not declares_scheduler([{"source": "other"}])
    assert not declares_scheduler("not-a-list")


# --- inheritance: the false-positive guard ----------------------------------
#
# The first cut of this gate read only a node's OWN `plugins:` and reported
# every inheriting node as unwired. On a correctly composed org template that
# is a false-positive storm, and a noisy gate gets muted — at which point it is
# not there for the genuinely dead schedules it exists to catch.

def test_org_defaults_plugins_satisfy_the_scheduler_requirement(tmp_path):
    f = scan(write(tmp_path, f"""
        defaults:
          plugins:
            - {SCHED}
        workspaces:
          - name: Inheritor
            plugins: []
            schedules:
              - name: H
                cron_expr: "0 * * * *"
                prompt: x
    """))
    assert codes(f) == [], "org defaults must count as installing the scheduler"


def test_removing_the_inherited_scheduler_reinstates_E1(tmp_path):
    """Negative control for the check above — without it, the suppression
    could be unconditional and prove nothing."""
    f = scan(write(tmp_path, """
        defaults:
          plugins:
            - gitea://molecule-ai/molecule-ai-plugin-seo
        workspaces:
          - name: Inheritor
            plugins: []
            schedules:
              - name: H
                cron_expr: "0 * * * *"
                prompt: x
    """))
    assert "E1" in codes(f)


def test_defaults_are_collected_repo_wide_across_files(tmp_path):
    """molecule-dev keeps the scheduler in org.yaml while schedules live in
    separate workspace.yaml files. Per-file isolation would flag all of them."""
    (tmp_path / "org.yaml").write_text(textwrap.dedent(f"""
        defaults:
          plugins:
            - {SCHED}
    """).lstrip(), encoding="utf-8")
    (tmp_path / "workspace.yaml").write_text(textwrap.dedent("""
        name: Community Manager
        plugins: []
        schedules:
          - name: Hourly sweep
            cron_expr: "0 * * * *"
            prompt_file: schedules/x.md
    """).lstrip(), encoding="utf-8")
    f = scan(sorted(tmp_path.glob("*.yaml")))
    assert codes(f) == [], "repo-wide defaults must reach nodes in sibling files"


def test_node_opt_out_defeats_an_inherited_scheduler(tmp_path):
    f = scan(write(tmp_path, f"""
        defaults:
          plugins:
            - {SCHED}
        workspaces:
          - name: Decliner
            plugins:
              - "!molecule-ai-plugin-scheduler"
            schedules:
              - name: H
                cron_expr: "0 * * * *"
                prompt: x
    """))
    assert "E1" in codes(f), "a node that declines the scheduler has no scheduler"
