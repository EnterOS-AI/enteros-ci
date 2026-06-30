"""Tests for validate-workspace-template.py — pin the drift contract.

Each test materialises a tiny template directory in a tmpdir, runs the
validator's check functions in-process, and asserts on the captured
ERRORS / WARNINGS lists. The 8 template repos in the wild are the
ground-truth integration test (CI runs this validator against each on
push), but those repos can change at any time. These tests pin the
contract itself so a refactor of the validator can't silently weaken
it.

Important: the validator was chosen to be import-safe (no top-level
side effects), so the test patches the cwd via os.chdir into tmpdirs.
The module's ERRORS/WARNINGS lists are reset at the start of each
test via _reset_validator_state().
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


VALIDATOR_PATH = Path(__file__).resolve().parent / "validate-workspace-template.py"


def _load_validator():
    """Load the validator module by path (its filename has a hyphen so
    we can't `import validate-workspace-template` directly)."""
    spec = importlib.util.spec_from_file_location("validator", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def validator(monkeypatch):
    """Fresh validator module per test, cwd pinned to tmpdir below."""
    mod = _load_validator()
    mod.ERRORS.clear()
    mod.WARNINGS.clear()
    return mod


def _good_dockerfile() -> str:
    """Canonical Dockerfile that should pass every check."""
    return (
        "FROM python:3.11-slim\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN useradd -u 1000 -m -s /bin/bash agent\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        'RUN pip install -r requirements.txt && \\\n'
        '    if [ -n "${RUNTIME_VERSION}" ]; then \\\n'
        '      pip install --upgrade "molecule-ai-workspace-runtime==${RUNTIME_VERSION}"; \\\n'
        '    fi\n'
        'ENTRYPOINT ["molecule-runtime"]\n'
    )


def _good_config_yaml() -> str:
    return (
        "name: test-template\n"
        "runtime: claude-code\n"
        "template_schema_version: 1\n"
        "description: A test template\n"
        "tier: 1\n"
    )


def _good_requirements_txt() -> str:
    return "molecule-ai-workspace-runtime>=0.1.0\n"


def _materialise(tmp_path: Path, dockerfile: str | None = None,
                 config_yaml: str | None = None,
                 requirements: str | None = None,
                 adapter_py: str | None = None) -> None:
    if dockerfile is not None:
        (tmp_path / "Dockerfile").write_text(dockerfile)
    if config_yaml is not None:
        (tmp_path / "config.yaml").write_text(config_yaml)
    if requirements is not None:
        (tmp_path / "requirements.txt").write_text(requirements)
    if adapter_py is not None:
        (tmp_path / "adapter.py").write_text(adapter_py)


# ───────────────────────────────────────────────────────── happy paths

def test_canonical_template_passes(validator, tmp_path, monkeypatch):
    _materialise(
        tmp_path,
        dockerfile=_good_dockerfile(),
        config_yaml=_good_config_yaml(),
        requirements=_good_requirements_txt(),
    )
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    validator.check_config_yaml()
    validator.check_requirements()
    validator.check_adapter()
    assert validator.ERRORS == [], validator.ERRORS


def test_custom_entrypoint_script_passes_when_it_execs_runtime(validator, tmp_path, monkeypatch):
    """claude-code style: ENTRYPOINT [/entrypoint.sh] + entrypoint.sh
    that exec's molecule-runtime at the end. Must pass."""
    df = (
        "FROM python:3.11-slim\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN useradd -u 1000 -m -s /bin/bash agent\n"
        "COPY requirements.txt .\n"
        'RUN pip install -r requirements.txt && \\\n'
        '    if [ -n "${RUNTIME_VERSION}" ]; then \\\n'
        '      pip install --upgrade "molecule-ai-workspace-runtime==${RUNTIME_VERSION}"; \\\n'
        '    fi\n'
        "COPY entrypoint.sh /entrypoint.sh\n"
        'ENTRYPOINT ["/entrypoint.sh"]\n'
    )
    ep = (
        "#!/bin/sh\n"
        "set -e\n"
        '# drop privileges then exec the runtime\n'
        'exec gosu agent molecule-runtime "$@"\n'
    )
    _materialise(
        tmp_path,
        dockerfile=df,
        config_yaml=_good_config_yaml(),
        requirements=_good_requirements_txt(),
    )
    (tmp_path / "entrypoint.sh").write_text(ep)
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert validator.ERRORS == [], validator.ERRORS


# ───────────────────────────────────────────────────────── Dockerfile drift

def test_wrong_base_image_errors(validator, tmp_path, monkeypatch):
    df = _good_dockerfile().replace("python:3.11-slim", "python:3.10-alpine")
    _materialise(tmp_path, dockerfile=df, config_yaml=_good_config_yaml(),
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert any("FROM python:3.11-slim" in e for e in validator.ERRORS)


def test_missing_arg_runtime_version_errors(validator, tmp_path, monkeypatch):
    """Without ARG RUNTIME_VERSION, the cascade rebuild silently ships
    the previous runtime — the cache trap that bit us 5x on 2026-04-27."""
    df = _good_dockerfile().replace("ARG RUNTIME_VERSION=\n", "")
    _materialise(tmp_path, dockerfile=df, config_yaml=_good_config_yaml(),
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert any("ARG RUNTIME_VERSION" in e for e in validator.ERRORS)


def test_missing_runtime_version_in_run_block_errors(validator, tmp_path, monkeypatch):
    """ARG declared but NEVER referenced in a RUN — same cache-trap,
    different shape. Pin both."""
    df = (
        "FROM python:3.11-slim\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN useradd -u 1000 -m -s /bin/bash agent\n"
        "RUN pip install molecule-ai-workspace-runtime\n"
        'ENTRYPOINT ["molecule-runtime"]\n'
    )
    _materialise(tmp_path, dockerfile=df, config_yaml=_good_config_yaml(),
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert any("RUNTIME_VERSION" in e and "RUN block" in e for e in validator.ERRORS)


def test_missing_agent_user_errors(validator, tmp_path, monkeypatch):
    df = _good_dockerfile().replace("RUN useradd -u 1000 -m -s /bin/bash agent\n", "")
    _materialise(tmp_path, dockerfile=df, config_yaml=_good_config_yaml(),
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert any("agent" in e for e in validator.ERRORS)


def test_missing_entrypoint_errors(validator, tmp_path, monkeypatch):
    df = _good_dockerfile().replace('ENTRYPOINT ["molecule-runtime"]\n', "")
    _materialise(tmp_path, dockerfile=df, config_yaml=_good_config_yaml(),
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert any("molecule-runtime" in e and ("ENTRYPOINT" in e or "entrypoint" in e)
               for e in validator.ERRORS)


# ───────────────────────────────────────────────────────── config.yaml drift

def test_missing_required_keys_errors(validator, tmp_path, monkeypatch):
    """A config without template_schema_version short-circuits with a
    SINGLE actionable error — listing 'also name and runtime are
    missing' is noise on top of the real problem (no version means the
    validator can't pick a schema contract to enforce). Once the
    version is present, the v1 dispatch will list the other missing
    keys (next test pins that)."""
    cfg = "description: only description, no name/runtime/version\n"
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_config_yaml()
    missing_msgs = [e for e in validator.ERRORS if "missing required key" in e]
    # Exactly one error: the missing version. v1 dispatch is skipped
    # because we can't choose a contract without a version.
    assert len(missing_msgs) == 1, missing_msgs
    assert "template_schema_version" in missing_msgs[0]


def test_missing_required_keys_under_v1_dispatch_errors(validator, tmp_path, monkeypatch):
    """When `template_schema_version: 1` IS present but other required
    keys are missing, the v1 dispatch fires and lists them. Pins that
    the v1 contract still enforces name + runtime."""
    cfg = (
        "template_schema_version: 1\n"
        "description: only the version + description\n"
    )
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_config_yaml()
    missing_msgs = [e for e in validator.ERRORS if "missing required key" in e]
    keys = {e.split("`")[1] for e in missing_msgs}
    assert "name" in keys, missing_msgs
    assert "runtime" in keys, missing_msgs


def test_string_template_schema_version_errors(validator, tmp_path, monkeypatch):
    cfg = (
        "name: t\n"
        "runtime: claude-code\n"
        'template_schema_version: "1"\n'  # str, not int
    )
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_config_yaml()
    assert any("template_schema_version must be int" in e for e in validator.ERRORS)


def test_unknown_runtime_warns_not_errors(validator, tmp_path, monkeypatch):
    cfg = _good_config_yaml().replace("claude-code", "my-experimental-runtime")
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_config_yaml()
    assert any("not in known set" in w for w in validator.WARNINGS)
    assert validator.ERRORS == []  # custom runtimes are allowed


def test_unknown_top_level_keys_warn(validator, tmp_path, monkeypatch):
    cfg = _good_config_yaml() + "weird_drift_key: something\n"
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_config_yaml()
    assert any("unknown top-level keys" in w and "weird_drift_key" in w
               for w in validator.WARNINGS)


# ───────────────────────────────────────────────────────── requirements.txt

def test_missing_runtime_in_requirements_errors(validator, tmp_path, monkeypatch):
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=_good_config_yaml(),
                 requirements="fastapi\n")
    monkeypatch.chdir(tmp_path)
    validator.check_requirements()
    assert any("molecule-ai-workspace-runtime" in e for e in validator.ERRORS)


# ───────────────────────────────────────────────────────── adapter.py

def test_legacy_molecule_ai_import_warns(validator, tmp_path, monkeypatch):
    """Pre-#87 package was named differently. Catch any laggards."""
    adapter = "from molecule_ai.adapter_base import BaseAdapter\n"
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter()
    assert any("molecule_ai" in w for w in validator.WARNINGS)


def test_modern_molecule_runtime_import_does_not_warn(validator, tmp_path, monkeypatch):
    """Regression cover: the original validator's warning ('don't import
    molecule_runtime') was BACKWARDS — that's the canonical name now.
    Pin that the new validator does NOT emit a false positive."""
    adapter = "from molecule_runtime.adapter_base import BaseAdapter\n"
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter()
    legacy_warnings = [w for w in validator.WARNINGS if "molecule_ai" in w]
    assert legacy_warnings == [], legacy_warnings


# ──────────────────── adapter.py runtime-load (strong contract)
#
# These tests pin the contract that adapter.py must be importable AND
# define at least one BaseAdapter subclass — the same path the runtime
# uses at workspace boot. Skipped when molecule-ai-workspace-runtime
# isn't installed in the test environment (the validator's CI workflow
# guarantees it via `pip install -r requirements.txt` before invoking
# the validator; local pytest can run with or without it).

def _has_runtime_installed() -> bool:
    """True if molecule-ai-workspace-runtime is importable. Used to skip
    the runtime-load tests when running pytest locally without the
    runtime in the venv."""
    try:
        import molecule_runtime.adapters.base  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False


_RUNTIME_AVAILABLE = _has_runtime_installed()
_skip_no_runtime = pytest.mark.skipif(
    not _RUNTIME_AVAILABLE,
    reason="molecule-ai-workspace-runtime not installed in test env",
)


def test_no_adapter_skips_runtime_load_silently(validator, tmp_path, monkeypatch):
    """No adapter.py = use default langgraph executor from the wheel.
    That's policy, not drift, so runtime-load check should not fire."""
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    # No ERRORS, no runtime-load WARNINGS specifically.
    runtime_load_warnings = [
        w for w in validator.WARNINGS if "runtime-load check" in w
    ]
    assert validator.ERRORS == [], validator.ERRORS
    assert runtime_load_warnings == [], runtime_load_warnings


def _good_adapter_py() -> str:
    """A fully concrete BaseAdapter subclass — overrides every
    abstract method BaseAdapter declares. Mirrors the shape of all 8
    production templates so tests of the runtime-load check exercise
    the same path the real templates do."""
    return (
        "from molecule_runtime.adapters.base import BaseAdapter\n"
        "\n"
        "class MyAdapter(BaseAdapter):\n"
        "    @staticmethod\n"
        "    def name(): return 'test-adapter'\n"
        "    @staticmethod\n"
        "    def display_name(): return 'Test'\n"
        "    @staticmethod\n"
        "    def description(): return 'fixture adapter'\n"
        "    def setup(self, config): pass\n"
        "    def create_executor(self, config): return None\n"
    )


@_skip_no_runtime
def test_valid_baseadapter_subclass_passes(validator, tmp_path, monkeypatch):
    """The happy path: adapter.py defines a fully concrete class
    inheriting from BaseAdapter. All 8 production templates match
    this shape."""
    _materialise(tmp_path, adapter_py=_good_adapter_py())
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert validator.ERRORS == [], validator.ERRORS


@_skip_no_runtime
def test_adapter_with_no_baseadapter_subclass_errors(validator, tmp_path, monkeypatch):
    """The most insidious silent-failure mode: adapter.py imports
    cleanly, defines classes, but NONE inherit from BaseAdapter. The
    runtime's class-discovery would silently skip this file and fall
    through to the default executor — workspace would 'work' but with
    the wrong runtime. Must hard-error."""
    adapter = (
        "class JustSomePlainClass:\n"
        "    def run(self): pass\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert any(
        "no concrete class inheriting from" in e and "BaseAdapter" in e
        for e in validator.ERRORS
    ), validator.ERRORS


@_skip_no_runtime
def test_abstract_intermediate_alone_does_not_count(validator, tmp_path, monkeypatch):
    """A locally-defined abstract subclass (e.g., a framework-level
    intermediate that templates extend) must not satisfy the contract
    on its own. The runtime needs a CONCRETE class to instantiate;
    accepting an abstract one would let workspace boot fail at
    instantiation time instead of validator time."""
    adapter = (
        "from abc import abstractmethod\n"
        "from molecule_runtime.adapters.base import BaseAdapter\n"
        "\n"
        "class FrameworkAdapter(BaseAdapter):\n"
        "    @abstractmethod\n"
        "    def my_abstract_method(self): ...\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert any(
        "no concrete class inheriting from" in e
        for e in validator.ERRORS
    ), validator.ERRORS


@_skip_no_runtime
def test_abstract_plus_concrete_passes_with_concrete_only(validator, tmp_path, monkeypatch):
    """The legitimate factoring pattern: define an abstract framework-
    level intermediate, then a concrete leaf. Only the concrete leaf
    counts toward the "at least one" requirement — the framework
    intermediate is filtered out by `inspect.isabstract`."""
    adapter = (
        "from abc import abstractmethod\n"
        "from molecule_runtime.adapters.base import BaseAdapter\n"
        "\n"
        "class FrameworkAdapter(BaseAdapter):\n"
        "    @abstractmethod\n"
        "    def framework_specific_hook(self): ...\n"
        "\n"
        "class ConcreteAdapter(FrameworkAdapter):\n"
        "    def framework_specific_hook(self): pass\n"
        "    @staticmethod\n"
        "    def name(): return 'concrete'\n"
        "    @staticmethod\n"
        "    def display_name(): return 'Concrete'\n"
        "    @staticmethod\n"
        "    def description(): return 'leaf'\n"
        "    def setup(self, config): pass\n"
        "    def create_executor(self, config): return None\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert validator.ERRORS == [], validator.ERRORS


@_skip_no_runtime
def test_multiple_concrete_baseadapter_subclasses_errors(validator, tmp_path, monkeypatch):
    """Two concrete BaseAdapter subclasses in the same file is a
    silent ambiguity: the runtime's class-discovery picks one per
    its own resolution rules, so the WRONG class might be loaded
    after a future runtime refactor. Force the maintainer to either
    mark intermediates abstract or split into separate modules."""
    adapter = (
        "from molecule_runtime.adapters.base import BaseAdapter\n"
        "\n"
        "class FirstConcreteAdapter(BaseAdapter):\n"
        "    @staticmethod\n"
        "    def name(): return 'first'\n"
        "    @staticmethod\n"
        "    def display_name(): return 'First'\n"
        "    @staticmethod\n"
        "    def description(): return 'first'\n"
        "    def setup(self, config): pass\n"
        "    def create_executor(self, config): return None\n"
        "\n"
        "class SecondConcreteAdapter(BaseAdapter):\n"
        "    @staticmethod\n"
        "    def name(): return 'second'\n"
        "    @staticmethod\n"
        "    def display_name(): return 'Second'\n"
        "    @staticmethod\n"
        "    def description(): return 'second'\n"
        "    def setup(self, config): pass\n"
        "    def create_executor(self, config): return None\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    multi_errors = [
        e for e in validator.ERRORS
        if "multiple concrete BaseAdapter subclasses" in e
    ]
    assert len(multi_errors) == 1, validator.ERRORS
    # Both names should appear in the error so the operator knows
    # exactly which classes are competing.
    assert "FirstConcreteAdapter" in multi_errors[0]
    assert "SecondConcreteAdapter" in multi_errors[0]


@_skip_no_runtime
def test_aliased_concrete_class_is_deduplicated(validator, tmp_path, monkeypatch):
    """Production templates often do `Adapter = ConcreteAdapter` as a
    module-level alias for the runtime's class-discovery convention.
    `vars(mod)` returns BOTH bindings pointing at the same class
    object — without identity-based dedup, the multi-concrete-class
    error fires falsely (regression caught against the real langgraph
    template during the Q3 fix). Pin that aliased templates pass."""
    adapter = _good_adapter_py() + "\nAdapter = MyAdapter\n"
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert validator.ERRORS == [], validator.ERRORS


@_skip_no_runtime
def test_only_imported_baseadapter_subclass_does_not_count(validator, tmp_path, monkeypatch):
    """Re-exported imports do not satisfy the contract. If the only
    BaseAdapter subclass in adapter.py is something `from
    molecule_runtime.adapters.base import BaseAdapter` re-exports (or
    a future abstract intermediate), the runtime's class-discovery
    would correctly skip it — and the validator must too. Without
    this check, an `__module__`-filter regression would mask the
    'no concrete subclass' case the gate exists to catch.
    """
    adapter = (
        # This file imports BaseAdapter but never SUBCLASSES it.
        # `BaseAdapter` itself is in vars(mod) but it's already
        # filtered by `obj is not BaseAdapter`. The new __module__
        # filter ensures no third-party class slipping in via import
        # is counted either.
        "from molecule_runtime.adapters.base import BaseAdapter  # noqa: F401\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert any(
        "no concrete class inheriting from" in e
        for e in validator.ERRORS
    ), validator.ERRORS


@_skip_no_runtime
def test_adapter_with_syntax_error_errors(validator, tmp_path, monkeypatch):
    """SyntaxError at import is the same failure mode that crashes
    workspace boot. Catch it here."""
    adapter = "this is not valid python at all\n"
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert any("failed to import" in e for e in validator.ERRORS), validator.ERRORS


@_skip_no_runtime
def test_adapter_with_import_error_errors(validator, tmp_path, monkeypatch):
    """ImportError during adapter.py exec — same failure mode as
    workspace boot. The error message should point the contributor at
    requirements.txt as the right fix."""
    adapter = (
        "import this_package_definitely_does_not_exist_0xdeadbeef\n"
        "from molecule_runtime.adapters.base import BaseAdapter\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert any(
        "failed to import" in e and "ModuleNotFoundError" in e
        for e in validator.ERRORS
    ), validator.ERRORS


# ─────────────────────────────────────── schema-version dispatch
#
# Pin the contract that the validator routes to per-version checks
# based on `template_schema_version`, that unknown versions hard-fail,
# and that deprecated versions warn but pass.

def test_v1_is_in_known_schema_versions(validator):
    """Document the floor: v1 is always understood. Future bumps add
    versions; v1 stays accepted (or deprecated) but the validator
    never silently drops it."""
    assert 1 in validator.KNOWN_SCHEMA_VERSIONS or 1 in validator.DEPRECATED_SCHEMA_VERSIONS


def test_unknown_schema_version_errors(validator, tmp_path, monkeypatch):
    """A template declaring template_schema_version=999 must hard-fail
    — silently allowing it would let drift land disguised as a
    'future' version."""
    cfg = (
        "name: t\n"
        "runtime: claude-code\n"
        "template_schema_version: 999\n"
    )
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_config_yaml()
    assert any("template_schema_version=999 is unknown" in e
               for e in validator.ERRORS), validator.ERRORS


def test_deprecated_schema_version_warns_but_passes(validator, tmp_path, monkeypatch):
    """During a deprecation window, v<N-1> templates still validate
    (so the consumer can keep merging unrelated PRs while migrating)
    but the warning surfaces the migration command."""
    # Inject a fake deprecated version for the duration of this test —
    # we don't have a real deprecated version yet (only v1 exists).
    validator.KNOWN_SCHEMA_VERSIONS.add(2)
    validator.DEPRECATED_SCHEMA_VERSIONS.add(1)
    validator.SCHEMA_CHECKS[2] = lambda config: None  # accept-all stub for v2

    try:
        cfg = (
            "name: t\n"
            "runtime: claude-code\n"
            "template_schema_version: 1\n"
        )
        _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                     requirements=_good_requirements_txt())
        monkeypatch.chdir(tmp_path)
        validator.check_config_yaml()
        # No errors — deprecation is warning-only.
        assert validator.ERRORS == [], validator.ERRORS
        assert any(
            "template_schema_version=1 is deprecated" in w
            and "migrate-template.py" in w
            for w in validator.WARNINGS
        ), validator.WARNINGS
    finally:
        validator.KNOWN_SCHEMA_VERSIONS.discard(2)
        validator.DEPRECATED_SCHEMA_VERSIONS.discard(1)
        validator.SCHEMA_CHECKS.pop(2, None)


def test_per_version_dispatch_calls_correct_check(validator, tmp_path, monkeypatch):
    """Pin that SCHEMA_CHECKS[N] is the function called when a template
    declares template_schema_version=N. Without this, the dispatch could
    fire the wrong contract on a multi-version codebase."""
    called: list[int] = []
    validator.KNOWN_SCHEMA_VERSIONS.add(7)
    validator.SCHEMA_CHECKS[7] = lambda config: called.append(7)

    try:
        cfg = (
            "name: t\n"
            "runtime: claude-code\n"
            "template_schema_version: 7\n"
        )
        _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                     requirements=_good_requirements_txt())
        monkeypatch.chdir(tmp_path)
        validator.check_config_yaml()
        assert called == [7], f"v7 dispatch was not invoked; called={called}"
    finally:
        validator.KNOWN_SCHEMA_VERSIONS.discard(7)
        validator.SCHEMA_CHECKS.pop(7, None)


def test_runtime_not_installed_warns_not_errors(validator, tmp_path, monkeypatch):
    """If the validator runs in an env without molecule-ai-workspace-runtime,
    we WARN (loud) but don't error — hard-erroring would say 'your adapter
    is broken' when the actual issue is the CI infra. Mock the import to
    simulate this regardless of what's installed locally."""
    adapter = (
        "from molecule_runtime.adapters.base import BaseAdapter\n"
        "class A(BaseAdapter): pass\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)

    # Force the runtime import to fail by hiding the module.
    import sys
    saved = {k: sys.modules.pop(k) for k in list(sys.modules)
             if k.startswith("molecule_runtime")}
    saved_meta = sys.meta_path[:]
    class _Block:
        def find_spec(self, name, path=None, target=None):
            if name == "molecule_runtime" or name.startswith("molecule_runtime."):
                raise ImportError(f"blocked for test: {name}")
            return None
    sys.meta_path.insert(0, _Block())
    try:
        validator.check_adapter_runtime_load()
    finally:
        sys.meta_path[:] = saved_meta
        sys.modules.update(saved)

    assert validator.ERRORS == [], validator.ERRORS
    assert any(
        "skipping runtime-load check" in w
        for w in validator.WARNINGS
    ), validator.WARNINGS


# ──────────────────────────────── platform-model SSOT drift gate

def _manifest_fixture() -> str:
    """Minimal controlplane providers manifest: only the runtimes block the
    drift gate reads."""
    return (
        "schema_version: 1\n"
        "runtimes:\n"
        "  hermes:\n"
        "    providers:\n"
        "      - name: kimi-coding\n"
        "        models: [kimi-coding/kimi-k2]\n"
        "      - name: platform\n"
        "        models: [moonshot/kimi-k2.6, moonshot/kimi-k2.5]\n"
    )


def _config_with_platform(runtime: str, platform_ids: list[str]) -> str:
    lines = [
        "name: t\n",
        f"runtime: {runtime}\n",
        "template_schema_version: 1\n",
        "runtime_config:\n",
        "  models:\n",
        "    - id: kimi-coding/kimi-k2\n",
        "      required_env: [KIMI_API_KEY]\n",
    ]
    for mid in platform_ids:
        lines += [f"    - id: {mid}\n", "      provider: platform\n", "      required_env: []\n"]
    return "".join(lines)


def _setup_drift(tmp_path, monkeypatch, config_yaml, manifest_text=None):
    (tmp_path / "config.yaml").write_text(config_yaml)
    if manifest_text is not None:
        mp = tmp_path / "manifest.yaml"
        mp.write_text(manifest_text)
        monkeypatch.setenv("PROVIDERS_MANIFEST_FILE", str(mp))
    monkeypatch.chdir(tmp_path)


def test_platform_models_subset_passes(validator, tmp_path, monkeypatch):
    _setup_drift(tmp_path, monkeypatch,
                 _config_with_platform("hermes", ["moonshot/kimi-k2.6"]),
                 _manifest_fixture())
    validator.check_platform_models()
    assert validator.ERRORS == [], validator.ERRORS
    assert validator.WARNINGS == [], validator.WARNINGS


def test_platform_model_not_in_manifest_errors(validator, tmp_path, monkeypatch):
    _setup_drift(tmp_path, monkeypatch,
                 _config_with_platform("hermes", ["moonshot/kimi-k2.6", "moonshot/kimi-k2.99"]),
                 _manifest_fixture())
    validator.check_platform_models()
    assert any("kimi-k2.99" in e for e in validator.ERRORS), validator.ERRORS


def test_no_platform_models_skips(validator, tmp_path, monkeypatch):
    cfg = (
        "name: t\nruntime: hermes\ntemplate_schema_version: 1\n"
        "runtime_config:\n  models:\n    - id: kimi-coding/kimi-k2\n      required_env: [KIMI_API_KEY]\n"
    )
    _setup_drift(tmp_path, monkeypatch, cfg, _manifest_fixture())
    validator.check_platform_models()
    assert validator.ERRORS == []
    assert validator.WARNINGS == []


def test_manifest_unreachable_warns_not_errors(validator, tmp_path, monkeypatch):
    _setup_drift(tmp_path, monkeypatch,
                 _config_with_platform("hermes", ["moonshot/kimi-k2.6"]))
    # Point at a path that does not exist -> fetch returns None -> warn-skip.
    monkeypatch.setenv("PROVIDERS_MANIFEST_FILE", str(tmp_path / "nope.yaml"))
    validator.check_platform_models()
    assert validator.ERRORS == [], validator.ERRORS
    assert any("drift check skipped" in w for w in validator.WARNINGS), validator.WARNINGS


def test_runtime_absent_from_manifest_warns(validator, tmp_path, monkeypatch):
    _setup_drift(tmp_path, monkeypatch,
                 _config_with_platform("mystery-runtime", ["moonshot/kimi-k2.6"]),
                 _manifest_fixture())
    validator.check_platform_models()
    assert validator.ERRORS == [], validator.ERRORS
    assert any("not in the controlplane providers manifest" in w for w in validator.WARNINGS), validator.WARNINGS


def test_manifest_fetch_via_real_git_clone(validator, tmp_path, monkeypatch):
    """Exercise the REAL blobless+sparse git clone fetch path (not the
    PROVIDERS_MANIFEST_FILE short-circuit) against a local file:// repo.
    Regression guard: the sparse-checkout must use a DIRECTORY (cone mode),
    not a file path — the file-path form silently failed -> WARN-skip, so the
    gate never blocked via the live path."""
    import os as _os
    import shutil as _shutil
    import subprocess as _sp

    if _shutil.which("git") is None:
        import pytest as _pytest
        _pytest.skip("git not available")

    # Build a source repo containing internal/providers/providers.yaml.
    src = tmp_path / "cp-src"
    (src / "internal" / "providers").mkdir(parents=True)
    (src / "internal" / "providers" / "providers.yaml").write_text(_manifest_fixture())
    (src / "README.md").write_text("root file so the sparse cone has a base\n")
    genv = {
        **_os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    _sp.run(["git", "init", "-q", "-b", "main", str(src)], check=True, capture_output=True)
    _sp.run(["git", "-C", str(src), "add", "-A"], check=True, capture_output=True, env=genv)
    _sp.run(["git", "-C", str(src), "commit", "-q", "-m", "init"], check=True, capture_output=True, env=genv)

    # The template under validation (cwd) — hermes offering an in-manifest model.
    tmpl = tmp_path / "tmpl"
    tmpl.mkdir()
    (tmpl / "config.yaml").write_text(_config_with_platform("hermes", ["moonshot/kimi-k2.6"]))
    monkeypatch.chdir(tmpl)
    monkeypatch.delenv("PROVIDERS_MANIFEST_FILE", raising=False)
    # file:// enables --filter on a local clone (plain paths ignore it).
    monkeypatch.setenv("PROVIDERS_MANIFEST_REPO", "file://" + str(src))

    validator.check_platform_models()
    # Fetch succeeded via the clone path -> subset holds -> no error, no skip-warn.
    assert validator.ERRORS == [], validator.ERRORS
    assert validator.WARNINGS == [], validator.WARNINGS


def test_real_git_clone_detects_drift(validator, tmp_path, monkeypatch):
    """Same real-clone path, but the template offers a platform model NOT in
    the fetched manifest -> must err (proves the live path actually gates)."""
    import os as _os
    import shutil as _shutil
    import subprocess as _sp

    if _shutil.which("git") is None:
        import pytest as _pytest
        _pytest.skip("git not available")

    src = tmp_path / "cp-src2"
    (src / "internal" / "providers").mkdir(parents=True)
    (src / "internal" / "providers" / "providers.yaml").write_text(_manifest_fixture())
    (src / "README.md").write_text("x\n")
    genv = {
        **_os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    _sp.run(["git", "init", "-q", "-b", "main", str(src)], check=True, capture_output=True)
    _sp.run(["git", "-C", str(src), "add", "-A"], check=True, capture_output=True, env=genv)
    _sp.run(["git", "-C", str(src), "commit", "-q", "-m", "init"], check=True, capture_output=True, env=genv)

    tmpl = tmp_path / "tmpl2"
    tmpl.mkdir()
    (tmpl / "config.yaml").write_text(_config_with_platform("hermes", ["moonshot/kimi-k2.99"]))
    monkeypatch.chdir(tmpl)
    monkeypatch.delenv("PROVIDERS_MANIFEST_FILE", raising=False)
    monkeypatch.setenv("PROVIDERS_MANIFEST_REPO", "file://" + str(src))

    validator.check_platform_models()
    assert any("kimi-k2.99" in e for e in validator.ERRORS), validator.ERRORS


# ──────────── internal#718 P4 PR-3: full-providers + native-set drift gate ────────────
#
# check_full_providers_block extends the platform-only gate to ALL
# (provider, model) pairs in runtime_config.models. It fails closed on:
#   (a) a `provider:` ref that is NOT in the runtime's manifest native set
#   (b) a model id NOT in that native provider's manifest model set
# and fails OPEN on:
#   - templates without runtime_config.models (legacy top-level `model:` shape)
#   - models without an explicit `provider:` (adapter infers; out of scope)
#   - runtimes absent from the manifest (federation-friendly)
#   - manifest fetch failures (best-effort)


def _full_manifest_fixture() -> str:
    """Richer fixture covering multi-provider per runtime (claude-code) so the
    full-providers gate can be exercised across the (a)+(b) failure modes."""
    return (
        "schema_version: 1\n"
        "runtimes:\n"
        "  claude-code:\n"
        "    providers:\n"
        "      - name: anthropic-api\n"
        "        models: [claude-opus-4-7, anthropic:claude-opus-4-7]\n"
        "      - name: kimi-coding\n"
        "        models: [kimi-for-coding, moonshot:kimi-k2.6]\n"
        "      - name: platform\n"
        "        models: [anthropic/claude-opus-4-7, moonshot/kimi-k2.6]\n"
        "  hermes:\n"
        "    providers:\n"
        "      - name: kimi-coding\n"
        "        models: [kimi-coding/kimi-k2]\n"
        "      - name: platform\n"
        "        models: [moonshot/kimi-k2.6]\n"
    )


def _config_with_providers(runtime: str, entries) -> str:
    """entries = [(model_id, provider_name), ...]"""
    lines = [
        "name: t\n",
        f"runtime: {runtime}\n",
        "template_schema_version: 1\n",
        "runtime_config:\n",
        "  models:\n",
    ]
    for mid, prov in entries:
        lines += [f"    - id: {mid}\n"]
        if prov:
            lines += [f"      provider: {prov}\n"]
        lines += ["      required_env: []\n"]
    return "".join(lines)


def test_full_providers_subset_passes_no_drift(validator, tmp_path, monkeypatch):
    cfg = _config_with_providers("claude-code", [
        ("claude-opus-4-7", "anthropic-api"),
        ("anthropic:claude-opus-4-7", "anthropic-api"),
        ("kimi-for-coding", "kimi-coding"),
        ("anthropic/claude-opus-4-7", "platform"),
    ])
    _setup_drift(tmp_path, monkeypatch, cfg, _full_manifest_fixture())
    validator.check_full_providers_block()
    assert validator.ERRORS == [], validator.ERRORS
    assert validator.WARNINGS == [], validator.WARNINGS


def test_full_providers_unknown_provider_errors(validator, tmp_path, monkeypatch):
    cfg = _config_with_providers("claude-code", [
        ("nousresearch/hermes-4-70b", "nousresearch"),
    ])
    _setup_drift(tmp_path, monkeypatch, cfg, _full_manifest_fixture())
    validator.check_full_providers_block()
    assert any("nousresearch" in e and "NATIVE provider set" in e for e in validator.ERRORS), validator.ERRORS


def test_full_providers_native_provider_unknown_model_errors(validator, tmp_path, monkeypatch):
    cfg = _config_with_providers("claude-code", [
        ("claude-opus-99", "anthropic-api"),
    ])
    _setup_drift(tmp_path, monkeypatch, cfg, _full_manifest_fixture())
    validator.check_full_providers_block()
    assert any("claude-opus-99" in e and "native model set" in e for e in validator.ERRORS), validator.ERRORS


def test_full_providers_no_models_block_skips(validator, tmp_path, monkeypatch):
    cfg = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "model: claude-opus-4-7\n"
    )
    _setup_drift(tmp_path, monkeypatch, cfg, _full_manifest_fixture())
    validator.check_full_providers_block()
    assert validator.ERRORS == [], validator.ERRORS
    assert validator.WARNINGS == [], validator.WARNINGS


def test_full_providers_unknown_runtime_warns(validator, tmp_path, monkeypatch):
    cfg = _config_with_providers("federated-runtime", [
        ("any-model", "any-provider"),
    ])
    _setup_drift(tmp_path, monkeypatch, cfg, _full_manifest_fixture())
    validator.check_full_providers_block()
    assert validator.ERRORS == [], validator.ERRORS
    assert any("not in the controlplane providers manifest" in w for w in validator.WARNINGS), validator.WARNINGS


def test_full_providers_manifest_unreachable_warns(validator, tmp_path, monkeypatch):
    cfg = _config_with_providers("claude-code", [
        ("claude-opus-4-7", "anthropic-api"),
    ])
    _setup_drift(tmp_path, monkeypatch, cfg)  # no manifest_text
    monkeypatch.setenv("PROVIDERS_MANIFEST_FILE", str(tmp_path / "nope.yaml"))
    validator.check_full_providers_block()
    assert validator.ERRORS == [], validator.ERRORS
    assert any("drift check skipped" in w for w in validator.WARNINGS), validator.WARNINGS


def test_full_providers_blank_provider_ignored(validator, tmp_path, monkeypatch):
    cfg = _config_with_providers("claude-code", [
        ("some-id-no-provider", ""),
    ])
    _setup_drift(tmp_path, monkeypatch, cfg, _full_manifest_fixture())
    validator.check_full_providers_block()
    assert validator.ERRORS == [], validator.ERRORS


# ──────────── SSOT-inheritance enforcement gate (--official) ────────────
#
# check_no_hardcoded_provider_model(official, allow_self_model) is the
# principal's "the OFFICIAL repo must ENFORCE the SSOT, not just convention"
# gate. Under --official it ERRORs when a template config.yaml hardcodes the
# DEFAULT provider/model or pins the Molecule platform LLM proxy base_url —
# all of which the controlplane resolves/injects at provision from the
# env-derived LLM-mode SSOT (llm_mode.go) + providers.yaml registry. It is
# OFF without --official (community + un-migrated repos unaffected), and
# --allow-self-model exempts ONLY the top-level `model:` (platform-agent,
# core#2594).

def _silent_official_config() -> str:
    """A silent / inheriting official template: no top-level model, no
    runtime_config.model/provider, no proxy base_url pin. A user-selectable
    model catalog is allowed (it is not a default pin)."""
    return (
        "name: t\n"
        "runtime: claude-code\n"
        "template_schema_version: 1\n"
        "runtime_config:\n"
        "  models:\n"
        "    - id: moonshot/kimi-k2.6\n"
        "      name: Kimi K2.6\n"
        "      required_env: []\n"
        "  required_env: []\n"
        "  timeout: 0\n"
    )


def _repinned_official_config() -> str:
    """A re-pinned official template: hardcodes top-level model, the default
    runtime_config.model + a VENDOR provider, and the platform proxy base_url.
    (runtime_config.provider is a VENDOR name — `platform` is the allowed proxy
    route, so only a vendor pin trips the default-provider class.)"""
    return (
        "name: t\n"
        "runtime: claude-code\n"
        "template_schema_version: 1\n"
        "model: moonshot/kimi-k2.6\n"
        "providers:\n"
        "  - name: platform\n"
        "    auth_mode: third_party_anthropic_compat\n"
        "    model_prefixes: [moonshot/]\n"
        "    base_url: https://api.moleculesai.app/api/v1/internal/llm/anthropic\n"
        "    auth_env: [MOLECULE_LLM_USAGE_TOKEN]\n"
        "runtime_config:\n"
        "  model: moonshot/kimi-k2.6\n"
        "  provider: moonshot\n"
        "  models:\n"
        "    - id: moonshot/kimi-k2.6\n"
        "      name: Kimi K2.6\n"
        "      required_env: []\n"
        "  required_env: []\n"
        "  timeout: 0\n"
    )


def test_official_off_by_default_does_not_fire(validator, tmp_path, monkeypatch):
    """Without --official (official=False) the gate is a no-op even on a fully
    re-pinned config — community + un-migrated repos stay green."""
    _materialise(tmp_path, config_yaml=_repinned_official_config())
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=False, allow_self_model=False)
    assert validator.ERRORS == [], validator.ERRORS


def test_official_silent_template_passes(validator, tmp_path, monkeypatch):
    """A silent / inheriting official template passes under --official."""
    _materialise(tmp_path, config_yaml=_silent_official_config())
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=False)
    assert validator.ERRORS == [], validator.ERRORS


def test_official_top_level_model_pin_errors(validator, tmp_path, monkeypatch):
    cfg = _silent_official_config() + "model: moonshot/kimi-k2.6\n"
    _materialise(tmp_path, config_yaml=cfg)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=False)
    assert any("top-level" in e and "`model:`" in e for e in validator.ERRORS), validator.ERRORS


def test_official_runtime_config_model_pin_errors(validator, tmp_path, monkeypatch):
    cfg = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "runtime_config:\n  model: moonshot/kimi-k2.6\n  required_env: []\n"
    )
    _materialise(tmp_path, config_yaml=cfg)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=False)
    assert any("runtime_config.model" in e for e in validator.ERRORS), validator.ERRORS


def test_official_runtime_config_provider_pin_errors(validator, tmp_path, monkeypatch):
    """A VENDOR `runtime_config.provider` (minimax/moonshot/openai/...) is the
    drift the gate catches — provider selection is the CP's, not the template's."""
    cfg = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "runtime_config:\n  provider: minimax\n  required_env: []\n"
    )
    _materialise(tmp_path, config_yaml=cfg)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=False)
    assert any("runtime_config.provider" in e and "VENDOR" in e for e in validator.ERRORS), validator.ERRORS


def test_official_provider_platform_route_gated_without_flag(validator, tmp_path, monkeypatch):
    """`runtime_config.provider: platform` is still GATED for a generic official
    template (it forces the platform route even where the CP would pick byok).
    Only the platform-agent opts out via --allow-platform-route."""
    cfg = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "runtime_config:\n  provider: platform\n  models: []\n  required_env: []\n"
    )
    _materialise(tmp_path, config_yaml=cfg)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=False)
    assert any("runtime_config.provider" in e for e in validator.ERRORS), validator.ERRORS


def test_official_allow_platform_route_exempts_provider_platform(validator, tmp_path, monkeypatch):
    """--allow-platform-route exempts the platform-agent (Org Concierge) CP-proxy
    ROUTE: `runtime_config.provider: platform` is the route, NOT a vendor pin
    (principal Rule #13). The de-pinned platform-agent config keeps it deliberately."""
    cfg = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "runtime_config:\n  provider: platform\n  models: []\n  required_env: []\n"
    )
    _materialise(tmp_path, config_yaml=cfg)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(
        official=True, allow_self_model=False, allow_platform_route=True)
    assert validator.ERRORS == [], validator.ERRORS


def test_official_allow_platform_route_still_flags_vendor_provider(validator, tmp_path, monkeypatch):
    """--allow-platform-route exempts ONLY `platform`; a VENDOR provider pin is
    still the drift and is flagged even with the flag."""
    cfg = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "runtime_config:\n  provider: minimax\n  required_env: []\n"
    )
    _materialise(tmp_path, config_yaml=cfg)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(
        official=True, allow_self_model=False, allow_platform_route=True)
    assert any("runtime_config.provider" in e and "VENDOR" in e for e in validator.ERRORS), validator.ERRORS


def test_official_allow_platform_route_exempts_platform_proxy_base_url(validator, tmp_path, monkeypatch):
    """--allow-platform-route also exempts a `platform`-named provider entry's
    Molecule-proxy base_url (the platform-agent's structural CP-proxy registry),
    while still flagging a proxy base_url on a NON-platform entry."""
    # platform-named proxy entry → exempt under the flag
    cfg_ok = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "providers:\n"
        "  - name: platform\n"
        "    base_url: https://api.moleculesai.app/api/v1/internal/llm/anthropic\n"
        "    auth_env: [MOLECULE_LLM_USAGE_TOKEN]\n"
    )
    _materialise(tmp_path, config_yaml=cfg_ok)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(
        official=True, allow_self_model=False, allow_platform_route=True)
    assert validator.ERRORS == [], validator.ERRORS

    # same proxy path on a NON-platform-named entry → still flagged
    validator.ERRORS.clear()
    cfg_bad = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "providers:\n"
        "  - name: sneaky\n"
        "    base_url: https://api.moleculesai.app/api/v1/internal/llm/anthropic\n"
    )
    _materialise(tmp_path, config_yaml=cfg_bad)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(
        official=True, allow_self_model=False, allow_platform_route=True)
    assert any("proxy base_url" in e for e in validator.ERRORS), validator.ERRORS


def test_official_platform_proxy_base_url_pin_errors(validator, tmp_path, monkeypatch):
    """A providers[] entry hardcoding the platform LLM proxy base_url
    (contains the `internal/llm/` path) must error — that endpoint is injected
    by the CP (PlatformLLMProxyEnv), not pinned per template."""
    cfg = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "providers:\n"
        "  - name: platform\n"
        "    auth_mode: third_party_anthropic_compat\n"
        "    base_url: https://api.moleculesai.app/api/v1/internal/llm/anthropic\n"
        "    auth_env: [MOLECULE_LLM_USAGE_TOKEN]\n"
    )
    _materialise(tmp_path, config_yaml=cfg)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=False)
    assert any("proxy base_url" in e and "internal/llm" in e.lower()
               for e in validator.ERRORS), validator.ERRORS


def test_official_proxy_pin_under_runtime_config_providers_errors(validator, tmp_path, monkeypatch):
    """The proxy-pin scan also covers a registry inlined under
    runtime_config.providers when it carries dict entries with base_url."""
    cfg = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "runtime_config:\n"
        "  providers:\n"
        "    - name: platform\n"
        "      base_url: https://api.moleculesai.app/api/v1/internal/llm/openai/v1\n"
        "  required_env: []\n"
    )
    _materialise(tmp_path, config_yaml=cfg)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=False)
    assert any("proxy base_url" in e for e in validator.ERRORS), validator.ERRORS


def test_official_third_party_base_url_not_flagged(validator, tmp_path, monkeypatch):
    """A non-platform third-party base_url (api.kimi.com, api.minimax.io) is
    NOT the platform proxy and must not be false-flagged as a proxy pin."""
    cfg = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "providers:\n"
        "  - name: kimi-coding\n"
        "    base_url: https://api.kimi.com/coding/\n"
        "  - name: minimax\n"
        "    base_url: https://api.minimax.io/anthropic\n"
    )
    _materialise(tmp_path, config_yaml=cfg)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=False)
    assert validator.ERRORS == [], validator.ERRORS


def test_official_full_repin_errors_on_all_four(validator, tmp_path, monkeypatch):
    """The fully re-pinned official config trips all four error classes."""
    _materialise(tmp_path, config_yaml=_repinned_official_config())
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=False)
    joined = " || ".join(validator.ERRORS)
    assert "top-level" in joined and "`model:`" in joined, validator.ERRORS
    assert "runtime_config.model" in joined, validator.ERRORS
    assert "runtime_config.provider" in joined, validator.ERRORS
    assert "proxy base_url" in joined, validator.ERRORS


def test_official_allow_self_model_exempts_top_level_only(validator, tmp_path, monkeypatch):
    """--allow-self-model (platform-agent, core#2594) exempts ONLY the
    top-level `model:`. A top-level-model-only config passes; but the other
    pins are still flagged even with the exemption."""
    # top-level model only -> exempt -> passes
    cfg_ok = _silent_official_config() + "model: moonshot/kimi-k2.6\n"
    _materialise(tmp_path, config_yaml=cfg_ok)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=True)
    assert validator.ERRORS == [], validator.ERRORS

    # but a runtime_config.model pin still errors even with the exemption
    validator.ERRORS.clear()
    cfg_bad = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "model: moonshot/kimi-k2.6\n"
        "runtime_config:\n  model: moonshot/kimi-k2.6\n  required_env: []\n"
    )
    p2 = tmp_path / "bad"
    p2.mkdir()
    (p2 / "config.yaml").write_text(cfg_bad)
    monkeypatch.chdir(p2)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=True)
    assert any("runtime_config.model" in e for e in validator.ERRORS), validator.ERRORS
    assert not any("top-level" in e for e in validator.ERRORS), validator.ERRORS


def test_official_empty_model_value_not_flagged(validator, tmp_path, monkeypatch):
    """A `model:` key present but blank/None is 'unset', not a pin."""
    cfg = (
        "name: t\nruntime: claude-code\ntemplate_schema_version: 1\n"
        "model:\n"  # None value
    )
    _materialise(tmp_path, config_yaml=cfg)
    monkeypatch.chdir(tmp_path)
    validator.check_no_hardcoded_provider_model(official=True, allow_self_model=False)
    assert validator.ERRORS == [], validator.ERRORS


# ── End-to-end CLI: the real validator process against committed fixtures ──
#
# These exercise the actual `python3 validate-workspace-template.py
# --official --static-only` entrypoint (real argv parse, real exit code)
# against scripts/fixtures/official-{inherit,repinned}/ — the strongest proof
# that "the lint reds on a re-pinned official template fixture" and passes on a
# silent one. --static-only so the run needs neither the runtime wheel nor a
# Docker daemon.

import subprocess  # noqa: E402
import sys as _sys  # noqa: E402

_FIXTURES = VALIDATOR_PATH.parent / "fixtures"


def _run_validator_cli(fixture_dir, *flags):
    # sys.executable, not a bare "python3": portable across the CI runner
    # (ubuntu python3) and a Windows dev box (where `python3` is a store stub).
    import os as _os
    env = dict(_os.environ)
    # Hermetic + fast + cross-platform:
    #  - PYTHONUTF8/PYTHONIOENCODING: the validator prints a "✓" (U+2713);
    #    a Windows cp1252 stdout would UnicodeEncodeError on it. Force UTF-8.
    #  - PROVIDERS_MANIFEST_FILE -> a nonexistent path so check_full_providers_block
    #    short-circuits to a warn-skip instead of attempting a 60s network clone
    #    of the (private) controlplane repo. This isolates the test to the
    #    --official SSOT-inheritance gate under exercise.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PROVIDERS_MANIFEST_FILE"] = str(_FIXTURES / "_no_such_manifest.yaml")
    return subprocess.run(
        [_sys.executable, str(VALIDATOR_PATH), "--static-only", *flags],
        cwd=str(_FIXTURES / fixture_dir),
        capture_output=True, text=True, env=env,
    )


def test_official_lint_cli_reds_on_repinned_fixture():
    r = _run_validator_cli("official-repinned", "--official")
    assert r.returncode == 1, f"expected red exit, got {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "::error::" in r.stdout
    assert "model" in r.stdout and "proxy base_url" in r.stdout, r.stdout


def test_official_lint_cli_passes_on_inherit_fixture():
    r = _run_validator_cli("official-inherit", "--official")
    assert r.returncode == 0, f"expected green exit, got {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "official SSOT-inheritance" in r.stdout, r.stdout


def test_official_lint_cli_repinned_passes_without_official_flag():
    """The same re-pinned fixture is GREEN without --official — the gate is
    strictly opt-in, so it can never break a community / un-migrated repo."""
    r = _run_validator_cli("official-repinned")
    assert r.returncode == 0, f"expected green without --official, got {r.returncode}\n{r.stdout}\n{r.stderr}"


def test_ssot_inheritance_only_passes_config_overlay_platform_route():
    """--ssot-inheritance-only runs JUST the model/provider/proxy gate, skipping
    the Dockerfile/adapter/template_schema_version structural checks. The
    config-overlay platform-agent (no Dockerfile/adapter) passes with
    --allow-platform-route — this is exactly how the concierge template's CI gates
    a re-pin without adopting the full runtime-template contract."""
    r = _run_validator_cli("official-platform-route", "--ssot-inheritance-only", "--allow-platform-route")
    assert r.returncode == 0, f"expected green, got {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "SSOT-inheritance gate passed" in r.stdout, r.stdout


def test_ssot_inheritance_only_gates_model_repin(tmp_path):
    """--ssot-inheritance-only still REDs a re-introduced model pin (even with
    --allow-platform-route) — the config-overlay re-pin gate."""
    import shutil, os as _os
    repin = tmp_path / "overlay-repinned"
    shutil.copytree(_FIXTURES / "official-platform-route", repin)
    cfg = repin / "config.yaml"
    cfg.write_text(cfg.read_text() + "\nmodel: minimax/MiniMax-M2.7\n")
    env = dict(_os.environ); env["PYTHONUTF8"] = "1"; env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [_sys.executable, str(VALIDATOR_PATH), "--ssot-inheritance-only", "--allow-platform-route"],
        cwd=str(repin), capture_output=True, text=True, env=env,
    )
    assert r.returncode == 1, f"expected red on model re-pin, got {r.returncode}\n{r.stdout}"
    assert "`model:`" in r.stdout, r.stdout


def test_official_lint_cli_platform_route_passes_with_flag():
    """The REAL de-pinned platform-agent (Org Concierge) shape — no model pin but
    the platform CP-proxy route kept — PASSES `--official --allow-platform-route`.
    This is the proof that wiring the gate on the concierge template is green."""
    r = _run_validator_cli("official-platform-route", "--official", "--allow-platform-route")
    assert r.returncode == 0, f"expected green, got {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "official SSOT-inheritance" in r.stdout, r.stdout


def test_official_lint_cli_platform_route_reds_without_route_flag():
    """Without --allow-platform-route the concierge's platform route declarations
    are gated — proves the flag is load-bearing (not a silent always-pass)."""
    r = _run_validator_cli("official-platform-route", "--official")
    assert r.returncode == 1, f"expected red, got {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "runtime_config.provider" in r.stdout and "proxy base_url" in r.stdout, r.stdout


def test_official_lint_cli_gates_model_repin_on_platform_agent(tmp_path):
    """THE re-pin gate: a re-introduced model pin on the de-pinned platform-agent
    is RED even WITH --allow-platform-route (the flag exempts the platform route,
    never a model). This is the lint that gates a template model re-pin."""
    import shutil
    repin = tmp_path / "official-platform-route-repinned"
    shutil.copytree(_FIXTURES / "official-platform-route", repin)
    cfg = repin / "config.yaml"
    cfg.write_text(cfg.read_text() + "\n# RE-PIN (the violation this gate catches):\nmodel: minimax/MiniMax-M2.7\n")
    import os as _os
    env = dict(_os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PROVIDERS_MANIFEST_FILE"] = str(_FIXTURES / "_no_such_manifest.yaml")
    r = subprocess.run(
        [_sys.executable, str(VALIDATOR_PATH), "--static-only", "--official", "--allow-platform-route"],
        cwd=str(repin), capture_output=True, text=True, env=env,
    )
    assert r.returncode == 1, f"expected red on model re-pin, got {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "top-level" in r.stdout and "`model:`" in r.stdout, r.stdout
