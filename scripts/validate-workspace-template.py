#!/usr/bin/env python3
"""Validate a workspace template against the canonical Molecule contract.

Run from the template repository root. The validator checks Dockerfile,
config.yaml, requirements, adapter loading, and SDK-backed schema boundaries.
"""
import json
import os
import re
import sys
from pathlib import Path

import yaml
from requirements_contract import (
    PRIVATE_INDEX_URL,
    RETIRED_RUNTIME_PROJECT,
    RUNTIME_PROJECT,
    RequirementsContractError,
    inspect_requirements,
)

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print(
        "::error::jsonschema not installed — validate-workspace-template.py "
        "validates config.yaml against the vendored molecule-ai-sdk schema "
        "and needs `pip install jsonschema`. (CI installs it; see the "
        "validate-workspace-template workflow.)"
    )
    sys.exit(1)

ERRORS: list[str] = []
WARNINGS: list[str] = []

def err(msg: str) -> None:
    ERRORS.append(msg)

def warn(msg: str) -> None:
    WARNINGS.append(msg)


def _find_schema(name: str) -> Path | None:
    """Locate a vendored schema by walking up from this script to the repo
    root's schemas/ dir."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "schemas" / name
        if cand.is_file():
            return cand
    err(f"vendored schema not found: schemas/{name} (looked up from {here})")
    return None


_WORKSPACE_SCHEMA = None

def _workspace_schema() -> dict | None:
    """Load + cache the vendored marketplace workspace-template JSON-Schema
    (SSOT, from molecule-ai-sdk). This is the authority for the config.yaml
    field, required-key, RuntimeId, and type shape; see _check_schema_v1."""
    global _WORKSPACE_SCHEMA
    if _WORKSPACE_SCHEMA is None:
        path = _find_schema("workspace-template.schema.json")
        if path is None:
            return None
        _WORKSPACE_SCHEMA = json.loads(path.read_text())
    return _WORKSPACE_SCHEMA


# ───────────────────────────────────────────────────────────── Dockerfile

def _logical_dockerfile_instructions(content: str) -> list[str]:
    """Join Dockerfile continuation lines for command-level policy checks."""
    instructions: list[str] = []
    current = ""
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        continued = raw_line.rstrip().endswith("\\")
        piece = raw_line.rstrip()
        if continued:
            piece = piece[:-1]
        current = f"{current} {piece.strip()}".strip()
        if not continued:
            instructions.append(current)
            current = ""
    if current:
        instructions.append(current)
    return instructions


def _check_private_runtime_wheel_install(dockerfile: str) -> None:
    """Require private-only wheel acquisition before public dependency solve."""
    instructions = _logical_dockerfile_instructions(dockerfile)
    run_instructions = [
        instruction
        for instruction in instructions
        if instruction.upper().startswith("RUN ")
    ]
    downloads = [
        instruction
        for instruction in run_instructions
        if re.search(r"\bpip(?:3)?\s+download\b", instruction)
        and RUNTIME_PROJECT in instruction
    ]
    expected_arg = f"ARG MOLECULE_RUNTIME_INDEX={PRIVATE_INDEX_URL}"
    if expected_arg not in instructions:
        err(
            "Dockerfile: private-only runtime wheel acquisition must declare "
            f"`{expected_arg}`"
        )
    if len(downloads) != 1:
        err(
            "Dockerfile: private-only runtime wheel acquisition must use exactly "
            f"one `pip download` for `{RUNTIME_PROJECT}`"
        )
    else:
        download = downloads[0]
        required_tokens = (
            "--isolated",
            "--only-binary=:all:",
            "--no-deps",
            "--index-url",
            "MOLECULE_RUNTIME_INDEX",
        )
        missing = [token for token in required_tokens if token not in download]
        if missing or "--extra-index-url" in download:
            err(
                "Dockerfile: private-only runtime wheel acquisition is incomplete; "
                f"missing={missing}, extra-index={('--extra-index-url' in download)}"
            )

    download_instruction = downloads[0] if len(downloads) == 1 else ""
    has_local_solve = (
        bool(re.search(r"\bpip(?:3)?\s+install\b", download_instruction))
        and "--isolated" in download_instruction
        and ".whl" in download_instruction
        and bool(
            re.search(
                r"(?:^|\s)-r\s+(?:requirements\.txt|/tmp/template-requirements\.txt)"
                r"(?=\s|;|$)",
                download_instruction,
            )
        )
    )
    if not has_local_solve:
        err(
            "Dockerfile: install the source-pinned local runtime `.whl` in the "
            "same isolated RUN and dependency solve"
        )

    for instruction in run_instructions:
        for match in re.finditer(
            r"\bpip(?:3)?\s+install\b(?P<args>.*?)(?=(?:&&|;|\|\||$))",
            instruction,
        ):
            args = match.group("args")
            if RUNTIME_PROJECT in args and ".whl" not in args:
                err(
                    "Dockerfile: must not install the private runtime by project "
                    "name; install the source-pinned local wheel"
                )


def check_dockerfile() -> None:
    if not os.path.isfile("Dockerfile"):
        warn("no Dockerfile — skipping container drift checks (library-only template?)")
        return
    df = open("Dockerfile").read()

    if not re.search(r"^FROM python:3\.11-slim\b", df, re.MULTILINE):
        err("Dockerfile: must base on `FROM python:3.11-slim` — see contract doc")

    if not re.search(r"^ARG RUNTIME_VERSION", df, re.MULTILINE):
        err(
            "Dockerfile: missing `ARG RUNTIME_VERSION=`. "
            "This arg invalidates the pip-install cache when the cascade "
            "publishes a new wheel; without it, the cascade silently ships "
            "the previous runtime (cache trap observed 2026-04-27, 5x in a row)."
        )

    requirements_has_runtime = False
    if os.path.isfile("requirements.txt"):
        try:
            inspect_requirements(Path("requirements.txt"), root=Path.cwd())
            requirements_has_runtime = True
        except RequirementsContractError:
            pass  # check_requirements() reports the actionable parse error.
    if RUNTIME_PROJECT not in df and not requirements_has_runtime:
        err(
            "Dockerfile + requirements.txt: must install the runtime dist "
            f"`{RUNTIME_PROJECT}`"
        )
    if RETIRED_RUNTIME_PROJECT in df:
        err(
            "Dockerfile: must not install the retired runtime distribution "
            f"`{RETIRED_RUNTIME_PROJECT}`; use `{RUNTIME_PROJECT}`"
        )

    _check_private_runtime_wheel_install(df)

    if "${RUNTIME_VERSION}" not in df and "$RUNTIME_VERSION" not in df:
        err(
            "Dockerfile: must reference `${RUNTIME_VERSION}` in the private "
            "runtime-wheel download layer so releases invalidate its cache key"
        )

    if not re.search(r"useradd[^\n]*\bagent\b", df):
        err(
            "Dockerfile: must create the `agent` user "
            "(`RUN useradd -u 1000 -m -s /bin/bash agent`). "
            "Runtime drops to uid 1000; without it, claude-code refuses "
            "`--dangerously-skip-permissions` for safety."
        )

    has_direct_entrypoint = bool(
        re.search(r'(ENTRYPOINT|CMD)\s*\[?\s*"?molecule-runtime"?', df)
    )
    has_custom_entrypoint = bool(
        re.search(r'ENTRYPOINT\s*\[?\s*"?(/?[\w./-]*entrypoint\.sh|/?[\w./-]*start\.sh)', df)
    )
    if not has_direct_entrypoint and not has_custom_entrypoint:
        err(
            "Dockerfile: must end at `molecule-runtime` "
            "(`ENTRYPOINT [\"molecule-runtime\"]` or via custom "
            "entrypoint.sh / start.sh that exec's molecule-runtime)"
        )
    if has_custom_entrypoint:
        m = re.search(r'ENTRYPOINT\s*\[?\s*"?(/?[\w./-]+)', df)
        if m:
            ep_in_image = m.group(1).lstrip("/")
            ep_local = os.path.basename(ep_in_image)
            if os.path.isfile(ep_local):
                if "molecule-runtime" not in open(ep_local).read():
                    err(
                        f"Dockerfile uses ENTRYPOINT [{ep_in_image}] but "
                        f"{ep_local} does not exec `molecule-runtime`"
                    )
            else:
                warn(
                    f"Dockerfile points ENTRYPOINT at {ep_in_image} but "
                    f"{ep_local} not found in repo root — verify it's COPYed in"
                )


# ───────────────────────────────────────────────────────────── config.yaml

# NOTE (SSOT switch, RFC molecule-core#3285): the former hand-rolled
# KNOWN_RUNTIMES set and the per-key required/optional validation of config.yaml
# have been RETIRED. The open RuntimeId contract + required/type field shape
# now live in the marketplace workspace-template JSON-Schema vendored from
# molecule-ai-sdk (schemas/workspace-template.schema.json), and _check_schema_v1
# validates against it. The schema-version DISPATCH machinery below stays — which
# contract version is current / deprecated / unknown is molecule-ci migration
# policy that the JSON-Schema (a single-version shape) does not express.

# ──────────────────────────────────────────── schema versioning
#
# `template_schema_version: int` in each template's config.yaml selects
# which contract this validator enforces. Versions are FROZEN once
# shipped — never edit a SCHEMA_V* constant in place. To bump:
#
#   1. Add the v<N+1> shape to the marketplace workspace-template schema in
#      molecule-ai-sdk and re-vendor it (see schemas/PROVENANCE.md). The
#      schema — not a hand-maintained key list — is the field/required SSOT.
#   2. Add `_check_schema_v<N+1>(config)` that validates against that schema
#      (mirror `_check_schema_v1`; if contracts ships per-version schema files,
#      point it at the v<N+1> file).
#   3. Add the entry to SCHEMA_CHECKS below.
#   4. Move version N from KNOWN_SCHEMA_VERSIONS to
#      DEPRECATED_SCHEMA_VERSIONS so existing v<N> templates warn but
#      still pass — buys a deprecation window.
#   5. Ship a corresponding migration in scripts/migrate-template.py's
#      MIGRATIONS table (key = N, value = callable that produces the
#      v<N+1> dict from a v<N> dict).
#   6. Run migrate-template.py on each consumer template repo as a PR.
#   7. After all consumers migrate, drop version N from
#      DEPRECATED_SCHEMA_VERSIONS in a follow-up PR.
#
# This discipline means a schema version always has exactly one valid
# enforcement function, never "branch on minor variants" — the whole
# point of versioning is to avoid that drift.

KNOWN_SCHEMA_VERSIONS: set[int] = {1}
DEPRECATED_SCHEMA_VERSIONS: set[int] = set()

def _check_schema_v1(config: dict) -> None:
    """v1 contract — validated against the marketplace workspace-template
    JSON-Schema vendored from molecule-ai-sdk (the SSOT). This REPLACES the
    former hand-rolled required-key list + KNOWN_RUNTIMES set: the schema is now
    the authority for which keys are required, which types are allowed, and the
    open bounded/path-safe `runtime` identifier.

    `template_schema_version` is verified present + int by the dispatcher before
    we get here; the schema also requires it (integer), which is consistent — a
    present int satisfies both, so there is no duplicate/contradictory error.

    Errors are formatted into molecule-ci's own actionable messages (the schema
    drives WHAT is wrong; this function reports it in a stable voice):
      * a `required` violation  -> config.yaml: missing required key `X`
      * a malformed `runtime` -> a RuntimeId shape error. Safe custom IDs pass;
        official-runtime discovery is a separate SDK registry concern.
    Unknown top-level keys are TOLERATED by the schema (additionalProperties:true,
    forward-compat) but still surfaced as a drift WARNING, with the known-key set
    derived from the schema's own `properties` (no hand-maintained list)."""
    schema = _workspace_schema()
    if schema is None:
        return  # _find_schema already recorded the missing-schema error

    for e in sorted(Draft202012Validator(schema).iter_errors(config),
                    key=lambda e: list(e.path)):
        if e.validator == "required":
            for key in schema.get("required", []):
                if key == "template_schema_version":
                    # Already verified by the dispatcher; don't double-report.
                    continue
                if key not in config and f"'{key}'" in e.message:
                    err(f"config.yaml: missing required key `{key}`")
        else:
            loc = "/".join(str(p) for p in e.path) or "(root)"
            err(f"config.yaml schema violation at `{loc}`: {e.message}")

    # Unknown top-level keys: not a schema error (forward-compat), but a useful
    # drift signal. Known keys come from the schema's declared properties (SSOT).
    known = set(schema.get("properties", {}).keys())
    unknown = set(config.keys()) - known
    if unknown:
        warn(
            f"config.yaml: unknown top-level keys {sorted(unknown)} — "
            f"may be drift. If intentional, add them to the workspace-template "
            f"schema in molecule-ai-sdk."
        )


SCHEMA_CHECKS = {
    1: _check_schema_v1,
}


def check_config_yaml() -> None:
    if not os.path.isfile("config.yaml"):
        err("config.yaml: missing at repo root")
        return
    with open("config.yaml") as f:
        try:
            config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            err(f"config.yaml: invalid YAML — {e}")
            return
    if not isinstance(config, dict):
        err(f"config.yaml: root must be a mapping, got {type(config).__name__}")
        return

    # Schema-version dispatch. Validate the version field shape first
    # so error messages are actionable.
    sv = config.get("template_schema_version")
    if sv is None:
        err("config.yaml: missing required key `template_schema_version`")
        # Can't dispatch without a version. Don't fall through to v1
        # checks — that would mask the missing-version error.
        return
    if not isinstance(sv, int):
        err(
            f"config.yaml: template_schema_version must be int, "
            f"got {type(sv).__name__}={sv!r}"
        )
        return

    if sv in DEPRECATED_SCHEMA_VERSIONS:
        latest = max(KNOWN_SCHEMA_VERSIONS)
        warn(
            f"config.yaml: template_schema_version={sv} is deprecated; "
            f"migrate to v{latest} via "
            f"`python3 scripts/migrate-template.py --to {latest} .`. "
            f"Support for v{sv} will be removed in a future cycle."
        )
    elif sv not in KNOWN_SCHEMA_VERSIONS:
        valid = sorted(KNOWN_SCHEMA_VERSIONS | DEPRECATED_SCHEMA_VERSIONS)
        err(
            f"config.yaml: template_schema_version={sv} is unknown — "
            f"this validator understands {valid}. Either bump the "
            f"validator (add a SCHEMA_V{sv} block) or correct the version."
        )
        return

    SCHEMA_CHECKS[sv](config)


# ───────────────────────────────────────────────────────────── requirements.txt

def check_requirements() -> None:
    if not os.path.isfile("requirements.txt"):
        warn("no requirements.txt — Dockerfile must install runtime by other means")
        return
    try:
        inspect_requirements(Path("requirements.txt"), root=Path.cwd())
    except RequirementsContractError as exc:
        for message in str(exc).splitlines():
            err(message)


# ───────────────────────────────────────────────────────────── adapter.py

def check_adapter() -> None:
    """Static-text adapter checks. Fast — no imports."""
    if not os.path.isfile("adapter.py"):
        warn("no adapter.py — runtime will use its packaged default adapter")
        return
    content = open("adapter.py").read()
    # The original validator's warning ("don't import molecule_runtime") was
    # backwards — that's the canonical package name. The previous check shipped
    # for ~2 weeks producing false-positive warnings. Removed.
    if re.search(r"\bfrom molecule_ai\b|\bimport molecule_ai\b", content):
        warn(
            "adapter.py imports `molecule_ai` — that's a pre-#87 package name; "
            "use `molecule_runtime`"
        )


def check_adapter_runtime_load() -> None:
    """Strong adapter contract: import adapter.py the same way the runtime
    does at workspace boot, and assert at least one class in it inherits
    from molecule_runtime.adapters.base.BaseAdapter.

    The Docker build smoke test in validate-workspace-template.yml builds
    the image but doesn't RUN it — adapter.py is only imported at
    container startup. So a template with a syntactically-valid Dockerfile
    + a broken adapter.py (wrong base class, ImportError on a missing
    framework dep, typo) builds clean and fails on first user prompt.
    This check exercises the same class-resolution path the runtime uses,
    so a passing validator means a passing workspace boot for the
    adapter-load step.

    Skip conditions:
      - No adapter.py exists. Templates without one inherit the runtime's
        packaged default adapter (intentional, not drift).
      - molecules-workspace-runtime not importable in the validator
        environment. That's a CI-config bug — the workflow that runs
        this validator must run `install_workspace_dependencies.py`
        first. Warn loudly so the misconfiguration surfaces, but don't
        hard-fail (we'd be saying "your adapter is broken" when the
        actual cause is missing infra). The source-pinned dependency-install
        step in validate-workspace-template.yml
        normally satisfies this transitively.

    Hard-error conditions:
      - adapter.py raises any exception during import. The same
        exception would crash workspace boot.
      - No class in the module inherits from BaseAdapter. The runtime's
        adapter-discovery would silently fall through to the default
        executor, ignoring this file — exactly the kind of human-error
        mode this contract is supposed to eliminate.
    """
    if not os.path.isfile("adapter.py"):
        return  # check_adapter() already warned; don't double-warn

    try:
        from molecule_runtime.adapters.base import BaseAdapter  # noqa: PLC0415
    except ImportError:
        warn(
            "adapter.py: skipping runtime-load check — "
            "`molecules-workspace-runtime` not installed in the validator "
            "environment. The CI workflow that invokes this script must "
            "run `install_workspace_dependencies.py` first; otherwise this critical check is "
            "silently bypassed."
        )
        return

    # Load adapter.py as a module under a per-call-unique name so it
    # doesn't collide with any installed `adapter` package OR with a
    # previous invocation in the same Python process. The id() of the
    # cwd-anchored absolute path is sufficient — we just need
    # different invocations to land on different sys.modules keys so
    # one invocation's lingering references can't bleed into the
    # next's adapter discovery.
    import importlib.util  # noqa: PLC0415
    import sys             # noqa: PLC0415

    abs_path = os.path.abspath("adapter.py")
    module_name = f"_template_adapter_under_validation_{abs(hash(abs_path)):x}"
    spec = importlib.util.spec_from_file_location(module_name, "adapter.py")
    if spec is None or spec.loader is None:
        err("adapter.py: cannot construct an import spec — file may be unreadable")
        return

    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod  # required so dataclass / pydantic refs resolve

    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        err(
            f"adapter.py: failed to import — `{type(e).__name__}: {e}`. "
            f"This is the same failure mode that crashes workspace boot at "
            f"runtime; the cure is to fix the adapter, not skip this check. "
            f"If the import fails because a transitive dep isn't installed in "
            f"this CI env, add it to the template's requirements.txt — that's "
            f"what the workspace container does, and the validator job "
            f"installs requirements.txt before running this check."
        )
        sys.modules.pop(module_name, None)
        return

    # Class discovery: only count CONCRETE classes DEFINED in
    # adapter.py, not re-exported imports and not abstract
    # intermediates. Three filter axes:
    #
    #   1. `__module__ == module_name` — defined HERE, not imported
    #      from molecule_runtime or a third-party framework.
    #   2. `obj is not BaseAdapter` — BaseAdapter itself doesn't count.
    #   3. `not inspect.isabstract(obj)` — abstract intermediates
    #      defined locally don't count. Catches the
    #      `class Framework(BaseAdapter): pass` + `class Concrete(Framework):`
    #      pattern where vars(mod) has BOTH and we'd otherwise count
    #      both as "real" adapters.
    import inspect  # noqa: PLC0415
    # Deduplicate by class identity. Many production adapters do
    # `Adapter = ConcreteAdapter` as a module-level alias for the
    # runtime's discovery — `vars(mod)` returns both bindings
    # (`Adapter` AND `ConcreteAdapter`) pointing at the same class
    # object. Without dedup, the multiple-concrete-subclasses
    # error fires falsely on every aliased template.
    adapter_classes = list({
        id(obj): obj
        for name, obj in vars(mod).items()
        if isinstance(obj, type)
        and obj is not BaseAdapter
        and issubclass(obj, BaseAdapter)
        and getattr(obj, "__module__", None) == module_name
        and not inspect.isabstract(obj)
    }.values())
    sys.modules.pop(module_name, None)

    if not adapter_classes:
        err(
            "adapter.py: no concrete class inheriting from "
            "`molecule_runtime.adapters.base.BaseAdapter` defined "
            "in this file. The runtime resolves the adapter via "
            "class discovery on adapter.py's own definitions — "
            "imports of base classes from molecule_runtime do not "
            "count, and abstract intermediates do not count. "
            "Without a concrete subclass DEFINED here, workspace "
            "boot falls through to the packaged default adapter "
            "and ignores this file silently. If that's intentional, "
            "delete adapter.py."
        )
        return

    if len(adapter_classes) > 1:
        names = sorted(c.__name__ for c in adapter_classes)
        err(
            f"adapter.py: multiple concrete BaseAdapter subclasses "
            f"defined: {names}. The runtime's class-discovery picks "
            f"one per its own resolution rules (typically last-defined "
            f"or first-by-iteration), so shipping more than one is a "
            f"silent ambiguity — the wrong class might be loaded after "
            f"a future runtime refactor. Either keep exactly one "
            f"concrete subclass + mark the others abstract via "
            f"`abc.ABC` / abstract methods, or move them to separate "
            f"importable modules."
        )


# ───────────────────────────────── platform-model SSOT drift gate
#
# The controlplane providers manifest (internal/providers/providers.yaml
# `runtimes:` block) is the SINGLE source of truth for which
# platform-managed (Molecule-billed) models each runtime offers (RFC
# internal#580 Option C). A template's config.yaml `runtime_config.models`
# entries tagged `provider: platform` are a PROJECTION of that SSOT — they
# must be a SUBSET. Offering a platform model the manifest doesn't declare
# risks shipping an unservable option (the SEO 1033 / "Exception: success"
# class), so we gate it here.
#
# Best-effort by design: if the manifest can't be fetched (no network /
# git access in this CI context) we WARN and skip rather than couple every
# template's CI to controlplane reachability. The deploy-time e2e
# platform-models smoke (molecule-controlplane) is the hard backstop that
# actually proves servability.

def _template_platform_models(config: dict) -> list[str]:
    rc = config.get("runtime_config") or {}
    out = []
    for m in rc.get("models") or []:
        if isinstance(m, dict) and str(m.get("provider", "")).strip().lower() == "platform":
            mid = m.get("id")
            if mid:
                out.append(mid)
    return out


def _fetch_providers_manifest() -> dict | None:
    """Load the controlplane providers manifest. PROVIDERS_MANIFEST_FILE
    (a local path) short-circuits the fetch for tests / offline. Otherwise
    a blobless sparse `git` clone pulls just providers.yaml using the
    runner's ambient git credentials (same access the molecule-ci clone
    uses). Returns the parsed dict, or None on any failure."""
    local = os.environ.get("PROVIDERS_MANIFEST_FILE")
    if local:
        try:
            with open(local, encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception:
            return None
    import shutil
    import subprocess
    import tempfile
    repo = os.environ.get(
        "PROVIDERS_MANIFEST_REPO",
        "https://git.moleculesai.app/molecule-ai/molecule-controlplane.git",
    )
    rel = "internal/providers/providers.yaml"
    # sparse-checkout cone mode (the default) takes DIRECTORY paths, not file
    # paths — `set internal/providers/providers.yaml` fails ("not a
    # directory"). Use the containing directory; the file read below narrows it.
    sparse_dir = "internal/providers"
    tmp = tempfile.mkdtemp(prefix="cp-manifest-")
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", repo, tmp],
            check=True, capture_output=True, timeout=60,
        )
        subprocess.run(
            ["git", "-C", tmp, "sparse-checkout", "set", sparse_dir],
            check=True, capture_output=True, timeout=30,
        )
        with open(os.path.join(tmp, rel), encoding="utf-8") as f:
            return yaml.safe_load(f)
    except subprocess.CalledProcessError as e:
        # Log stderr so a future fetch breakage is visible, not a silent skip.
        stderr = (e.stderr or b"").decode("utf-8", "replace")[-300:] if isinstance(e.stderr, bytes) else str(e.stderr or "")[-300:]
        print(f"::warning::providers manifest fetch failed (git {e.returncode}): {stderr.strip()}")
        return None
    except Exception as e:
        print(f"::warning::providers manifest fetch failed: {e}")
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_platform_models() -> None:
    if not os.path.isfile("config.yaml"):
        return  # check_config_yaml already errored
    try:
        with open("config.yaml") as f:
            config = yaml.safe_load(f)
    except Exception:
        return  # check_config_yaml already errored on the parse
    if not isinstance(config, dict):
        return

    tmpl_models = _template_platform_models(config)
    if not tmpl_models:
        return  # nothing platform-managed to gate

    runtime = config.get("runtime")
    manifest = _fetch_providers_manifest()
    if manifest is None:
        warn(
            "platform-model SSOT drift check skipped: could not load the controlplane "
            "providers manifest (no git/network access here, or set "
            "PROVIDERS_MANIFEST_FILE). The deploy-time platform-models e2e smoke is the "
            "backstop."
        )
        return

    runtimes = (manifest.get("runtimes") or {})
    if runtime not in runtimes:
        warn(
            f"platform-model SSOT drift check skipped: runtime `{runtime}` is not in the "
            f"controlplane providers manifest runtimes block, so its platform set is "
            f"undefined there. Add it to providers.yaml to enable the gate."
        )
        return

    allowed = set()
    for ref in (runtimes[runtime].get("providers") or []):
        if ref.get("name") == "platform":
            allowed.update(ref.get("models") or [])

    extra = [m for m in tmpl_models if m not in allowed]
    if extra:
        err(
            f"config.yaml: runtime `{runtime}` offers platform model(s) {sorted(extra)} "
            f"NOT in the controlplane providers manifest's platform set for this runtime "
            f"({sorted(allowed)}). That manifest (internal/providers/providers.yaml "
            f"runtimes block) is the SSOT for platform-managed models — declare them there "
            f"first, or remove them here. Offering a platform model the SSOT doesn't "
            f"declare risks an unservable option (the 1033 class)."
        )
    else:
        print(f"✓ platform models {sorted(tmpl_models)} ⊆ manifest platform set for `{runtime}`")


# ─────────────────────── full-providers + runtime-native-set drift gate
#
# internal#718 P4 PR-3 (Audit-A finding #4 / #715C closure): extend the
# platform-only drift gate above to the FULL providers block + runtime native
# sets. The controlplane providers manifest (internal/providers/providers.yaml)
# is the SSOT for not just platform-managed models but every (provider, model)
# pair each runtime natively supports. A template's `runtime_config.models`
# entries — across ALL providers, not just `platform` — must be a subset of the
# manifest's per-runtime native set for that runtime.
#
# Pre-P4 the gate only caught drift on `provider: platform` models — the 1033
# class. After PR-1's colon-vocab reconcile the registry now lists every
# legitimate (runtime, model) pair (bare + slash + colon forms across each
# runtime's native providers); pre-PR4 the FULL set was unenforced and
# templates could silently offer e.g. `nousresearch/hermes-4-70b` on hermes
# (drift outside the CTO kimi-only matrix). PR-4 codegen will retire the
# hand-authored providers block entirely, but the drift gate is what keeps the
# template in sync with the registry as long as the providers block is still
# hand-authored.
#
# Semantics: every `runtime_config.models[*].id` whose `provider:` is in the
# manifest's per-runtime native provider name set MUST also be in that
# provider's native model set. Models with a `provider:` that is NOT in the
# native set are themselves a drift signal (the template is offering a model
# routed through a provider the runtime cannot natively serve).
#
# Same best-effort posture as check_platform_models: skip on manifest-fetch
# failure (warn, do not error); skip on runtime absent from the manifest
# (federation-friendly).
#
# Fail OPEN intentionally on templates that DO NOT declare runtime_config.models
# at all: the legacy templates carry only a top-level `model:` and no per-model
# entries; gating those would be a behavior change orthogonal to this PR.

def _template_models_by_provider(config: dict) -> dict[str, list[str]]:
    """Group runtime_config.models entries by their declared `provider:`."""
    rc = config.get("runtime_config") or {}
    out: dict[str, list[str]] = {}
    for m in rc.get("models") or []:
        if not isinstance(m, dict):
            continue
        prov = str(m.get("provider", "")).strip()
        mid = m.get("id")
        if not mid:
            continue
        out.setdefault(prov, []).append(str(mid))
    return out


def check_full_providers_block() -> None:
    """internal#718 P4 PR-3: validate template's FULL runtime_config.models
    (every provider, not just `platform`) against the manifest's per-runtime
    native (provider, model) matrix."""
    if not os.path.isfile("config.yaml"):
        return  # check_config_yaml already errored
    try:
        with open("config.yaml") as f:
            config = yaml.safe_load(f)
    except Exception:
        return  # check_config_yaml already errored
    if not isinstance(config, dict):
        return

    by_prov = _template_models_by_provider(config)
    if not by_prov:
        return  # no per-model entries — nothing to gate (legacy top-level `model:` shape)

    runtime = config.get("runtime")
    manifest = _fetch_providers_manifest()
    if manifest is None:
        warn(
            "full-providers SSOT drift check skipped (P4 PR-3): could not load the "
            "controlplane providers manifest. The deploy-time platform-models e2e "
            "smoke + the workspace-server only-registered create gate (internal#718 "
            "P4 PR-2 422 UNREGISTERED_MODEL_FOR_RUNTIME) remain backstops."
        )
        return

    runtimes = (manifest.get("runtimes") or {})
    if runtime not in runtimes:
        # Federation-friendly: a non-first-party runtime is not in the registry
        # by design. The workspace-server only-registered gate fails OPEN for it
        # too; do not block here.
        warn(
            f"full-providers SSOT drift check skipped (P4 PR-3): runtime `{runtime}` is "
            f"not in the controlplane providers manifest. Federation-runtime templates "
            f"are gated by the deploy-time only-registered fail-open path, not here."
        )
        return

    # Build the per-runtime native (provider → set-of-models) matrix from the
    # manifest. Both keys (provider names) AND values (model id sets) are the
    # gate inputs.
    native: dict[str, set[str]] = {}
    for ref in (runtimes[runtime].get("providers") or []):
        native[ref.get("name")] = set(ref.get("models") or [])

    if not native:
        warn(
            f"full-providers SSOT drift check skipped (P4 PR-3): runtime `{runtime}` "
            f"has an empty native provider set in the manifest. Declare its native "
            f"matrix in providers.yaml to enable this gate."
        )
        return

    # Two failure modes:
    #
    #   (a) The template references a `provider:` that is NOT in the runtime's
    #       native set at all. The template is offering a model routed through
    #       a provider the runtime cannot natively serve — over-offer drift.
    #
    #   (b) The provider IS native but the specific model id is NOT in that
    #       provider's native model set — model-id drift (same class as the
    #       pre-PR3 platform gate, generalized to every provider).
    unknown_providers: dict[str, list[str]] = {}
    extra_models_by_prov: dict[str, list[str]] = {}
    for prov, mids in by_prov.items():
        if not prov:
            # Models without an explicit `provider:` are out of scope (the
            # workspace adapter infers their provider at boot; gating those
            # requires the inferVendor heuristic which is a different layer).
            continue
        if prov not in native:
            unknown_providers[prov] = sorted(set(mids))
            continue
        extra = [m for m in mids if m not in native[prov]]
        if extra:
            extra_models_by_prov[prov] = sorted(set(extra))

    if unknown_providers:
        for prov, mids in sorted(unknown_providers.items()):
            err(
                f"config.yaml: runtime `{runtime}` offers models {mids} routed through "
                f"provider `{prov}`, which is NOT in the runtime's NATIVE provider set "
                f"per the controlplane providers manifest ({sorted(native.keys())}). "
                f"Either remove these entries, or declare `{prov}` as a native provider "
                f"for `{runtime}` in providers.yaml. (internal#718 P4 PR-3 — extends the "
                f"platform-only gate to the full providers block.)"
            )

    if extra_models_by_prov:
        for prov, mids in sorted(extra_models_by_prov.items()):
            err(
                f"config.yaml: runtime `{runtime}` provider `{prov}` offers model(s) {mids} "
                f"NOT in the manifest's native model set for `{prov}` "
                f"({sorted(native[prov])}). Add the ids to providers.yaml's runtimes "
                f"block first, or remove them here. (internal#718 P4 PR-3.)"
            )

    if not unknown_providers and not extra_models_by_prov:
        flat = sorted({m for mids in by_prov.values() for m in mids})
        print(f"✓ full providers block ⊆ manifest native (provider, model) matrix for `{runtime}` ({len(flat)} model id(s))")


# ─────────────────── SSOT-inheritance enforcement (official templates) ───────────
#
# The principal's rule: the OFFICIAL repo must ENFORCE the SSOT, not merely
# rely on convention. An official-plugin workspace template MUST NOT hardcode
# the DEFAULT provider/model, nor pin the Molecule platform LLM proxy base_url,
# in its config.yaml. Those are resolved at PROVISION time by the controlplane:
#
#   • the LLM routing mode (platform vs byok) is derived from the env-identity
#     SSOT — molecule-controlplane internal/provisioner/llm_mode.go
#     (ResolveLLMMode + LLMModeForEnv): production/staging/e2e → platform,
#     dev → byok;
#   • the platform proxy endpoint + usage-token auth are INJECTED by the CP
#     (PlatformLLMProxyEnv → MOLECULE_LLM_*/ANTHROPIC_BASE_URL/…), never pinned
#     per template;
#   • the default model comes from the providers.yaml registry SSOT.
#
# A template that RE-PINS any of these re-introduces exactly the silent
# prod-routing drift the CP SSOT eliminated — the "Not logged in" /
# unservable-option class (a workspace whose pinned model→provider has no
# usable auth on SaaS). So we gate it here, per-PR.
#
# OFF by default: this gate only fires under --official, so community templates
# (which legitimately bring their own provider/model/base_url) are unaffected,
# and un-migrated repos that don't pass --official stay green.
#
# --allow-self-model exempts ONLY the top-level `model:` key. It exists for the
# platform-agent (Org Concierge) template whose OWN declared model IS its
# identity per core#2594 — that top-level `model:` is the load-bearing,
# provision-validated concierge model, NOT a user-workspace default. Every
# OTHER pin (runtime_config.model / runtime_config.provider / proxy base_url)
# is still flagged even under --allow-self-model.
#
# --allow-platform-route exempts the platform-agent's CP-proxy ROUTE: a
# `runtime_config.provider: platform` default AND a `platform`-named provider
# entry's Molecule proxy base_url. The concierge is ALWAYS platform-managed
# (every turn routes through the CP proxy for billing/audit), so those are the
# proxy ROUTE, not a vendor lock-in (principal Rule #13 forbids VENDOR pins, not
# the platform route). The CURRENT de-pinned concierge template inherits its
# MODEL from the SSOT (no model pin) but keeps this route, so the gate runs with
# --allow-platform-route (NOT --allow-self-model — a re-pinned concierge model is
# STILL gated). A VENDOR provider, or a proxy base_url on a non-`platform` entry,
# is flagged regardless of the flag.
#
# What it does NOT flag: a `runtime_config.models` CATALOG (the user-selectable
# menu + per-entry required_env). That catalog is the legitimate per-template
# surface and is already kept ⊆ the registry SSOT by check_full_providers_block
# / check_platform_models. Only a DEFAULT pin (model/provider/proxy) is the
# SSOT-inheritance violation this gate exists to catch.

# Substring identifying the Molecule platform-managed LLM proxy in a provider
# base_url. The CP builds these as "<cp>/api/v1/internal/llm/anthropic" and
# "<cp>/api/v1/internal/llm/openai/v1" (PlatformLLMProxyEnv). A template config
# carrying this literal has PINNED the proxy instead of letting the CP inject it
# from the env-derived SSOT. Third-party provider base_urls (api.kimi.com/…,
# api.minimax.io/…) do NOT contain this path, so they are not false-flagged.
_PLATFORM_PROXY_PATH_MARK = "internal/llm/"


def _is_set(v) -> bool:
    """A config value counts as a PIN only when it's a non-None, non-blank
    scalar. `model:` with no value (None) or an empty string is treated as
    'unset' so a stray-but-empty key isn't flagged as a hardcoded default."""
    if v is None:
        return False
    if isinstance(v, str) and v.strip() == "":
        return False
    return True


def _iter_provider_base_urls(config: dict):
    """Yield (where, name, base_url) for every provider entry declaring a
    base_url, across the top-level `providers:` registry AND a
    `runtime_config.providers` list when it carries dict entries (some
    templates inline the registry under runtime_config)."""
    def _scan(block, where):
        if not isinstance(block, list):
            return
        for entry in block:
            if isinstance(entry, dict) and _is_set(entry.get("base_url")):
                yield where, entry.get("name"), str(entry["base_url"])
    yield from _scan(config.get("providers"), "providers")
    rc = config.get("runtime_config")
    if isinstance(rc, dict):
        yield from _scan(rc.get("providers"), "runtime_config.providers")


def check_no_hardcoded_provider_model(official: bool, allow_self_model: bool,
                                      allow_platform_route: bool = False) -> None:
    """--official SSOT-inheritance gate — ERROR on a re-pinned official
    template; pass when the template is silent / inherits. See header above.

    allow_platform_route (--allow-platform-route) exempts the platform-agent
    (Org Concierge) template's LEGITIMATE platform-proxy ROUTE declarations:
    `runtime_config.provider: platform` AND a `platform`-named provider entry's
    Molecule-proxy base_url. The concierge is ALWAYS platform-managed (it routes
    every turn through the CP proxy for billing/audit), so those are the proxy
    ROUTE, not a vendor lock-in (principal Rule #13 forbids VENDOR pins, not the
    platform route). It does NOT exempt a model pin — a re-pinned concierge model
    is still gated, which is the whole point."""
    if not official:
        return  # gate is opt-in; community + un-migrated templates unaffected
    if not os.path.isfile("config.yaml"):
        return  # check_config_yaml already errored
    before = len(ERRORS)
    try:
        with open("config.yaml") as f:
            config = yaml.safe_load(f)
    except Exception:
        return  # check_config_yaml already errored on the parse
    if not isinstance(config, dict):
        return

    # 1. top-level `model:` pin (exempt only with --allow-self-model)
    if _is_set(config.get("model")):
        if allow_self_model:
            print(
                "::notice::--allow-self-model: top-level `model:` exempt "
                "(platform-agent self-declared concierge model, core#2594)"
            )
        else:
            err(
                "config.yaml: official templates must NOT hardcode a top-level "
                "`model:` — the controlplane resolves the default model from the "
                "env-derived LLM-mode SSOT + providers.yaml registry at provision. "
                "Remove it so the workspace inherits the SSOT default. (For the "
                "platform-agent concierge whose own model is its identity per "
                "core#2594, run the lint with --allow-self-model.)"
            )

    rc = config.get("runtime_config")
    if isinstance(rc, dict):
        # 2. default-model pin
        if _is_set(rc.get("model")):
            err(
                "config.yaml: official templates must NOT pin `runtime_config.model` "
                "(the default model). The CP injects MODEL_PROVIDER=platform + the "
                "providers.yaml SSOT default at provision; a re-pin here re-introduces "
                "the prod-routing drift (the 'Not logged in' class). Drop it and let "
                "the runtime inherit the SSOT default."
            )
        # 3. default-provider pin. Provider selection (platform vs byok) is the
        #    CP's call (ResolveLLMMode/LLMModeForEnv), so a generic official
        #    template must NOT pin it. EXCEPTION (--allow-platform-route): the
        #    platform-agent (Org Concierge) is ALWAYS platform-managed and keeps
        #    `runtime_config.provider: platform` deliberately — that is the
        #    CP-proxy ROUTE, not a vendor lock-in (Rule #13). A VENDOR name
        #    (minimax/moonshot/openai/...) is ALWAYS the drift and is flagged
        #    regardless of the flag.
        rc_provider = rc.get("provider")
        if _is_set(rc_provider):
            prov = str(rc_provider).strip()
            if prov.lower() == "platform" and allow_platform_route:
                print(
                    "::notice::--allow-platform-route: `runtime_config.provider: platform` "
                    "exempt (platform-agent CP-proxy route, Rule #13)"
                )
            else:
                kind = "a VENDOR" if prov.lower() != "platform" else "the"
                hint = (
                    " (the platform PROXY ROUTE is allowed for the platform-agent "
                    "concierge via --allow-platform-route)"
                    if prov.lower() == "platform" else ""
                )
                err(
                    f"config.yaml: official templates must NOT pin {kind} "
                    f"`runtime_config.provider` (got {prov!r}). Provider selection "
                    "(platform vs byok) is resolved by the CP from the env-derived "
                    f"LLM-mode SSOT (ResolveLLMMode/LLMModeForEnv). Drop it and inherit.{hint}"
                )

    # 4. platform-proxy base_url pin. The CP injects the proxy endpoint +
    #    MOLECULE_LLM_USAGE_TOKEN auth (PlatformLLMProxyEnv), so a generic
    #    template must NOT hardcode it. EXCEPTION (--allow-platform-route): a
    #    `platform`-named provider entry in the platform-agent's own registry
    #    legitimately carries the proxy base_url (its structural CP-proxy route,
    #    overridden by the CP at provision on SaaS, used as-is on self-host). A
    #    proxy base_url on any NON-`platform` entry is still flagged.
    pinned = []
    for where, name, base_url in _iter_provider_base_urls(config):
        if _PLATFORM_PROXY_PATH_MARK in base_url.lower():
            if allow_platform_route and str(name).strip().lower() == "platform":
                continue  # platform-agent's legitimate platform-route registry entry
            pinned.append(f"{where}[{name}].base_url={base_url}")
    if pinned:
        err(
            "config.yaml: official templates must NOT hardcode the Molecule platform "
            "LLM proxy base_url — that endpoint + its MOLECULE_LLM_USAGE_TOKEN auth "
            "are injected by the controlplane (PlatformLLMProxyEnv) from the "
            f"env-derived SSOT, not pinned per template. Pinned proxy base_url(s): "
            f"{sorted(pinned)}. Deliver the provider registry as the SSOT artifact and "
            "drop the hardcoded proxy base_url. (The platform-agent concierge's own "
            "`platform`-named registry entry is exempt via --allow-platform-route.)"
        )

    if len(ERRORS) == before:
        print("✓ official SSOT-inheritance: no hardcoded provider/model/proxy pin")


def main() -> None:
    # --static-only skips check_adapter_runtime_load(), which calls
    # importlib's exec_module() on the template's adapter.py. That's
    # untrusted code execution — fine on internal PRs and post-merge,
    # unsafe on external fork PRs (#135). Static checks (file presence,
    # YAML parse, regex/AST inspection) stay enabled in static mode.
    static_only = "--static-only" in sys.argv
    # --official turns on the SSOT-inheritance enforcement gate
    # (check_no_hardcoded_provider_model). It is purely static (config.yaml
    # inspection, no code execution), so it runs in BOTH --static-only and
    # full modes — fork PRs on official templates are gated too.
    official = "--official" in sys.argv
    # --allow-self-model exempts ONLY the top-level `model:` (platform-agent).
    allow_self_model = "--allow-self-model" in sys.argv
    # --allow-platform-route exempts the platform-agent (Org Concierge) template's
    # CP-proxy ROUTE declarations: `runtime_config.provider: platform` and a
    # `platform`-named provider entry's Molecule-proxy base_url (Rule #13: the
    # platform route is not a vendor pin). It does NOT exempt a model pin.
    allow_platform_route = "--allow-platform-route" in sys.argv

    # --ssot-inheritance-only runs JUST the SSOT-inheritance gate
    # (check_no_hardcoded_provider_model), skipping the Dockerfile / adapter.py /
    # requirements / providers-manifest structural checks. It exists for OFFICIAL
    # CONFIG-OVERLAY templates that are NOT standard runtime images — notably the
    # platform-agent (Org Concierge), which ships only config.yaml + prompts (no
    # Dockerfile / adapter.py / template_schema_version). Those repos still want
    # the model-re-pin gate without being forced into the full runtime-template
    # contract. Implies --official (the gate is the whole point here).
    ssot_only = "--ssot-inheritance-only" in sys.argv
    if ssot_only:
        check_no_hardcoded_provider_model(True, allow_self_model, allow_platform_route)
        for e in ERRORS:
            print(f"::error::{e}")
        if ERRORS:
            sys.exit(1)
        print("✓ SSOT-inheritance gate passed (config-overlay official template)")
        return

    check_dockerfile()
    check_config_yaml()
    check_platform_models()
    check_full_providers_block()
    check_no_hardcoded_provider_model(official, allow_self_model, allow_platform_route)
    check_requirements()
    check_adapter()
    if not static_only:
        check_adapter_runtime_load()
    else:
        print("::notice::skipping adapter.py import check (--static-only mode)")

    for w in WARNINGS:
        print(f"::warning::{w}")
    for e in ERRORS:
        print(f"::error::{e}")
    if ERRORS:
        sys.exit(1)
    suffix = " [static-only]" if static_only else ""
    print(f"✓ Template validation passed ({len(WARNINGS)} warning(s)){suffix}")


if __name__ == "__main__":
    main()
