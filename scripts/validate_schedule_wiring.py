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
E0  A target file that this gate could not read or parse, or an explicit
    expected file/schedule-block count that was not reached. Molecule
    composition tags (``!include``, ``!external``, and other local ``!`` tags)
    are loaded safely as their underlying scalar/list/mapping so they cannot
    make the whole document disappear from this gate. Duplicate or non-string
    mapping keys, symlinks/path escapes, oversized inputs, and resource-limit
    violations also fail closed without copying source lines into CI logs.

(There is no E1 — see the long note in check_node. The scheduler is declared
on every workspace at provision, so "template does not name the scheduler" has
no reachable true positive and was removed rather than left as noise.)

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
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, NamedTuple

try:
    import yaml
    from yaml.events import AliasEvent
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

# Per-file and aggregate resource limits apply before, during, and after parsing.
# They are intentionally much larger than the current fleet (the largest file is
# well below 1 MiB and each template has tens, not thousands, of nodes), while
# still preventing a wide repository, tiny alias graph, or defaults-by-workspace
# cross product from consuming an entire CI runner.
MAX_YAML_FILE_BYTES = 4 * 1024 * 1024
MAX_YAML_DOCUMENTS = 128
MAX_YAML_NODES = 100_000
MAX_YAML_DEPTH = 100
MAX_YAML_ALIASES = 256
MAX_WORKSPACE_NODES = 10_000
MAX_WORKSPACE_DEPTH = 64
MAX_TARGET_FILES = 2_048
MAX_DISCOVERY_ENTRIES = 100_000
MAX_TOTAL_YAML_BYTES = 32 * 1024 * 1024
MAX_TOTAL_YAML_DOCUMENTS = 2_048
MAX_TOTAL_YAML_NODES = 500_000
MAX_TOTAL_WORKSPACE_NODES = 50_000
MAX_PLUGIN_COMPOSITION_WORK = 1_000_000

# Tool checkouts live inside consumer worktrees but are not consumer input.
# Counting their YAML makes exact anti-vacuity assertions depend on whichever
# files happen to exist in the validator repo itself.
DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".molecule-ci-canonical",
        ".molecule-ci-ssot",
    }
)


class YamlResourceLimitError(yaml.YAMLError):
    """A bounded-parser refusal whose message is safe for public CI logs."""

    def __init__(self, safe_reason: str, mark: Any | None = None) -> None:
        super().__init__(safe_reason)
        self.safe_reason = safe_reason
        self.problem_mark = mark


class DuplicateKeyError(yaml.constructor.ConstructorError):
    """Duplicate mapping key without retaining the potentially secret key."""

    safe_reason = "target contains a duplicate mapping key"


class NonStringMappingKeyError(yaml.constructor.ConstructorError):
    """A mapping key the structural validator cannot safely interpret."""

    safe_reason = "target contains a non-string mapping key"


class ScanInputError(Exception):
    """A safe, non-disclosing refusal at the file/traversal boundary."""

    def __init__(self, safe_reason: str) -> None:
        super().__init__(safe_reason)
        self.safe_reason = safe_reason


class ScanBudget:
    """Aggregate resource accounting shared by every file in one scan."""

    __slots__ = (
        "yaml_bytes",
        "yaml_documents",
        "yaml_nodes",
        "workspace_nodes",
        "plugin_composition_work",
        "exhausted",
    )

    def __init__(self) -> None:
        self.yaml_bytes = 0
        self.yaml_documents = 0
        self.yaml_nodes = 0
        self.workspace_nodes = 0
        self.plugin_composition_work = 0
        self.exhausted = False

    def consume_yaml_bytes(self, amount: int) -> None:
        if amount > MAX_TOTAL_YAML_BYTES - self.yaml_bytes:
            self.exhausted = True
            raise ScanInputError("scan exceeds the aggregate YAML byte limit")
        self.yaml_bytes += amount

    def consume_yaml_document(self) -> None:
        if self.yaml_documents >= MAX_TOTAL_YAML_DOCUMENTS:
            self.exhausted = True
            raise YamlResourceLimitError(
                "scan exceeds the aggregate YAML document limit"
            )
        self.yaml_documents += 1

    def consume_yaml_node(self, mark: Any | None) -> None:
        if self.yaml_nodes >= MAX_TOTAL_YAML_NODES:
            self.exhausted = True
            raise YamlResourceLimitError(
                "scan exceeds the aggregate YAML node limit",
                mark,
            )
        self.yaml_nodes += 1

    def consume_workspace_node(self) -> None:
        if self.workspace_nodes >= MAX_TOTAL_WORKSPACE_NODES:
            self.exhausted = True
            raise ScanInputError("scan exceeds the aggregate workspace node limit")
        self.workspace_nodes += 1

    def consume_plugin_composition_work(self, amount: int) -> None:
        if amount > MAX_PLUGIN_COMPOSITION_WORK - self.plugin_composition_work:
            self.exhausted = True
            raise ScanInputError("scan exceeds the plugin composition work limit")
        self.plugin_composition_work += amount


class PermissiveLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves Molecule composition-tag contents.

    Org templates legitimately contain local tags such as ``!include`` and
    ``!external``. ``yaml.safe_load_all`` rejects the whole document when it
    sees one, which previously made this gate silently skip otherwise visible
    schedule declarations. The tag itself has no wiring semantics here, so
    construct its underlying scalar/list/mapping using SafeLoader primitives.
    """

    # PyYAML defaults to YAML 1.1, where the standard Actions key `on` becomes
    # boolean True. Molecule templates and Gitea Actions use YAML 1.2 semantics:
    # only true/false are implicit booleans. Clone the table before narrowing it
    # so the process-wide SafeLoader is not mutated.
    yaml_implicit_resolvers = {
        first: [
            (tag, expression)
            for tag, expression in resolvers
            if tag != "tag:yaml.org,2002:bool"
        ]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def __init__(self, stream: Any, *, budget: ScanBudget | None = None) -> None:
        super().__init__(stream)
        self._molecule_budget = budget
        self._molecule_aliases = 0
        self._molecule_nodes = 0
        self._molecule_depth = 0

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        event = self.peek_event()
        if self._molecule_budget is not None:
            self._molecule_budget.consume_yaml_node(event.start_mark)
        self._molecule_nodes += 1
        if self._molecule_nodes > MAX_YAML_NODES:
            raise YamlResourceLimitError(
                "target exceeds the YAML node limit",
                event.start_mark,
            )
        if self.check_event(AliasEvent):
            self._molecule_aliases += 1
            if self._molecule_aliases > MAX_YAML_ALIASES:
                raise YamlResourceLimitError(
                    "target exceeds the YAML alias limit",
                    event.start_mark,
                )

        self._molecule_depth += 1
        try:
            if self._molecule_depth > MAX_YAML_DEPTH:
                raise YamlResourceLimitError(
                    "target exceeds the YAML depth limit",
                    event.start_mark,
                )
            return super().compose_node(parent, index)
        finally:
            self._molecule_depth -= 1

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            return super().construct_mapping(node, deep=deep)

        # SafeLoader normally accepts duplicate keys and silently keeps the
        # last value. A later benign `plugins:`/`schedules:` key can therefore
        # hide a broken earlier block from both findings and exact counters.
        # Inspect the literal mapping before SafeLoader expands `<<` merges.
        # An explicit key overriding an inherited merge value is standard YAML,
        # whereas two literal keys in this node are ambiguous and rejected.
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                key: Any = (key_node.tag, "<<")
            else:
                # Compare the value SafeLoader will actually place in the
                # Python mapping. A custom tag is deliberately erased by this
                # loader, so it must not disguise a duplicate plain key.
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise NonStringMappingKeyError(
                        "while constructing a mapping",
                        node.start_mark,
                        "non-string mapping key",
                        key_node.start_mark,
                    )
            try:
                if key in seen:
                    raise DuplicateKeyError(
                        "while constructing a mapping",
                        node.start_mark,
                        "duplicate mapping key",
                        key_node.start_mark,
                    )
                seen.add(key)
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from exc

        return super().construct_mapping(node, deep=deep)


PermissiveLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _construct_custom_tag(
    loader: PermissiveLoader,
    _suffix: str,
    node: yaml.Node,
) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


PermissiveLoader.add_multi_constructor("!", _construct_custom_tag)


class ScanStats(NamedTuple):
    target_files: int
    parsed_files: int
    documents: int
    schedule_blocks: int
    schedule_entries: int


class Finding:
    __slots__ = ("code", "path", "node", "detail")

    def __init__(self, code: str, path: str, node: str, detail: str) -> None:
        self.code, self.path, self.node, self.detail = code, path, node, detail

    def __str__(self) -> str:
        path = _sanitize_log_text(self.path)
        node = _sanitize_log_text(self.node)
        detail = _sanitize_log_text(self.detail)
        where = path + (f" :: node {node!r}" if node else "")
        return f"::error::[{self.code}] {where} — {detail}"


def _sanitize_log_text(value: object) -> str:
    """Escape controls so one finding always occupies exactly one log line."""

    safe: list[str] = []
    for character in str(value):
        if character.isprintable():
            safe.append(character)
            continue
        codepoint = ord(character)
        if codepoint <= 0xFF:
            safe.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            safe.append(f"\\u{codepoint:04x}")
        else:
            safe.append(f"\\U{codepoint:08x}")
    return "".join(safe)


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
) -> int:
    if not isinstance(schedules, list):
        return 0
    seen_entries: set[int] = set()
    for sched in schedules:
        if not isinstance(sched, dict):
            continue
        object_id = id(sched)
        if object_id in seen_entries:
            raise ScanInputError(
                "target contains a cyclic or repeated YAML alias in schedules"
            )
        seen_entries.add(object_id)
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
    return len(schedules)


def effective_plugins(
    node_obj: dict,
    inherited: list,
    budget: ScanBudget | None = None,
) -> list:
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
    if budget is not None:
        budget.consume_plugin_composition_work(len(inherited) + len(own_list))

    declined: set[str] = set()
    for entry in own_list:
        src = plugin_source(entry)
        if src.startswith(("!", "-")):
            target = src.lstrip("!-")
            if target:
                declined.add(target)

    # Core's mergePlugins uses exact source-string identity for opt-outs. A set
    # keeps this O(defaults + own) even when a workspace declines many plugins.
    kept_inherited = [
        entry for entry in inherited if plugin_source(entry) not in declined
    ]
    return kept_inherited + own_list


def check_node(
    node_obj: dict,
    path: str,
    node_name: str,
    out: list[Finding],
    inherited_plugins: list | None = None,
    budget: ScanBudget | None = None,
) -> tuple[int, int]:
    """Wiring checks for one workspace node (or a whole workspace template)."""
    legacy = node_obj.get("schedules")
    plugins = effective_plugins(node_obj, inherited_plugins or [], budget)

    plugin_scheds: list[tuple[str, Any]] = []
    seen_plugin_objects: set[int] = set()
    if isinstance(plugins, list):
        for p in plugins:
            if not isinstance(p, dict):
                continue
            object_id = id(p)
            if object_id in seen_plugin_objects:
                raise ScanInputError(
                    "target contains a cyclic or repeated YAML alias in plugins"
                )
            seen_plugin_objects.add(object_id)
            cfg = p.get("config")
            if isinstance(cfg, dict) and "schedules" in cfg:
                plugin_scheds.append((plugin_source(p), cfg["schedules"]))

    has_legacy = isinstance(legacy, list) and len(legacy) > 0
    has_plugin_scheds = len(plugin_scheds) > 0
    schedule_blocks = len(plugin_scheds) + (1 if "schedules" in node_obj else 0)
    schedule_entries = 0

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

    # THERE IS NO E1. Deliberately, after chasing it to the bottom.
    #
    # A template never has to declare the scheduler, in EITHER shape:
    # workspace_provision_shared.go declares it on EVERY workspace at provision
    # ("the SCHEDULER is a BASE per-workspace ability"), behind a DEFAULT-ON
    # kill switch (declareSchedulerPluginEnabled: true unless explicitly set to
    # 0/false/no). Additional on-demand declares exist in org_import.go,
    # schedules.go Create and schedules_inheritance.go.
    #
    # Two earlier cuts of this file were wrong about that, in opposite
    # directions, and both LOOKED right against fixtures:
    #   * fire on both shapes  -> 12 findings on the real fleet, all false
    #   * fire on the new shape only, justified by "M3 deletes the auto-attach
    #     trigger" -> also false: the provision-time declare is not the
    #     render-time one, and M3 does not touch it.
    #
    # A check with no reachable true positive is not a safety net, it is future
    # noise that trains people to skip the gate. E2-E5 below are the checks
    # that survive scrutiny.

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
        schedule_entries += check_schedule_entries(
            scheds,
            path,
            node_name,
            f"via plugin {src}",
            out,
        )

    if has_legacy:
        schedule_entries += check_schedule_entries(
            legacy,
            path,
            node_name,
            "node-level",
            out,
        )
    return schedule_blocks, schedule_entries


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


def check_document(
    doc: Any,
    path: str,
    out: list[Finding],
    inherited_plugins: list | None = None,
    *,
    defaults_in_inherited: bool = False,
    budget: ScanBudget | None = None,
) -> tuple[int, int]:
    if not isinstance(doc, dict):
        return 0, 0
    # A node inherits BOTH the repo-wide org defaults and any defaults declared
    # in its own document. scan_with_stats passes the complete repo-defaults
    # collection, which already contains this document's defaults; direct
    # callers retain the historical behavior of adding document defaults here.
    inherited = list(inherited_plugins or [])
    if not defaults_in_inherited:
        inherited.extend(org_default_plugins(doc))
    # org template: workspaces live under teams[].workspaces[] or workspaces[]
    nodes: list[tuple[str, dict]] = []
    seen_containers: set[int] = set()
    seen_nodes: set[int] = set()
    node_visits = 0

    def collect(container: Any, depth: int = 1) -> None:
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
        nonlocal node_visits
        if not isinstance(container, list):
            return
        if depth > MAX_WORKSPACE_DEPTH:
            raise ScanInputError("target exceeds the workspace depth limit")
        container_id = id(container)
        if container_id in seen_containers:
            raise ScanInputError(
                "target contains a cyclic or repeated YAML alias in the workspace tree"
            )
        seen_containers.add(container_id)
        for w in container:
            if not isinstance(w, dict):
                continue
            node_id = id(w)
            if node_id in seen_nodes:
                raise ScanInputError(
                    "target contains a cyclic or repeated YAML alias in the workspace tree"
                )
            seen_nodes.add(node_id)
            node_visits += 1
            if node_visits > MAX_WORKSPACE_NODES:
                raise ScanInputError("target exceeds the workspace node limit")
            if budget is not None:
                budget.consume_workspace_node()
            nodes.append((str(w.get("name", "<unnamed>")), w))
            collect(w.get("children"), depth + 1)

    collect(doc.get("workspaces"))
    teams = doc.get("teams")
    if isinstance(teams, list):
        for t in teams:
            if isinstance(t, dict):
                collect(t.get("workspaces"))

    if nodes:
        schedule_blocks = 0
        schedule_entries = 0
        for name, node in nodes:
            node_blocks, node_entries = check_node(
                node,
                path,
                name,
                out,
                inherited,
                budget,
            )
            schedule_blocks += node_blocks
            schedule_entries += node_entries
        return schedule_blocks, schedule_entries
    # workspace template: the document IS the node
    return check_node(doc, path, "", out, inherited, budget)


def _deduplicate_paths(paths: list[Path]) -> list[Path]:
    """Preserve first-seen order while counting each lexical file once."""
    unique: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        if key in seen:
            continue
        if len(unique) >= MAX_TARGET_FILES:
            raise ScanInputError("scan exceeds the aggregate target file limit")
        seen.add(key)
        unique.append(path)
    return unique


def _resolve_scan_root(paths: list[Path], root: Path | None) -> Path:
    candidate: Path
    if root is not None:
        candidate = Path(root)
    elif paths:
        try:
            parents = [
                os.path.dirname(os.path.abspath(os.fspath(path))) for path in paths
            ]
            candidate = Path(os.path.commonpath(parents))
        except ValueError as exc:
            raise ScanInputError("scan root could not be inferred") from exc
    else:
        candidate = Path.cwd()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScanInputError("scan root is unavailable") from exc
    if not resolved.is_dir():
        raise ScanInputError("scan root is not a directory")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_bounded_yaml(path: Path, root: Path) -> tuple[str, int]:
    """Read one regular in-root file without following the final symlink."""
    lexical = Path(os.path.abspath(os.fspath(path)))
    try:
        path_stat = lexical.lstat()
    except OSError as exc:
        raise ScanInputError("target could not be safely opened") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        raise ScanInputError(
            "target is a symbolic link; only regular files are allowed"
        )
    if not stat.S_ISREG(path_stat.st_mode):
        raise ScanInputError("target is not a regular file")

    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScanInputError("target could not be safely resolved") from exc
    if not _is_within(resolved, root):
        raise ScanInputError("target resolves outside the scan root")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ScanInputError("target could not be safely opened") from exc
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ScanInputError("target is not a regular file")
        if opened_stat.st_size > MAX_YAML_FILE_BYTES:
            raise ScanInputError(
                f"target exceeds the maximum size of {MAX_YAML_FILE_BYTES} bytes"
            )

        chunks: list[bytes] = []
        bytes_read = 0
        while bytes_read <= MAX_YAML_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_YAML_FILE_BYTES + 1 - bytes_read),
            )
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
        if bytes_read > MAX_YAML_FILE_BYTES:
            raise ScanInputError(
                f"target exceeds the maximum size of {MAX_YAML_FILE_BYTES} bytes"
            )
    except OSError as exc:
        raise ScanInputError("target could not be safely read") from exc
    finally:
        os.close(descriptor)

    try:
        return b"".join(chunks).decode("utf-8"), bytes_read
    except UnicodeDecodeError as exc:
        raise ScanInputError("target is not valid UTF-8") from exc


def _yaml_error_detail(exc: yaml.YAMLError) -> str:
    if isinstance(exc, (DuplicateKeyError, NonStringMappingKeyError)):
        detail = exc.safe_reason
    elif isinstance(exc, YamlResourceLimitError):
        detail = exc.safe_reason
    else:
        detail = "target contains invalid YAML"
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        detail += f" at line {mark.line + 1}, column {mark.column + 1}"
    return detail


def _load_documents(
    text: str,
    budget: ScanBudget | None = None,
) -> list[Any]:
    documents: list[Any] = []
    loader = PermissiveLoader(text, budget=budget)
    try:
        while loader.check_data():
            if len(documents) >= MAX_YAML_DOCUMENTS:
                raise YamlResourceLimitError("target exceeds the YAML document limit")
            if budget is not None:
                budget.consume_yaml_document()
            documents.append(loader.get_data())
    finally:
        loader.dispose()
    return documents


def scan_with_stats(
    paths: list[Path],
    *,
    root: Path | None = None,
) -> tuple[list[Finding], ScanStats]:
    out: list[Finding] = []
    try:
        targets = _deduplicate_paths(paths)
    except ScanInputError as exc:
        out.append(Finding("E0", "<scan>", "", exc.safe_reason))
        return out, ScanStats(len(paths), 0, 0, 0, 0)

    loaded: list[tuple[Path, list[Any]]] = []
    budget = ScanBudget()
    try:
        scan_root = _resolve_scan_root(targets, root)
    except ScanInputError as exc:
        out.append(Finding("E0", "<scan>", "", exc.safe_reason))
        return out, ScanStats(len(targets), 0, 0, 0, 0)

    for p in targets:
        try:
            text, byte_count = _read_bounded_yaml(p, scan_root)
            budget.consume_yaml_bytes(byte_count)
        except ScanInputError as exc:
            out.append(
                Finding(
                    "E0",
                    str(p),
                    "",
                    exc.safe_reason,
                )
            )
            if budget.exhausted:
                break
            continue
        try:
            docs = _load_documents(text, budget)
        except yaml.YAMLError as exc:
            out.append(
                Finding(
                    "E0",
                    str(p),
                    "",
                    _yaml_error_detail(exc),
                )
            )
            if budget.exhausted:
                break
            continue
        except RecursionError:
            out.append(
                Finding(
                    "E0",
                    str(p),
                    "",
                    "target exceeds the YAML processing limits",
                )
            )
            continue
        loaded.append((p, docs))

    # PASS 1 — collect repo-wide org defaults. In an org template the nodes
    # frequently live in SEPARATE workspace.yaml files while the scheduler is
    # installed once via org.yaml's `defaults.plugins`. Checking each file in
    # isolation would report every one of them as unwired: a false-positive
    # storm on a correctly composed repo.
    repo_defaults: list = []
    for _path, docs in loaded:
        for doc in docs:
            repo_defaults.extend(org_default_plugins(doc))

    schedule_blocks = 0
    schedule_entries = 0
    for p, docs in loaded:
        if budget.exhausted:
            break
        for doc in docs:
            try:
                document_blocks, document_entries = check_document(
                    doc,
                    str(p),
                    out,
                    repo_defaults,
                    defaults_in_inherited=True,
                    budget=budget,
                )
            except ScanInputError as exc:
                out.append(Finding("E0", str(p), "", exc.safe_reason))
                if budget.exhausted:
                    break
                continue
            schedule_blocks += document_blocks
            schedule_entries += document_entries
    return out, ScanStats(
        target_files=len(targets),
        parsed_files=len(loaded),
        documents=sum(len(docs) for _path, docs in loaded),
        schedule_blocks=schedule_blocks,
        schedule_entries=schedule_entries,
    )


def scan(paths: list[Path], *, root: Path | None = None) -> list[Finding]:
    findings, _stats = scan_with_stats(paths, root=root)
    return findings


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _directory_name(raw: str) -> str:
    if not raw or raw in {".", ".."} or "/" in raw or "\\" in raw:
        raise argparse.ArgumentTypeError("must be a single directory name")
    return raw


def discover_yaml_targets(root: Path, excluded_dirs: set[str]) -> list[Path]:
    """Discover YAML under root while excluding tool/generated directories."""

    targets: list[Path] = []
    discovered_entries = 0

    def refuse_walk_error(exc: OSError) -> None:
        raise ScanInputError("scan root could not be safely discovered") from exc

    for current, directories, files in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=refuse_walk_error,
    ):
        directories[:] = sorted(
            name for name in directories if name not in excluded_dirs
        )
        names = [*directories, *sorted(files)]
        for name in names:
            discovered_entries += 1
            if discovered_entries > MAX_DISCOVERY_ENTRIES:
                raise ScanInputError("scan exceeds the discovery entry limit")
            if not Path(name).match("*.y*ml"):
                continue
            if len(targets) >= MAX_TARGET_FILES:
                raise ScanInputError("scan exceeds the aggregate target file limit")
            targets.append(Path(current) / name)
    return sorted(targets)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "paths",
        nargs="*",
        default=None,
        help=(
            "Explicit YAML allowlist (default: discover *.yaml/*.yml under --root). "
            "Every target must be a regular file inside --root; duplicates collapse."
        ),
    )
    ap.add_argument(
        "--root",
        default=".",
        help="Security boundary and discovery root (default: current directory).",
    )
    ap.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        type=_directory_name,
        metavar="NAME",
        help=(
            "Additional directory name to omit during discovery (repeatable). "
            "Tool checkouts such as .molecule-ci-canonical are excluded by default."
        ),
    )
    ap.add_argument(
        "--expect-parsed-files",
        type=_positive_int,
        help="Fail unless exactly this positive number of target files are parsed.",
    )
    ap.add_argument(
        "--expect-schedule-blocks",
        type=_positive_int,
        help=(
            "Fail unless exactly this positive number of node/plugin schedule "
            "blocks are reached."
        ),
    )
    args = ap.parse_args()

    root = Path(args.root)
    discovery_error: ScanInputError | None = None
    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        excluded_dirs = set(DEFAULT_EXCLUDED_DIRS)
        excluded_dirs.update(args.exclude_dir)
        try:
            targets = discover_yaml_targets(root, excluded_dirs)
        except ScanInputError as exc:
            discovery_error = exc
            targets = []

    if discovery_error is None:
        findings, stats = scan_with_stats(targets, root=root)
    else:
        findings = [Finding("E0", "<scan>", "", discovery_error.safe_reason)]
        stats = ScanStats(0, 0, 0, 0, 0)
    if (
        args.expect_parsed_files is not None
        and stats.parsed_files != args.expect_parsed_files
    ):
        findings.append(
            Finding(
                "E0",
                "<scan>",
                "",
                f"expected {args.expect_parsed_files} parsed file(s), "
                f"reached {stats.parsed_files}",
            )
        )
    if (
        args.expect_schedule_blocks is not None
        and stats.schedule_blocks != args.expect_schedule_blocks
    ):
        findings.append(
            Finding(
                "E0",
                "<scan>",
                "",
                f"expected {args.expect_schedule_blocks} schedule block(s), "
                f"reached {stats.schedule_blocks}",
            )
        )
    if not findings:
        print(
            "schedule wiring OK — "
            f"{stats.parsed_files}/{stats.target_files} file(s) parsed, "
            f"{stats.documents} document(s), "
            f"{stats.schedule_blocks} schedule block(s), "
            f"{stats.schedule_entries} schedule entry/entries checked, "
            "no unreachable schedules"
        )
        return 0
    for f in findings:
        print(str(f), file=sys.stderr)
    print(
        f"::error::{len(findings)} schedule-wiring or scan-integrity problem(s) — "
        f"see the codes above.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
