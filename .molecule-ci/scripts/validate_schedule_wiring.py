#!/usr/bin/env python3
"""Fail a template repo whose schedules can never fire.

WHY THIS EXISTS
---------------
A schedule declaration is the single easiest thing in this system to get
SILENTLY wrong. Every failure mode below produces a template that parses
cleanly, validates against the JSON schema, imports without error, and then
simply never runs anything. On 2026-07-27 an audit found **35 of 37** declared
schedules across the fleet in exactly that state — no error anywhere, because
"declared but unreachable" is indistinguishable from "declared" to every
validator that only checks shape.

The existing schema gate checks the SHAPE of a schedule (name, cron_expr,
types). Shape was never the problem. This checks the WIRING: whether anything
on the box will ever read the block.

The M3 flag day moves the block from a workspace node's own `schedules:` into
`plugins[].config.schedules` on the scheduler plugin, with NO alias window —
so during and after that move the two dead-ends below are live hazards, and a
gate that only understands one location would go green through the migration.

WHAT IT REFUSES
---------------
E1  schedules declared, no scheduler plugin installed
    The runtime materializes the grid from the kind:trigger scheduler plugin.
    No scheduler in the node's effective `plugins:` => nothing fires. This is
    the 35-of-37 failure.

E2  plugins[].config.schedules on a plugin that is NOT the scheduler
    Per-install config is delivered to `<install-name>.json` and read by that
    plugin alone. Hanging schedules off an unrelated plugin delivers a file
    nobody opens. A missing settings file is a clean no-op by design, so this
    fails silently in the most literal sense.

E3  BOTH node-level `schedules:` and `plugins[].config.schedules`
    Ambiguous ownership during the flag day. Refuse rather than guess which
    one wins — guessing is how you lose half a grid.

E4  cron_expr that is not 5 fields
    6-field (seconds-first) cron is the common paste from other schedulers; it
    parses as a string and never matches.

E5  neither `prompt` nor `prompt_file`
    A schedule with no body ticks forever and does nothing.

Exit 0 clean, 1 on any refusal. Every message names the file, the node, the
schedule, and the concrete fix — a gate that says "invalid" without saying
where is a gate people learn to skip.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it
    print("::error::PyYAML is required (scripts/requirements.txt)", file=sys.stderr)
    raise SystemExit(2)

# Source strings that identify the scheduler. Matched as a SUBSTRING against
# each plugin source so a pin (`...#v0.2.0`) or a full gitea:// URL still
# matches. Kept as a tuple rather than one string because the install
# directory (repo name) and the manifest name differ, and templates in the
# wild reference it both ways.
SCHEDULER_MARKERS: tuple[str, ...] = (
    "molecule-ai-plugin-scheduler",
    "molecule-scheduler",
)


class Finding:
    __slots__ = ("code", "path", "node", "detail")

    def __init__(self, code: str, path: str, node: str, detail: str) -> None:
        self.code, self.path, self.node, self.detail = code, path, node, detail

    def __str__(self) -> str:
        where = f"{self.path}" + (f" :: node {self.node!r}" if self.node else "")
        return f"::error::[{self.code}] {where} — {self.detail}"


def plugin_source(entry: Any) -> str:
    """Source string for a `plugins:` entry.

    Both grammars are live: a bare string, and the {source, config} object a
    template writes when it sets per-install config (sdk#176). An entry that
    is neither contributes no source rather than raising — a malformed entry
    is the schema's problem, not this gate's.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        src = entry.get("source")
        if isinstance(src, str):
            return src
    return ""


def is_scheduler(source: str) -> bool:
    s = source.strip().lower()
    # Strip an opt-out prefix so "!molecule-ai-plugin-scheduler" is NOT counted
    # as installing the scheduler — that entry declines it.
    if s.startswith(("!", "-")):
        return False
    return any(m in s for m in SCHEDULER_MARKERS)


def declares_scheduler(plugins: Any) -> bool:
    if not isinstance(plugins, list):
        return False
    return any(is_scheduler(plugin_source(p)) for p in plugins)


def check_schedule_entries(
    schedules: Any, path: str, node: str, origin: str, out: list[Finding]
) -> None:
    if not isinstance(schedules, list):
        return
    for sched in schedules:
        if not isinstance(sched, dict):
            continue
        name = str(sched.get("name", "<unnamed>"))
        cron = sched.get("cron_expr")
        if isinstance(cron, str) and cron.strip():
            fields = cron.split()
            if len(fields) != 5:
                out.append(
                    Finding(
                        "E4",
                        path,
                        node,
                        f"schedule {name!r} ({origin}) has a {len(fields)}-field cron_expr "
                        f"{cron!r}; this system takes 5-field cron (min hour dom mon dow). "
                        f"A 6-field seconds-first expression parses fine and never matches.",
                    )
                )
        if not (sched.get("prompt") or sched.get("prompt_file")):
            out.append(
                Finding(
                    "E5",
                    path,
                    node,
                    f"schedule {name!r} ({origin}) declares neither `prompt` nor "
                    f"`prompt_file`; it would tick forever and run nothing.",
                )
            )


def effective_plugins(node_obj: dict, inherited: list) -> list:
    """A node's plugin list AFTER org-level `defaults.plugins` inheritance.

    Reading the node's own `plugins:` alone is wrong and would produce FALSE
    POSITIVES: core merges org defaults with the node's list, so a node may
    legitimately carry `plugins: []` (or omit the key) and still end up with
    the scheduler installed. A wiring gate that cries wolf on a correctly
    inheriting template is worse than no gate — it gets muted, and then it is
    not there for the real dead schedules.

    Precedence mirrors core's mergePlugins: defaults first, node second, so a
    node can DECLINE an inherited plugin with a leading "!"/"-" and that
    opt-out is respected here too (is_scheduler returns False for those).
    """
    own = node_obj.get("plugins")
    own_list = own if isinstance(own, list) else []

    # Opt-outs REMOVE, they do not merely fail to add. Concatenating the two
    # lists would leave the inherited entry in place and silently bless a node
    # that has explicitly declined the scheduler — the gate would then pass the
    # one configuration it most needs to catch. (Caught by
    # test_node_opt_out_defeats_an_inherited_scheduler; the docstring claimed
    # this behaviour before the code implemented it.)
    declined: list[str] = []
    for entry in own_list:
        src = plugin_source(entry).strip()
        if src.startswith(("!", "-")):
            declined.append(src.lstrip("!-").strip().lower())

    def is_declined(entry: Any) -> bool:
        src = plugin_source(entry).strip().lower()
        if not src:
            return False
        return any(d and (d in src or src in d) for d in declined)

    kept_inherited = [e for e in inherited if not is_declined(e)]
    return kept_inherited + own_list


def check_node(
    node_obj: dict, path: str, node_name: str, out: list[Finding],
    inherited_plugins: list | None = None,
) -> None:
    """Wiring checks for one workspace node (or a whole workspace template)."""
    legacy = node_obj.get("schedules")
    plugins = effective_plugins(node_obj, inherited_plugins or [])

    plugin_scheds: list[tuple[str, Any]] = []
    if isinstance(plugins, list):
        for p in plugins:
            if not isinstance(p, dict):
                continue
            cfg = p.get("config")
            if isinstance(cfg, dict) and "schedules" in cfg:
                plugin_scheds.append((plugin_source(p), cfg["schedules"]))

    has_legacy = isinstance(legacy, list) and len(legacy) > 0
    has_plugin_scheds = len(plugin_scheds) > 0

    if has_legacy and has_plugin_scheds:
        out.append(
            Finding(
                "E3",
                path,
                node_name,
                "declares BOTH a node-level `schedules:` block and "
                "`plugins[].config.schedules`. Ownership is ambiguous — the M3 move "
                "has no alias window, so pick ONE location.",
            )
        )

    # E1 applies to the NEW shape ONLY. This is the single most important
    # distinction in this file, and the first cut got it backwards.
    #
    # For LEGACY node-level `schedules:`, core auto-attaches the daemon: when
    # renderTemplateSchedulesYAML renders any entry, org_import.go calls
    # ensureSchedulerPluginDeclared (and schedules.go Create does the same on
    # the API path). So a legacy template that never names the scheduler is
    # CORRECT, and flagging it would have failed all three real fleet repos —
    # 12 findings, every one a false positive. Verified in
    # molecule-core/workspace-server/internal/handlers/org_import.go:569.
    #
    # For `plugins[].config.schedules` there is NO such auto-attach, and M3
    # DELETES renderTemplateSchedulesYAML — which is precisely the trigger the
    # legacy auto-declare hangs off. So the moment a repo moves to the new
    # shape, the safety net disappears and the scheduler must be declared
    # explicitly or the whole grid goes silent. That is the flag-day landmine
    # this gate exists to catch, and it is only reachable in the new shape.
    if has_plugin_scheds and not declares_scheduler(plugins):
        out.append(
            Finding(
                "E1",
                path,
                node_name,
                "uses `plugins[].config.schedules` but installs NO scheduler plugin. "
                "Unlike the legacy node-level `schedules:` block — which core "
                "auto-attaches the daemon for via ensureSchedulerPluginDeclared — the "
                "new shape has no auto-attach, and M3 deletes the render path that "
                "triggers it. Declare the scheduler in `plugins:` or nothing fires.",
            )
        )

    for src, scheds in plugin_scheds:
        if not is_scheduler(src):
            out.append(
                Finding(
                    "E2",
                    path,
                    node_name,
                    f"plugin {src!r} carries `config.schedules`, but per-install config "
                    f"is delivered only to THAT plugin's settings file. A non-scheduler "
                    f"plugin never reads it — and a missing settings key is a clean "
                    f"no-op, so this fails silently.",
                )
            )
        check_schedule_entries(scheds, path, node_name, f"via plugin {src}", out)

    if has_legacy:
        check_schedule_entries(legacy, path, node_name, "node-level", out)


def org_default_plugins(doc: Any) -> list:
    """`defaults.plugins` from an org document, else []."""
    if not isinstance(doc, dict):
        return []
    defaults = doc.get("defaults")
    if isinstance(defaults, dict):
        p = defaults.get("plugins")
        if isinstance(p, list):
            return p
    return []


def check_document(doc: Any, path: str, out: list[Finding],
                   inherited_plugins: list | None = None) -> None:
    if not isinstance(doc, dict):
        return
    # A node inherits BOTH the repo-wide org defaults and any defaults declared
    # in its own document.
    inherited = list(inherited_plugins or []) + org_default_plugins(doc)
    # org template: workspaces live under teams[].workspaces[] or workspaces[]
    nodes: list[tuple[str, dict]] = []

    def collect(container: Any) -> None:
        """Walk the node tree, INCLUDING `children:`.

        Org templates nest. reno-stars declares exactly ONE top-level
        workspace ("Business Intelligence") and hangs the five agents that
        actually carry schedules off its `children:`. A flat collector sees
        the root, finds no schedules on it, and reports the whole repo clean —
        a FALSE NEGATIVE on the one production template whose schedules matter
        most. Found by running this gate against the real fleet rather than
        against fixtures; every test here used a flat shape and all 20 passed
        while the gate was blind to reno-stars entirely.
        """
        if not isinstance(container, list):
            return
        for w in container:
            if not isinstance(w, dict):
                continue
            nodes.append((str(w.get("name", "<unnamed>")), w))
            collect(w.get("children"))

    collect(doc.get("workspaces"))
    teams = doc.get("teams")
    if isinstance(teams, list):
        for t in teams:
            if isinstance(t, dict):
                collect(t.get("workspaces"))

    if nodes:
        for name, node in nodes:
            check_node(node, path, name, out, inherited)
    else:
        # workspace template: the document IS the node
        check_node(doc, path, "", out, inherited)


def scan(paths: list[Path]) -> list[Finding]:
    out: list[Finding] = []
    # PASS 1 — collect repo-wide org defaults. In an org template the nodes
    # frequently live in SEPARATE workspace.yaml files while the scheduler is
    # installed once via org.yaml's `defaults.plugins`. Checking each file in
    # isolation would report every one of them as unwired: a false-positive
    # storm on a correctly composed repo.
    repo_defaults: list = []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue
        for doc in docs:
            repo_defaults.extend(org_default_plugins(doc))

    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            # Malformed YAML is the schema gate's job, not ours. Skipping keeps
            # this gate's failures unambiguous.
            continue
        for doc in docs:
            check_document(doc, str(p), out, repo_defaults)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", default=None,
                    help="YAML files to check (default: all *.yaml/*.yml under the repo root).")
    ap.add_argument("--root", default=".", help="Repo root to scan when no paths are given.")
    args = ap.parse_args()

    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        root = Path(args.root)
        targets = sorted(
            p for p in root.rglob("*.y*ml")
            if ".git" not in p.parts and "node_modules" not in p.parts
        )

    findings = scan(targets)
    if not findings:
        print(f"schedule wiring OK — {len(targets)} file(s) checked, no unreachable schedules")
        return 0
    for f in findings:
        print(str(f), file=sys.stderr)
    print(
        f"::error::{len(findings)} schedule-wiring problem(s). These parse and validate "
        f"but would never fire — see the codes above.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
