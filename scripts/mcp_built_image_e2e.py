#!/usr/bin/env python3
"""Verify the attested management MCP inside a final runtime-template image.

The caller supplies ``mcp_pin_lockstep.py --json`` output on stdin. This script
executes only installed image content, uses argv subprocesses (never a shell), and
expects the container itself to run with networking disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import BinaryIO, Callable
import urllib.parse


SENTINEL = "mcp-built-image-e2e:sentinel:executed"
STATIC_SENTINEL = "mcp-pin-lockstep:sentinel:executed"
MCP_PROTOCOL_VERSION = "2024-11-05"
RUNTIME_DISTRIBUTION = "molecules-workspace-runtime"
AGENT_HOME = "/home/agent"
AGENT_NPM_CACHE = f"{AGENT_HOME}/.npm"
AGENT_NPMRC = f"{AGENT_HOME}/.npmrc"
MAX_ATTESTATION_BYTES = 256 * 1024
MAX_INSTALLED_HELPER_BYTES = 2 * 1024 * 1024
MAX_PROCESS_OUTPUT_BYTES = 512 * 1024
PROCESS_TIMEOUT_SECONDS = 30
_MAX_FIELD_LENGTH = 4096
_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_NPM_SCOPE_RE = re.compile(r"^@[a-z0-9][a-z0-9._-]*$")
_NPM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_TOOL_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_RUNTIME_FILES = {
    "package": "molecule_runtime/__init__.py",
    "identity": "molecule_runtime/platform_agent_identity.py",
    "helper": "molecule_runtime/scripts/prebake-mgmt-mcp.sh",
}


class BuiltImageE2EError(Exception):
    """A fail-closed final-image contract violation."""


@dataclass(frozen=True)
class Attestation:
    runtime_version: str
    helper_sha256: str
    package: str
    pinned_version: str
    compatible_range: str
    registry: str
    registry_scope: str
    required_tool: str


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _strict_json_loads(payload: bytes, label: str):
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise BuiltImageE2EError(f"{label} is not UTF-8 JSON") from exc

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BuiltImageE2EError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        raise BuiltImageE2EError(f"{label} is malformed JSON") from exc


def _require_fields(value, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise BuiltImageE2EError(f"attestation {label} fields do not match schema")
    return value


def _require_string(
    value, label: str, *, pattern: re.Pattern[str] | None = None
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_FIELD_LENGTH
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise BuiltImageE2EError(f"attestation {label} is invalid")
    return value


def _semver(value: str, label: str) -> tuple[int, int, int]:
    if len(value) > 32:
        raise BuiltImageE2EError(f"attestation {label} is not a stable semver")
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        raise BuiltImageE2EError(f"attestation {label} is not a stable semver")
    return tuple(int(part) for part in match.groups())


def _caret_contains(compatible: str, pinned: str) -> bool:
    if not compatible.startswith("^"):
        raise BuiltImageE2EError("attestation compatible range is invalid")
    floor = _semver(compatible[1:], "compatible range")
    version = _semver(pinned, "pinned version")
    if floor[0] > 0:
        ceiling = (floor[0] + 1, 0, 0)
    elif floor[1] > 0:
        ceiling = (0, floor[1] + 1, 0)
    else:
        ceiling = (0, 0, floor[2] + 1)
    return floor <= version < ceiling


def _require_https_url(value, label: str) -> str:
    value = _require_string(value, label)
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BuiltImageE2EError(f"attestation {label} is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise BuiltImageE2EError(f"attestation {label} is invalid")
    return value


def load_attestation(stream: BinaryIO) -> Attestation:
    """Load and validate one bounded schema-v1 static attestation."""

    payload = stream.read(MAX_ATTESTATION_BYTES + 1)
    if len(payload) > MAX_ATTESTATION_BYTES:
        raise BuiltImageE2EError("static attestation is too large")
    root = _strict_json_loads(payload, "static attestation")
    root = _require_fields(
        root,
        {"schema_version", "checker_sentinel", "runtime", "management_mcp"},
        "root",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise BuiltImageE2EError("static attestation schema is unsupported")
    if root["checker_sentinel"] != STATIC_SENTINEL:
        raise BuiltImageE2EError("static attestation sentinel is missing")

    runtime = _require_fields(
        root["runtime"],
        {
            "project",
            "version",
            "wheel_url",
            "wheel_sha256",
            "prebake_helper_sha256",
        },
        "runtime",
    )
    if runtime["project"] != RUNTIME_DISTRIBUTION:
        raise BuiltImageE2EError("attestation runtime project is invalid")
    runtime_version = _require_string(runtime["version"], "runtime version")
    _semver(runtime_version, "runtime version")
    _require_https_url(runtime["wheel_url"], "runtime wheel URL")
    _require_string(runtime["wheel_sha256"], "runtime wheel digest", pattern=_SHA256_RE)
    helper_sha256 = _require_string(
        runtime["prebake_helper_sha256"],
        "helper digest",
        pattern=_SHA256_RE,
    )

    management = _require_fields(
        root["management_mcp"],
        {
            "package",
            "pinned_version",
            "compatible_range",
            "registry",
            "registry_scope",
            "required_tool",
            "artifact",
        },
        "management MCP",
    )
    package = _require_string(management["package"], "MCP package")
    scope = _require_string(management["registry_scope"], "MCP registry scope")
    if _NPM_SCOPE_RE.fullmatch(scope) is None:
        raise BuiltImageE2EError("attestation MCP registry scope is invalid")
    prefix = scope + "/"
    if (
        not package.startswith(prefix)
        or _NPM_NAME_RE.fullmatch(package.removeprefix(prefix)) is None
    ):
        raise BuiltImageE2EError("attestation MCP package is invalid")

    pinned = _require_string(management["pinned_version"], "pinned version")
    compatible = _require_string(management["compatible_range"], "compatible range")
    if not _caret_contains(compatible, pinned):
        raise BuiltImageE2EError(
            "attestation pinned version is outside compatible range"
        )
    registry = _require_https_url(management["registry"], "MCP registry")
    required_tool = _require_string(management["required_tool"], "required tool")
    if _TOOL_RE.fullmatch(required_tool) is None:
        raise BuiltImageE2EError("attestation required tool is invalid")

    artifact = _require_fields(
        management["artifact"],
        {"packument_url", "tarball_url", "integrity", "shasum"},
        "MCP artifact",
    )
    _require_https_url(artifact["packument_url"], "MCP packument URL")
    _require_https_url(artifact["tarball_url"], "MCP tarball URL")
    integrity = _require_string(artifact["integrity"], "MCP integrity")
    if not integrity.startswith("sha512-"):
        raise BuiltImageE2EError("attestation MCP integrity is invalid")
    _require_string(artifact["shasum"], "MCP shasum", pattern=_SHA1_RE)

    return Attestation(
        runtime_version=runtime_version,
        helper_sha256=helper_sha256,
        package=package,
        pinned_version=pinned,
        compatible_range=compatible,
        registry=registry,
        registry_scope=scope,
        required_tool=required_tool,
    )


def _distribution_file(distribution, relative: str) -> Path:
    entries = [
        entry
        for entry in (distribution.files or ())
        if str(entry).replace(os.sep, "/") == relative
    ]
    if len(entries) != 1:
        raise BuiltImageE2EError("installed runtime distribution files are incomplete")
    path = Path(distribution.locate_file(entries[0]))
    if path.is_symlink() or not path.is_file():
        raise BuiltImageE2EError("installed runtime distribution file is not regular")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise BuiltImageE2EError(
            "installed runtime distribution file is unreadable"
        ) from exc


def _module_origin(module, expected: Path) -> None:
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str):
        raise BuiltImageE2EError("installed runtime import origin is missing")
    try:
        actual = Path(origin).resolve(strict=True)
    except OSError as exc:
        raise BuiltImageE2EError(
            "installed runtime import origin is unreadable"
        ) from exc
    if actual != expected:
        raise BuiltImageE2EError("installed runtime import origin is shadowed")


def _installed_helper_digest(path: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        if path.stat().st_size > MAX_INSTALLED_HELPER_BYTES:
            raise BuiltImageE2EError("installed runtime helper is too large")
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                total += len(chunk)
                if total > MAX_INSTALLED_HELPER_BYTES:
                    raise BuiltImageE2EError("installed runtime helper is too large")
                digest.update(chunk)
    except OSError as exc:
        raise BuiltImageE2EError("installed runtime helper is unreadable") from exc
    return digest.hexdigest()


def verify_installed_runtime(attestation: Attestation) -> None:
    """Prove the imported runtime is the attested installed distribution."""

    try:
        distribution = metadata.distribution(RUNTIME_DISTRIBUTION)
    except metadata.PackageNotFoundError as exc:
        raise BuiltImageE2EError("installed runtime distribution is missing") from exc
    name = distribution.metadata.get("Name")
    normalized = re.sub(r"[-_.]+", "-", str(name)).lower()
    if normalized != RUNTIME_DISTRIBUTION:
        raise BuiltImageE2EError("installed runtime distribution identity is invalid")
    if distribution.version != attestation.runtime_version:
        raise BuiltImageE2EError("installed runtime distribution version disagrees")

    package_path = _distribution_file(distribution, _RUNTIME_FILES["package"])
    identity_path = _distribution_file(distribution, _RUNTIME_FILES["identity"])
    helper_path = _distribution_file(distribution, _RUNTIME_FILES["helper"])
    try:
        runtime_module = importlib.import_module("molecule_runtime")
        identity_module = importlib.import_module(
            "molecule_runtime.platform_agent_identity"
        )
    except Exception as exc:
        raise BuiltImageE2EError(
            "installed runtime modules cannot be imported"
        ) from exc
    _module_origin(runtime_module, package_path)
    _module_origin(identity_module, identity_path)

    helper_digest = _installed_helper_digest(helper_path)
    if helper_digest != attestation.helper_sha256:
        raise BuiltImageE2EError("installed runtime helper digest disagrees")

    expected_constants = {
        "MANAGEMENT_MCP_NPM_PACKAGE": attestation.package,
        "MANAGEMENT_MCP_PINNED_VERSION": attestation.pinned_version,
        "MANAGEMENT_MCP_COMPATIBLE_RANGE": attestation.compatible_range,
        "MANAGEMENT_MCP_REGISTRY": attestation.registry,
        "MANAGEMENT_MCP_REGISTRY_SCOPE": attestation.registry_scope,
        "REQUIRED_TOOL": attestation.required_tool,
    }
    if any(
        getattr(identity_module, name, None) != expected
        for name, expected in expected_constants.items()
    ):
        raise BuiltImageE2EError("installed runtime MCP constants disagree")


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    group_exists = True
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        group_exists = False
    except PermissionError:
        pass
    if group_exists:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def run_bounded_process(
    argv: list[str],
    *,
    input_bytes: bytes,
    env: dict[str, str],
    timeout_seconds: float = PROCESS_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_PROCESS_OUTPUT_BYTES,
) -> ProcessResult:
    """Run one argv-only process group with bounded time and combined output."""

    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise BuiltImageE2EError("offline MCP launch argv is invalid")
    if len(input_bytes) > 64 * 1024:
        raise BuiltImageE2EError("offline MCP request is too large")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise BuiltImageE2EError("offline MCP process could not start") from exc

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except BrokenPipeError:
            process.stdin.close()

        streams = {process.stdout: bytearray(), process.stderr: bytearray()}
        selector = selectors.DefaultSelector()
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_process_group(process)
                    raise BuiltImageE2EError("offline MCP process timed out")
                events = selector.select(min(remaining, 0.1))
                for key, _mask in events:
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    streams[key.fileobj].extend(chunk)
                    if sum(len(value) for value in streams.values()) > max_output_bytes:
                        _terminate_process_group(process)
                        raise BuiltImageE2EError(
                            "offline MCP process exceeded output limit"
                        )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                raise BuiltImageE2EError("offline MCP process timed out")
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                _terminate_process_group(process)
                raise BuiltImageE2EError("offline MCP process timed out") from exc
            _terminate_process_group(process)
        finally:
            selector.close()
    except Exception:
        if process.poll() is None:
            _terminate_process_group(process)
        raise

    try:
        stdout = bytes(streams[process.stdout]).decode("utf-8")
        stderr = bytes(streams[process.stderr]).decode("utf-8")
    except UnicodeError as exc:
        raise BuiltImageE2EError(
            "offline MCP process emitted non-UTF-8 output"
        ) from exc
    return ProcessResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _jsonrpc_request() -> bytes:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "molecule-ci-built-image-e2e",
                    "version": "1",
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    return (
        "\n".join(
            json.dumps(message, separators=(",", ":"), sort_keys=True)
            for message in messages
        )
        + "\n"
    ).encode()


def validate_jsonrpc_output(
    payload: bytes | str,
    *,
    required_tool: str,
) -> str | None:
    """Validate initialize and tools/list responses without logging server output."""

    raw = payload.encode() if isinstance(payload, str) else payload
    responses: dict[int, dict] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        message = _strict_json_loads(line, "MCP response")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise BuiltImageE2EError("MCP response has an invalid JSON-RPC envelope")
        response_id = message.get("id")
        if response_id is not None and type(response_id) is not int:
            raise BuiltImageE2EError("MCP response id is invalid")
        if response_id not in (1, 2):
            continue
        if response_id in responses:
            raise BuiltImageE2EError("MCP response id is duplicated")
        if "error" in message:
            raise BuiltImageE2EError("MCP server returned an error response")
        result = message.get("result")
        if not isinstance(result, dict):
            raise BuiltImageE2EError("MCP response result is invalid")
        responses[response_id] = result

    initialize = responses.get(1)
    tools_result = responses.get(2)
    if initialize is None or tools_result is None:
        raise BuiltImageE2EError("MCP initialize or tools/list response is missing")
    if initialize.get("protocolVersion") != MCP_PROTOCOL_VERSION:
        raise BuiltImageE2EError("MCP initialize protocol version is invalid")
    if not isinstance(initialize.get("capabilities"), dict):
        raise BuiltImageE2EError("MCP initialize capabilities are invalid")
    server_info = initialize.get("serverInfo")
    if not isinstance(server_info, dict):
        raise BuiltImageE2EError("MCP initialize serverInfo is invalid")
    server_name = server_info.get("name")
    if (
        not isinstance(server_name, str)
        or not server_name
        or len(server_name) > 128
        or any(ord(character) < 0x20 for character in server_name)
    ):
        raise BuiltImageE2EError("MCP initialize serverInfo name is invalid")
    server_version = server_info.get("version")
    if server_version is not None and (
        not isinstance(server_version, str)
        or not server_version
        or len(server_version) > 128
        or any(ord(character) < 0x20 for character in server_version)
    ):
        raise BuiltImageE2EError("MCP serverInfo version is invalid")

    tools = tools_result.get("tools")
    if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
        raise BuiltImageE2EError("MCP tools/list result is invalid")
    names = {tool.get("name") for tool in tools if isinstance(tool.get("name"), str)}
    if required_tool not in names:
        raise BuiltImageE2EError("MCP required tool is missing")
    return server_version


def find_npx(environ: dict[str, str] | None = None) -> tuple[str, str]:
    """Resolve npx, honoring the runtime helper's one sanctioned node-bin override."""

    values = os.environ if environ is None else environ
    path = values.get("PATH", "")
    override = values.get("MOLECULE_PREBAKE_NODE_BIN", "")
    if override:
        override_path = Path(override)
        if not override_path.is_absolute() or os.pathsep in override:
            raise BuiltImageE2EError("MOLECULE_PREBAKE_NODE_BIN is invalid")
        path = override + (os.pathsep + path if path else "")
    executable = shutil.which("npx", path=path)
    if executable is None:
        raise BuiltImageE2EError("npx is not reachable in the final image")
    return executable, path


def _probe_environment(home: str, path: str) -> dict[str, str]:
    return {
        "HOME": home,
        "PATH": path,
        "MOLECULE_MCP_MODE": "management",
        "npm_config_cache": AGENT_NPM_CACHE,
        "NPM_CONFIG_USERCONFIG": AGENT_NPMRC,
        "npm_config_offline": "true",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def verify_offline_mcp(
    attestation: Attestation,
    *,
    npx_path: str,
    path_env: str | None = None,
    run_process: Callable[..., ProcessResult] = run_bounded_process,
) -> str | None:
    """Run exact/range JSON-RPC probes under the agent and a foreign HOME."""

    if path_env is None:
        path_env = str(Path(npx_path).parent) + os.pathsep + os.environ.get("PATH", "")
    specs = (
        f"{attestation.package}@{attestation.pinned_version}",
        f"{attestation.package}@{attestation.compatible_range}",
    )
    request = _jsonrpc_request()
    reported_server_versions: set[str | None] = set()
    with tempfile.TemporaryDirectory(prefix="mcp-built-image-foreign-home-") as foreign:
        for home in (AGENT_HOME, foreign):
            environment = _probe_environment(home, path_env)
            for spec in specs:
                result = run_process(
                    [npx_path, "-y", "--offline", spec],
                    input_bytes=request,
                    env=environment,
                    timeout_seconds=PROCESS_TIMEOUT_SECONDS,
                    max_output_bytes=MAX_PROCESS_OUTPUT_BYTES,
                )
                if result.returncode != 0:
                    raise BuiltImageE2EError("offline MCP launch failed")
                server_version = validate_jsonrpc_output(
                    result.stdout,
                    required_tool=attestation.required_tool,
                )
                reported_server_versions.add(server_version)
                if len(reported_server_versions) > 1:
                    raise BuiltImageE2EError("offline MCP serverInfo versions disagree")
    return next(iter(reported_server_versions), None)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, BuiltImageE2EError):
        return str(exc) or "built-image MCP verification failed"
    return "unexpected built-image MCP verifier failure"


def main() -> int:
    print(SENTINEL)
    try:
        attestation = load_attestation(sys.stdin.buffer)
        verify_installed_runtime(attestation)
        npx_path, path_env = find_npx()
        server_version = verify_offline_mcp(
            attestation,
            npx_path=npx_path,
            path_env=path_env,
        )
    except Exception as exc:
        print(_safe_error(exc), file=sys.stderr)
        return 1
    version_proof = (
        json.dumps(server_version) if server_version is not None else "not-emitted"
    )
    print(
        "mcp-built-image-e2e: PASS "
        f"(four offline JSON-RPC probes green; serverInfo.version={version_proof})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
