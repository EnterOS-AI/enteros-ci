"""Tests for the final-image management-MCP Tier-4 verifier."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import signal
import sys
import time

import pytest


SCRIPT = Path(__file__).with_name("mcp_built_image_e2e.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("mcp_built_image_e2e_tested", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


built_image = _load_module()


def _python_child_env() -> dict[str, str]:
    """Keep only paths required to start this exact test interpreter."""

    env = {"PATH": os.environ.get("PATH", "")}
    for name in (
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
    ):
        if value := os.environ.get(name):
            env[name] = value
    return env


def _attestation_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "checker_sentinel": "mcp-pin-lockstep:sentinel:executed",
        "runtime": {
            "project": "molecules-workspace-runtime",
            "version": "0.4.35",
            "wheel_url": "https://git.moleculesai.app/runtime.whl",
            "wheel_sha256": "a" * 64,
            "prebake_helper_sha256": hashlib.sha256(b"helper\n").hexdigest(),
        },
        "management_mcp": {
            "package": "@molecule-ai/mcp-server",
            "pinned_version": "1.9.5",
            "compatible_range": "^1.8.0",
            "registry": "https://git.moleculesai.app/api/packages/molecule-ai/npm/",
            "registry_scope": "@molecule-ai",
            "required_tool": "provision_workspace",
            "artifact": {
                "packument_url": "https://git.moleculesai.app/packument",
                "tarball_url": "https://git.moleculesai.app/mcp.tgz",
                "integrity": "sha512-" + "A" * 88,
                "shasum": "b" * 40,
            },
        },
    }


def _attestation():
    return built_image.load_attestation(
        io.BytesIO(json.dumps(_attestation_payload()).encode())
    )


def _jsonrpc_output(
    *, tool: str = "provision_workspace", version: str | None = "1.0.0"
) -> bytes:
    server_info = {"name": "molecule-mcp-server"}
    if version is not None:
        server_info["version"] = version
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": server_info,
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"tools": [{"name": tool}]},
        },
    ]
    return ("\n".join(json.dumps(message) for message in messages) + "\n").encode()


def test_load_attestation_accepts_the_static_checker_contract() -> None:
    attestation = _attestation()

    assert attestation.runtime_version == "0.4.35"
    assert attestation.helper_sha256 == hashlib.sha256(b"helper\n").hexdigest()
    assert attestation.package == "@molecule-ai/mcp-server"
    assert attestation.pinned_version == "1.9.5"
    assert attestation.compatible_range == "^1.8.0"
    assert attestation.required_tool == "provision_workspace"


def test_load_attestation_rejects_duplicate_json_keys() -> None:
    raw = json.dumps(_attestation_payload()).replace(
        '"schema_version": 1,', '"schema_version": 1, "schema_version": 1,', 1
    )

    with pytest.raises(built_image.BuiltImageE2EError, match="duplicate JSON key"):
        built_image.load_attestation(io.BytesIO(raw.encode()))


def test_load_attestation_rejects_oversized_input_without_echoing_it(
    monkeypatch,
) -> None:
    marker = b"credential=must-not-log"
    monkeypatch.setattr(built_image, "MAX_ATTESTATION_BYTES", 32)

    with pytest.raises(built_image.BuiltImageE2EError) as caught:
        built_image.load_attestation(io.BytesIO(marker * 4))

    assert "too large" in str(caught.value)
    assert marker.decode() not in str(caught.value)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schema_version=2), "schema"),
        (
            lambda value: value.update(checker_sentinel="wrong"),
            "sentinel",
        ),
        (
            lambda value: value["runtime"].update(version="latest"),
            "runtime version",
        ),
        (
            lambda value: value["management_mcp"].update(
                compatible_range="credential=must-not-log"
            ),
            "compatible range",
        ),
        (
            lambda value: value["management_mcp"].update(pinned_version="2.0.0"),
            "compatible range",
        ),
        (
            lambda value: value["management_mcp"].update(
                package="@molecule-ai/mcp-server;touch-pwned"
            ),
            "package",
        ),
    ],
)
def test_load_attestation_rejects_malformed_contract(mutation, message) -> None:
    payload = _attestation_payload()
    mutation(payload)

    with pytest.raises(built_image.BuiltImageE2EError, match=message) as caught:
        built_image.load_attestation(io.BytesIO(json.dumps(payload).encode()))

    assert "must-not-log" not in str(caught.value)


def test_load_attestation_rejects_unversioned_shape_drift() -> None:
    payload = _attestation_payload()
    payload["runtime"]["unexpected"] = "new field"

    with pytest.raises(built_image.BuiltImageE2EError, match="runtime fields"):
        built_image.load_attestation(io.BytesIO(json.dumps(payload).encode()))


def test_load_attestation_rejects_boolean_schema_version() -> None:
    payload = _attestation_payload()
    payload["schema_version"] = True

    with pytest.raises(built_image.BuiltImageE2EError, match="schema"):
        built_image.load_attestation(io.BytesIO(json.dumps(payload).encode()))


class _FakeDistribution:
    def __init__(self, root: Path, version: str = "0.4.35") -> None:
        self.root = root
        self.version = version
        self.metadata = {"Name": "molecules-workspace-runtime"}
        self.files = [
            PurePosixPath("molecule_runtime/__init__.py"),
            PurePosixPath("molecule_runtime/platform_agent_identity.py"),
            PurePosixPath("molecule_runtime/scripts/prebake-mgmt-mcp.sh"),
        ]

    def locate_file(self, entry: PurePosixPath) -> Path:
        return self.root / str(entry)


def _installed_fixture(tmp_path: Path, monkeypatch, *, version: str = "0.4.35"):
    package = tmp_path / "molecule_runtime"
    scripts = package / "scripts"
    scripts.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("")
    identity_path = package / "platform_agent_identity.py"
    identity_path.write_text("# fixture\n")
    scripts.joinpath("prebake-mgmt-mcp.sh").write_bytes(b"helper\n")

    distribution = _FakeDistribution(tmp_path, version=version)
    monkeypatch.setattr(
        built_image.metadata,
        "distribution",
        lambda _name: distribution,
    )
    return distribution


def _import_probe_output(
    tmp_path: Path,
    *,
    runtime_origin: Path | None = None,
    identity_origin: Path | None = None,
    required_tool: str = "provision_workspace",
) -> str:
    package = tmp_path / "molecule_runtime"
    return json.dumps(
        {
            "runtime_origin": str(runtime_origin or package / "__init__.py"),
            "identity_origin": str(
                identity_origin or package / "platform_agent_identity.py"
            ),
            "constants": {
                "MANAGEMENT_MCP_NPM_PACKAGE": "@molecule-ai/mcp-server",
                "MANAGEMENT_MCP_PINNED_VERSION": "1.9.5",
                "MANAGEMENT_MCP_COMPATIBLE_RANGE": "^1.8.0",
                "MANAGEMENT_MCP_REGISTRY": (
                    "https://git.moleculesai.app/api/packages/molecule-ai/npm/"
                ),
                "MANAGEMENT_MCP_REGISTRY_SCOPE": "@molecule-ai",
                "REQUIRED_TOOL": required_tool,
            },
        }
    )


def _import_probe_runner(
    tmp_path: Path,
    *,
    runtime_origin: Path | None = None,
    identity_origin: Path | None = None,
    required_tool: str = "provision_workspace",
):
    def fake_run(_argv, **_kwargs):
        return built_image.ProcessResult(
            returncode=0,
            stdout=_import_probe_output(
                tmp_path,
                runtime_origin=runtime_origin,
                identity_origin=identity_origin,
                required_tool=required_tool,
            ),
            stderr="",
        )

    return fake_run


def test_installed_runtime_matches_distribution_origin_helper_and_constants(
    tmp_path, monkeypatch
) -> None:
    _installed_fixture(tmp_path, monkeypatch)

    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return built_image.ProcessResult(
            returncode=0,
            stdout=_import_probe_output(tmp_path),
            stderr="",
        )

    built_image.verify_installed_runtime(_attestation(), run_process=fake_run)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:2] == [sys.executable, "-I"]
    assert kwargs["timeout_seconds"] == built_image.PROCESS_TIMEOUT_SECONDS
    assert kwargs["max_output_bytes"] == built_image.MAX_PROCESS_OUTPUT_BYTES
    assert "PYTHONPATH" not in kwargs["env"]


def test_installed_runtime_redacts_failed_import_child_output(
    tmp_path, monkeypatch
) -> None:
    _installed_fixture(tmp_path, monkeypatch)
    marker = "credential=must-not-log"

    def fake_run(_argv, **_kwargs):
        return built_image.ProcessResult(
            returncode=1,
            stdout=marker,
            stderr=marker,
        )

    with pytest.raises(built_image.BuiltImageE2EError) as caught:
        built_image.verify_installed_runtime(_attestation(), run_process=fake_run)

    assert marker not in str(caught.value)


def test_installed_runtime_rejects_distribution_version_drift(
    tmp_path, monkeypatch
) -> None:
    _installed_fixture(tmp_path, monkeypatch, version="0.4.34")

    with pytest.raises(built_image.BuiltImageE2EError, match="version"):
        built_image.verify_installed_runtime(_attestation())


def test_installed_runtime_rejects_shadowed_import_origin(
    tmp_path, monkeypatch
) -> None:
    _installed_fixture(tmp_path, monkeypatch)
    shadow = tmp_path / "shadow" / "molecule_runtime" / "__init__.py"
    shadow.parent.mkdir(parents=True)
    shadow.write_text("")

    with pytest.raises(built_image.BuiltImageE2EError, match="import origin"):
        built_image.verify_installed_runtime(
            _attestation(),
            run_process=_import_probe_runner(tmp_path, runtime_origin=shadow),
        )


def test_installed_runtime_rejects_helper_digest_drift(tmp_path, monkeypatch) -> None:
    _installed_fixture(tmp_path, monkeypatch)
    (tmp_path / "molecule_runtime/scripts/prebake-mgmt-mcp.sh").write_bytes(
        b"different\n"
    )

    with pytest.raises(built_image.BuiltImageE2EError, match="helper digest"):
        built_image.verify_installed_runtime(
            _attestation(),
            run_process=_import_probe_runner(tmp_path),
        )


def test_installed_runtime_rejects_oversized_helper(tmp_path, monkeypatch) -> None:
    _installed_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(built_image, "MAX_INSTALLED_HELPER_BYTES", 4, raising=False)

    with pytest.raises(built_image.BuiltImageE2EError, match="helper is too large"):
        built_image.verify_installed_runtime(
            _attestation(),
            run_process=_import_probe_runner(tmp_path),
        )


def test_installed_runtime_rejects_executable_constant_drift(
    tmp_path, monkeypatch
) -> None:
    _installed_fixture(tmp_path, monkeypatch)

    with pytest.raises(built_image.BuiltImageE2EError, match="constants"):
        built_image.verify_installed_runtime(
            _attestation(),
            run_process=_import_probe_runner(
                tmp_path,
                required_tool="different_tool",
            ),
        )


def test_validate_jsonrpc_requires_initialize_and_management_tool() -> None:
    assert (
        built_image.validate_jsonrpc_output(
            _jsonrpc_output(),
            required_tool="provision_workspace",
        )
        == "1.0.0"
    )


def test_validate_jsonrpc_allows_server_info_without_version() -> None:
    assert (
        built_image.validate_jsonrpc_output(
            _jsonrpc_output(version=None),
            required_tool="provision_workspace",
        )
        is None
    )


def test_validate_jsonrpc_treats_server_info_version_as_opaque_protocol_metadata() -> (
    None
):
    assert (
        built_image.validate_jsonrpc_output(
            _jsonrpc_output(version="1.0.0"),
            required_tool="provision_workspace",
        )
        == "1.0.0"
    )


@pytest.mark.parametrize(
    "missing_field", ["protocolVersion", "capabilities", "serverInfo"]
)
def test_validate_jsonrpc_requires_initialize_result_shape(missing_field) -> None:
    messages = [json.loads(line) for line in _jsonrpc_output().splitlines()]
    del messages[0]["result"][missing_field]
    payload = ("\n".join(json.dumps(message) for message in messages) + "\n").encode()

    with pytest.raises(built_image.BuiltImageE2EError, match="MCP initialize"):
        built_image.validate_jsonrpc_output(
            payload,
            required_tool="provision_workspace",
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_jsonrpc_output(tool="other_tool"), "required tool"),
        (_jsonrpc_output(version=""), "serverInfo version"),
        (
            b'{"jsonrpc":"2.0","id":1,"result":{},"result":{}}\n',
            "duplicate JSON key",
        ),
        (
            b'{"jsonrpc":"2.0","id":1,"error":{"code":-1}}\n',
            "error response",
        ),
        (b"not-json\n", "malformed JSON"),
    ],
)
def test_validate_jsonrpc_fails_closed(payload, message) -> None:
    with pytest.raises(built_image.BuiltImageE2EError, match=message):
        built_image.validate_jsonrpc_output(
            payload,
            required_tool="provision_workspace",
        )


def test_validate_jsonrpc_rejects_boolean_response_ids() -> None:
    messages = [
        {"jsonrpc": "2.0", "id": True, "result": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"tools": [{"name": "provision_workspace"}]},
        },
    ]
    payload = ("\n".join(json.dumps(message) for message in messages) + "\n").encode()

    with pytest.raises(built_image.BuiltImageE2EError, match="response id"):
        built_image.validate_jsonrpc_output(
            payload,
            required_tool="provision_workspace",
        )


def test_python_child_env_preserves_required_loader_paths(monkeypatch) -> None:
    monkeypatch.setenv("LD_LIBRARY_PATH", "/runtime/lib")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/runtime/dyld")
    monkeypatch.setenv("DYLD_FALLBACK_LIBRARY_PATH", "/runtime/fallback")

    assert _python_child_env() == {
        "PATH": os.environ.get("PATH", ""),
        "LD_LIBRARY_PATH": "/runtime/lib",
        "DYLD_LIBRARY_PATH": "/runtime/dyld",
        "DYLD_FALLBACK_LIBRARY_PATH": "/runtime/fallback",
    }


def test_run_bounded_uses_argv_without_shell_interpretation(tmp_path) -> None:
    marker = tmp_path / "must-not-exist"
    literal = f"$(touch {marker})"
    code = "import json,sys; print(json.dumps(sys.argv[1:]))"

    result = built_image.run_bounded_process(
        [sys.executable, "-c", code, literal],
        input_bytes=b"",
        env=_python_child_env(),
        timeout_seconds=2,
        max_output_bytes=4096,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == [literal]
    assert not marker.exists()


def test_run_bounded_kills_a_timed_out_process_group() -> None:
    started = time.monotonic()

    with pytest.raises(built_image.BuiltImageE2EError, match="timed out"):
        built_image.run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            input_bytes=b"",
            env=_python_child_env(),
            timeout_seconds=0.1,
            max_output_bytes=4096,
        )

    assert time.monotonic() - started < 3


def test_terminate_process_group_kills_descendants_after_parent_exits(
    monkeypatch,
) -> None:
    class ParentThatExitsOnTerm:
        pid = 4242

        def wait(self, timeout):
            return 0

    delivered_signals = []

    def fake_killpg(process_group, delivered_signal):
        assert process_group == 4242
        delivered_signals.append(delivered_signal)

    monkeypatch.setattr(built_image.os, "killpg", fake_killpg)

    built_image._terminate_process_group(ParentThatExitsOnTerm())

    assert delivered_signals == [signal.SIGTERM, 0, signal.SIGKILL]


def test_run_bounded_cleans_the_process_group_after_success(monkeypatch) -> None:
    cleaned_processes = []
    monkeypatch.setattr(
        built_image,
        "_terminate_process_group",
        lambda process: cleaned_processes.append(process),
    )

    result = built_image.run_bounded_process(
        [sys.executable, "-c", "print('done')"],
        input_bytes=b"",
        env=_python_child_env(),
        timeout_seconds=2,
        max_output_bytes=4096,
    )

    assert result.returncode == 0
    assert len(cleaned_processes) == 1


def test_run_bounded_kills_a_process_that_exceeds_output_limit() -> None:
    with pytest.raises(built_image.BuiltImageE2EError, match="output limit"):
        built_image.run_bounded_process(
            [sys.executable, "-c", "print('x' * 10000)"],
            input_bytes=b"",
            env=_python_child_env(),
            timeout_seconds=2,
            max_output_bytes=1024,
        )


def test_offline_probes_cover_exact_and_range_under_both_homes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(built_image.tempfile, "tempdir", str(tmp_path))
    calls = []

    def fake_run(argv, *, input_bytes, env, timeout_seconds, max_output_bytes):
        calls.append((argv, input_bytes, dict(env), timeout_seconds, max_output_bytes))
        return built_image.ProcessResult(
            returncode=0,
            stdout=_jsonrpc_output().decode(),
            stderr="",
        )

    version_proof = built_image.verify_offline_mcp(
        _attestation(),
        npx_path="/opt/node/bin/npx",
        run_process=fake_run,
    )

    assert version_proof == "emitted-consistent"
    assert len(calls) == 4
    specs = [call[0][-1] for call in calls]
    assert specs == [
        "@molecule-ai/mcp-server@1.9.5",
        "@molecule-ai/mcp-server@^1.8.0",
        "@molecule-ai/mcp-server@1.9.5",
        "@molecule-ai/mcp-server@^1.8.0",
    ]
    homes = [call[2]["HOME"] for call in calls]
    assert homes[:2] == ["/home/agent", "/home/agent"]
    assert homes[2] == homes[3]
    assert homes[2] != "/home/agent"
    for argv, input_bytes, env, timeout_seconds, max_output_bytes in calls:
        assert argv[:4] == ["/opt/node/bin/npx", "-y", "--offline", argv[-1]]
        assert b'"method":"initialize"' in input_bytes
        assert b'"method":"notifications/initialized"' in input_bytes
        assert b'"method":"tools/list"' in input_bytes
        assert env["MOLECULE_MCP_MODE"] == "management"
        assert env["npm_config_cache"] == "/home/agent/.npm"
        assert env["NPM_CONFIG_USERCONFIG"] == "/home/agent/.npmrc"
        assert timeout_seconds == built_image.PROCESS_TIMEOUT_SECONDS
        assert max_output_bytes == built_image.MAX_PROCESS_OUTPUT_BYTES


def test_offline_probe_rejects_nonzero_server_exit() -> None:
    def fake_run(_argv, **_kwargs):
        return built_image.ProcessResult(
            returncode=1,
            stdout=_jsonrpc_output().decode(),
            stderr="credential=must-not-log",
        )

    with pytest.raises(built_image.BuiltImageE2EError) as caught:
        built_image.verify_offline_mcp(
            _attestation(),
            npx_path="/opt/node/bin/npx",
            run_process=fake_run,
        )

    assert "offline MCP launch failed" in str(caught.value)
    assert "must-not-log" not in str(caught.value)


def test_offline_probes_reject_inconsistent_emitted_server_info_versions() -> None:
    calls = 0

    def fake_run(_argv, **_kwargs):
        nonlocal calls
        calls += 1
        version = "1.0.0" if calls == 1 else "1.0.1"
        return built_image.ProcessResult(
            returncode=0,
            stdout=_jsonrpc_output(version=version).decode(),
            stderr="",
        )

    with pytest.raises(built_image.BuiltImageE2EError, match="serverInfo versions"):
        built_image.verify_offline_mcp(
            _attestation(),
            npx_path="/opt/node/bin/npx",
            run_process=fake_run,
        )


def test_offline_probes_reject_inconsistent_server_info_version_presence() -> None:
    calls = 0

    def fake_run(_argv, **_kwargs):
        nonlocal calls
        calls += 1
        version = "1.0.0" if calls == 1 else None
        return built_image.ProcessResult(
            returncode=0,
            stdout=_jsonrpc_output(version=version).decode(),
            stderr="",
        )

    with pytest.raises(built_image.BuiltImageE2EError, match="serverInfo versions"):
        built_image.verify_offline_mcp(
            _attestation(),
            npx_path="/opt/node/bin/npx",
            run_process=fake_run,
        )


def test_offline_probes_do_not_return_child_controlled_server_info_version() -> None:
    marker = "credential=must-not-log"

    def fake_run(_argv, **_kwargs):
        return built_image.ProcessResult(
            returncode=0,
            stdout=_jsonrpc_output(version=marker).decode(),
            stderr="",
        )

    version_proof = built_image.verify_offline_mcp(
        _attestation(),
        npx_path="/opt/node/bin/npx",
        run_process=fake_run,
    )

    assert version_proof == "emitted-consistent"
    assert marker not in version_proof


def test_find_npx_honors_the_sanctioned_node_bin_override(tmp_path) -> None:
    node_bin = tmp_path / "node-bin"
    node_bin.mkdir()
    npx = node_bin / "npx"
    npx.write_text("#!/bin/sh\nexit 0\n")
    npx.chmod(0o755)

    resolved, path = built_image.find_npx(
        {
            "MOLECULE_PREBAKE_NODE_BIN": str(node_bin),
            "PATH": "/usr/bin:/bin",
        }
    )

    assert resolved == str(npx)
    assert path.split(os.pathsep)[0] == str(node_bin)


def test_cli_emits_stable_sentinel_and_redacts_unexpected_errors(
    monkeypatch, capsys
) -> None:
    marker = "credential=must-not-log"
    attestation = _attestation()
    monkeypatch.setattr(built_image, "load_attestation", lambda _stream: attestation)
    monkeypatch.setattr(
        built_image,
        "verify_installed_runtime",
        lambda _attestation: (_ for _ in ()).throw(ValueError(marker)),
    )

    assert built_image.main() == 1
    captured = capsys.readouterr()
    assert built_image.SENTINEL in captured.out
    assert marker not in captured.out + captured.err
    assert "unexpected" in captured.err


def test_cli_success_includes_optional_server_info_version(monkeypatch, capsys) -> None:
    attestation = _attestation()
    monkeypatch.setattr(built_image, "load_attestation", lambda _stream: attestation)
    monkeypatch.setattr(built_image, "verify_installed_runtime", lambda _value: None)
    monkeypatch.setattr(
        built_image,
        "find_npx",
        lambda: ("/opt/node/bin/npx", "/opt/node/bin:/usr/bin"),
    )
    monkeypatch.setattr(
        built_image,
        "verify_offline_mcp",
        lambda *_args, **_kwargs: "emitted-consistent",
    )

    assert built_image.main() == 0
    captured = capsys.readouterr()
    assert built_image.SENTINEL in captured.out
    assert "serverInfo.version=emitted-consistent" in captured.out
    assert captured.err == ""
