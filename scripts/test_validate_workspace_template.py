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
