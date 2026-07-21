"""Tests for the data-only runtime-to-MCP artifact lockstep checker."""
from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import urllib.error
import zipfile
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent
SCRIPT = _SCRIPTS / "mcp_pin_lockstep.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mcp_pin_lockstep_tested", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


lockstep = _load_module()


def _runtime_wheel(
    *,
    version: str = "0.4.25",
    pinned: str = "1.8.3",
    compatible: str = "^1.8.0",
    package: str = "@molecule-ai/mcp-server",
    scope: str = "@molecule-ai",
    registry: str = "https://git.moleculesai.app/api/packages/molecule-ai/npm/",
    required_tool: str = "create_workspace",
    helper: bytes = b"#!/usr/bin/env bash\nexit 1\n",
    source_extra: str = "",
    source_text: str | None = None,
    metadata_name: str = "molecules-workspace-runtime",
    metadata_version: str | None = None,
    metadata_extra: str = "",
    extra_members: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    source = source_text if source_text is not None else "\n".join(
        [
            f'MANAGEMENT_MCP_NPM_PACKAGE = "{package}"',
            f'MANAGEMENT_MCP_PINNED_VERSION = "{pinned}"',
            f'MANAGEMENT_MCP_COMPATIBLE_RANGE = "{compatible}"',
            f'MANAGEMENT_MCP_REGISTRY = "{registry}"',
            f'MANAGEMENT_MCP_REGISTRY_SCOPE = "{scope}"',
            f'REQUIRED_TOOL = "{required_tool}"',
            source_extra,
        ]
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as wheel:
        wheel.writestr("molecule_runtime/platform_agent_identity.py", source)
        wheel.writestr(
            "molecule_runtime/scripts/prebake-mgmt-mcp.sh",
            helper,
        )
        wheel.writestr(
            f"molecules_workspace_runtime-{version}.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            f"Name: {metadata_name}\n"
            f"Version: {metadata_version or version}\n"
            f"{metadata_extra}",
        )
        for name, payload in extra_members:
            wheel.writestr(name, payload)
    return stream.getvalue()


def _mcp_tarball(
    *,
    version: str = "1.8.3",
    package: str = "@molecule-ai/mcp-server",
    duplicate_manifest: bool = False,
    manifest_payload: bytes | None = None,
    extra_members: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    payload = manifest_payload or json.dumps(
        {
            "name": package,
            "version": version,
            "bin": {"molecule-mcp": "./dist/index.js"},
        }
    ).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
        executable = b"#!/usr/bin/env node\n"
        executable_info = tarfile.TarInfo("package/dist/index.js")
        executable_info.mode = 0o755
        executable_info.size = len(executable)
        archive.addfile(executable_info, io.BytesIO(executable))
        if duplicate_manifest:
            duplicate = tarfile.TarInfo("package/package.json")
            duplicate.size = len(payload)
            archive.addfile(duplicate, io.BytesIO(payload))
        for name, member_payload in extra_members:
            member = tarfile.TarInfo(name)
            member.size = len(member_payload)
            archive.addfile(member, io.BytesIO(member_payload))
    return stream.getvalue()


def _fixture(
    tmp_path: Path,
    *,
    runtime_version: str = "0.4.25",
    pinned: str = "1.8.3",
    wheel: bytes | None = None,
) -> tuple[dict[str, bytes], object]:
    (tmp_path / ".runtime-version").write_text(runtime_version + "\n")
    wheel = wheel or _runtime_wheel(version=runtime_version, pinned=pinned)
    wheel_sha = hashlib.sha256(wheel).hexdigest()
    wheel_name = f"molecules_workspace_runtime-{runtime_version}-py3-none-any.whl"
    wheel_url = (
        "https://git.moleculesai.app/api/packages/molecule-ai/pypi/files/"
        f"molecules-workspace-runtime/{runtime_version}/{wheel_name}"
    )
    index = f'<a href="{wheel_url}#sha256={wheel_sha}">{wheel_name}</a>'.encode()

    tarball = _mcp_tarball(version=pinned)
    tarball_url = (
        "https://git.moleculesai.app/api/packages/molecule-ai/npm/"
        f"%40molecule-ai%2Fmcp-server/-/{pinned}/mcp-server-{pinned}.tgz"
    )
    packument_url = (
        "https://git.moleculesai.app/api/packages/molecule-ai/npm/"
        "%40molecule-ai%2Fmcp-server"
    )
    packument = json.dumps(
        {
            "name": "@molecule-ai/mcp-server",
            "versions": {
                pinned: {
                    "name": "@molecule-ai/mcp-server",
                    "version": pinned,
                    "dist": {
                        "integrity": "sha512-"
                        + base64.b64encode(hashlib.sha512(tarball).digest()).decode(),
                        "shasum": hashlib.sha1(tarball).hexdigest(),
                        "tarball": tarball_url,
                    },
                }
            },
        }
    ).encode()
    responses = {
        lockstep.MOLECULE_RUNTIME_INDEX_URL: index,
        wheel_url: wheel,
        packument_url: packument,
        tarball_url: tarball,
    }

    def fetch(url: str) -> bytes:
        if url not in responses:
            raise AssertionError(f"unexpected URL: {url}")
        return responses[url]

    return responses, fetch


def test_attestation_follows_exact_immutable_artifact_metadata(tmp_path):
    responses, fetch = _fixture(tmp_path)

    attestation = lockstep.attest(tmp_path, fetch_bytes=fetch)

    runtime = attestation["runtime"]
    management = attestation["management_mcp"]
    wheel_url = next(url for url in responses if url.endswith(".whl"))
    assert runtime == {
        "project": "molecules-workspace-runtime",
        "version": "0.4.25",
        "wheel_url": wheel_url,
        "wheel_sha256": hashlib.sha256(responses[wheel_url]).hexdigest(),
        "prebake_helper_sha256": hashlib.sha256(
            b"#!/usr/bin/env bash\nexit 1\n"
        ).hexdigest(),
    }
    assert management == {
        "package": "@molecule-ai/mcp-server",
        "pinned_version": "1.8.3",
        "compatible_range": "^1.8.0",
        "registry": "https://git.moleculesai.app/api/packages/molecule-ai/npm/",
        "registry_scope": "@molecule-ai",
        "required_tool": "create_workspace",
        "artifact": {
            "packument_url": "https://git.moleculesai.app/api/packages/molecule-ai/npm/%40molecule-ai%2Fmcp-server",
            "tarball_url": "https://git.moleculesai.app/api/packages/molecule-ai/npm/%40molecule-ai%2Fmcp-server/-/1.8.3/mcp-server-1.8.3.tgz",
            "integrity": json.loads(
                responses[
                    "https://git.moleculesai.app/api/packages/molecule-ai/npm/%40molecule-ai%2Fmcp-server"
                ]
            )["versions"]["1.8.3"]["dist"]["integrity"],
            "shasum": hashlib.sha1(
                responses[
                    "https://git.moleculesai.app/api/packages/molecule-ai/npm/%40molecule-ai%2Fmcp-server/-/1.8.3/mcp-server-1.8.3.tgz"
                ]
            ).hexdigest(),
        },
    }


def test_runner_labels_static_boundary_instead_of_claiming_execution(tmp_path):
    _responses, fetch = _fixture(tmp_path)

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert ok, detail
    assert "immutable wheel metadata" in detail
    assert "execution is Tier-4" in detail


@pytest.mark.parametrize("value", ["", "latest", "0.4", "0.4.25rc1", "01.2.3"])
def test_runtime_pin_is_exact_stable_semver(tmp_path, value):
    (tmp_path / ".runtime-version").write_text(value + "\n")

    with pytest.raises(lockstep.MCPPinLockstepError, match="exact stable semver"):
        lockstep._template_runtime_pin(tmp_path)


def test_missing_runtime_pin_fails_before_network(tmp_path):
    called = False

    def fetch(_url):
        nonlocal called
        called = True
        return b""

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert ".runtime-version" in detail
    assert not called


def test_runtime_pin_rejects_symlink_without_disclosing_target(tmp_path):
    secret = "http.extraheader=AUTHORIZATION: basic must-not-reach-logs"
    target = tmp_path / "checkout-config"
    target.write_text(secret)
    (tmp_path / ".runtime-version").symlink_to(target)

    with pytest.raises(lockstep.MCPPinLockstepError) as error:
        lockstep._template_runtime_pin(tmp_path)

    assert "regular file" in str(error.value)
    assert secret not in str(error.value)


def test_runtime_pin_rejects_oversized_value_without_disclosing_it(tmp_path):
    secret = "must-not-reach-logs-" * 20
    (tmp_path / ".runtime-version").write_text(secret)

    with pytest.raises(lockstep.MCPPinLockstepError) as error:
        lockstep._template_runtime_pin(tmp_path)

    assert "too large" in str(error.value)
    assert secret not in str(error.value)


def test_runtime_pin_semver_error_never_echoes_raw_value(tmp_path):
    secret = "invalid-secret-shaped-pin"
    (tmp_path / ".runtime-version").write_text(secret)

    with pytest.raises(lockstep.MCPPinLockstepError) as error:
        lockstep._template_runtime_pin(tmp_path)

    assert "exact stable semver" in str(error.value)
    assert secret not in str(error.value)


def test_wheel_hash_mismatch_fails_closed(tmp_path):
    responses, fetch = _fixture(tmp_path)
    wheel_url = next(url for url in responses if url.endswith(".whl"))
    responses[wheel_url] += b"tampered"

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "sha256 mismatch" in detail


@pytest.mark.parametrize(
    ("wheel", "message"),
    [
        (_runtime_wheel(helper=b""), "empty prebake helper"),
        (
            _runtime_wheel(metadata_name="other-runtime"),
            "wrong project name",
        ),
        (
            _runtime_wheel(metadata_version="9.9.9"),
            "does not match .runtime-version",
        ),
    ],
)
def test_wheel_identity_and_packaged_helper_fail_closed(tmp_path, wheel, message):
    _responses, fetch = _fixture(tmp_path, wheel=wheel)

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert message in detail


@pytest.mark.parametrize(
    "metadata_extra",
    [
        "Name: molecules-workspace-runtime\n",
        "Version: 0.4.25\n",
    ],
)
def test_wheel_metadata_rejects_duplicate_identity_headers(metadata_extra):
    wheel = _runtime_wheel(metadata_extra=metadata_extra)

    with pytest.raises(lockstep.MCPPinLockstepError, match="exactly one"):
        lockstep._runtime_contract(wheel, "0.4.25")


@pytest.mark.parametrize(
    ("wheel", "message"),
    [
        (_runtime_wheel(required_tool=""), "required tool"),
        (_runtime_wheel(scope="molecule-ai", package="molecule-ai/mcp-server"), "scope"),
        (
            _runtime_wheel(package="@molecule-ai/mcp-server/extra"),
            "package name",
        ),
    ],
)
def test_runtime_declared_names_are_nonempty_and_well_formed(wheel, message):
    with pytest.raises(lockstep.MCPPinLockstepError, match=message):
        lockstep._runtime_contract(wheel, "0.4.25")


def test_static_checker_never_executes_wheel_or_consumer_code(tmp_path):
    wheel = _runtime_wheel(source_extra='raise RuntimeError("must not execute")')
    _responses, fetch = _fixture(tmp_path, wheel=wheel)
    marker = tmp_path / "consumer-code-ran"
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "preinstall": f"touch {marker}",
                    "test": f"touch {marker}",
                }
            }
        )
    )

    attestation = lockstep.attest(tmp_path, fetch_bytes=fetch)

    assert attestation["management_mcp"]["pinned_version"] == "1.8.3"
    assert not marker.exists()


def test_duplicate_literal_metadata_is_rejected(tmp_path):
    wheel = _runtime_wheel(
        source_extra='MANAGEMENT_MCP_PINNED_VERSION = "9.9.9"'
    )
    _responses, fetch = _fixture(tmp_path, wheel=wheel)

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "exactly one top-level literal declaration" in detail


@pytest.mark.parametrize(
    "source_text",
    [
        'MANAGEMENT_MCP_NPM_PACKAGE = "@molecule-ai/mcp-server"',
        "\n".join(
            [
                'MANAGEMENT_MCP_NPM_PACKAGE = "@molecule-ai/mcp-server"',
                'MANAGEMENT_MCP_PINNED_VERSION = "".join(["1", ".8.3"])',
                'MANAGEMENT_MCP_COMPATIBLE_RANGE = "^1.8.0"',
                'MANAGEMENT_MCP_REGISTRY = "https://git.moleculesai.app/api/packages/molecule-ai/npm/"',
                'MANAGEMENT_MCP_REGISTRY_SCOPE = "@molecule-ai"',
                'REQUIRED_TOOL = "create_workspace"',
            ]
        ),
        "\n".join(
            [
                'MANAGEMENT_MCP_NPM_PACKAGE = "@molecule-ai/mcp-server"',
                'MANAGEMENT_MCP_PINNED_VERSION = "1.8.3"',
                'MANAGEMENT_MCP_COMPATIBLE_RANGE = "^1.8.0"',
                'MANAGEMENT_MCP_REGISTRY = "https://git.moleculesai.app/api/packages/molecule-ai/npm/"',
                'MANAGEMENT_MCP_REGISTRY_SCOPE = "@molecule-ai"',
                'REQUIRED_TOOL = "create_workspace"',
                "del REQUIRED_TOOL",
            ]
        ),
    ],
)
def test_runtime_metadata_requires_all_unambiguous_literal_declarations(source_text):
    wheel = _runtime_wheel(source_text=source_text)

    with pytest.raises(
        lockstep.MCPPinLockstepError,
        match="exactly one top-level literal declaration",
    ):
        lockstep._runtime_contract(wheel, "0.4.25")


def test_pin_outside_declared_compatible_range_is_rejected(tmp_path):
    wheel = _runtime_wheel(pinned="2.0.0", compatible="^1.8.0")
    _responses, fetch = _fixture(
        tmp_path,
        pinned="2.0.0",
        wheel=wheel,
    )

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "outside compatible range" in detail


def test_untrusted_runtime_registry_does_not_echo_artifact_controlled_value():
    marker = "credential=must-not-log"
    wheel = _runtime_wheel(
        registry=f"https://git.moleculesai.app/api/packages/molecule-ai/npm/?{marker}"
    )

    with pytest.raises(lockstep.MCPPinLockstepError) as error:
        lockstep._runtime_contract(wheel, "0.4.25")

    assert "untrusted MCP registry" in str(error.value)
    assert marker not in str(error.value)


def test_invalid_runtime_range_does_not_echo_artifact_controlled_value():
    marker = "credential=must-not-log"
    wheel = _runtime_wheel(compatible=marker)

    with pytest.raises(lockstep.MCPPinLockstepError) as error:
        lockstep._runtime_contract(wheel, "0.4.25")

    assert "caret stable semver" in str(error.value)
    assert marker not in str(error.value)


def test_missing_exact_mcp_version_is_rejected(tmp_path):
    responses, fetch = _fixture(tmp_path)
    packument_url = next(
        url for url in responses if url.endswith("%40molecule-ai%2Fmcp-server")
    )
    responses[packument_url] = json.dumps(
        {"name": "@molecule-ai/mcp-server", "versions": {}}
    ).encode()

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "exact MCP package version 1.8.3 is missing" in detail


def test_mcp_tarball_integrity_mismatch_is_rejected(tmp_path):
    responses, fetch = _fixture(tmp_path)
    tarball_url = next(url for url in responses if url.endswith(".tgz"))
    responses[tarball_url] += b"tampered"

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "integrity mismatch" in detail


def test_mcp_tarball_package_identity_is_rejected(tmp_path):
    responses, fetch = _fixture(tmp_path)
    tarball_url = next(url for url in responses if url.endswith(".tgz"))
    bad = _mcp_tarball(package="@molecule-ai/not-the-server")
    responses[tarball_url] = bad
    packument_url = next(
        url for url in responses if url.endswith("%40molecule-ai%2Fmcp-server")
    )
    packument = json.loads(responses[packument_url])
    dist = packument["versions"]["1.8.3"]["dist"]
    dist["integrity"] = "sha512-" + base64.b64encode(
        hashlib.sha512(bad).digest()
    ).decode()
    dist["shasum"] = hashlib.sha1(bad).hexdigest()
    responses[packument_url] = json.dumps(packument).encode()

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "identity does not match" in detail


def test_mcp_tarball_duplicate_manifest_is_rejected():
    tarball = _mcp_tarball(duplicate_manifest=True)

    with pytest.raises(lockstep.MCPPinLockstepError, match="duplicate member names"):
        lockstep._verify_mcp_tarball(
            tarball,
            package="@molecule-ai/mcp-server",
            version="1.8.3",
            integrity="sha512-"
            + base64.b64encode(hashlib.sha512(tarball).digest()).decode(),
            shasum=hashlib.sha1(tarball).hexdigest(),
        )


def test_mcp_manifest_rejects_duplicate_json_keys():
    payload = (
        b'{"name":"@molecule-ai/mcp-server",'
        b'"name":"@molecule-ai/mcp-server",'
        b'"version":"1.8.3","bin":{"molecule-mcp":"./dist/index.js"}}'
    )
    tarball = _mcp_tarball(manifest_payload=payload)

    with pytest.raises(lockstep.MCPPinLockstepError, match="duplicate JSON key"):
        lockstep._verify_mcp_tarball(
            tarball,
            package="@molecule-ai/mcp-server",
            version="1.8.3",
            integrity="sha512-"
            + base64.b64encode(hashlib.sha512(tarball).digest()).decode(),
            shasum=hashlib.sha1(tarball).hexdigest(),
        )


def test_mcp_manifest_must_be_a_json_object():
    tarball = _mcp_tarball(manifest_payload=b"[]")

    with pytest.raises(lockstep.MCPPinLockstepError, match="JSON object"):
        lockstep._verify_mcp_tarball(
            tarball,
            package="@molecule-ai/mcp-server",
            version="1.8.3",
            integrity="sha512-"
            + base64.b64encode(hashlib.sha512(tarball).digest()).decode(),
            shasum=hashlib.sha1(tarball).hexdigest(),
        )


@pytest.mark.parametrize(
    "bin_value",
    [
        {"molecule-mcp": ""},
        {"molecule-mcp": "../outside.js"},
        {"molecule-mcp": "./dist/missing.js"},
    ],
)
def test_mcp_manifest_bin_must_name_a_packaged_regular_file(bin_value):
    payload = json.dumps(
        {
            "name": "@molecule-ai/mcp-server",
            "version": "1.8.3",
            "bin": bin_value,
        }
    ).encode()
    tarball = _mcp_tarball(manifest_payload=payload)

    with pytest.raises(lockstep.MCPPinLockstepError, match="executable bin entry"):
        lockstep._verify_mcp_tarball(
            tarball,
            package="@molecule-ai/mcp-server",
            version="1.8.3",
            integrity="sha512-"
            + base64.b64encode(hashlib.sha512(tarball).digest()).decode(),
            shasum=hashlib.sha1(tarball).hexdigest(),
        )


@pytest.mark.parametrize("duplicate", ["name", "dist"])
def test_packument_rejects_duplicate_json_keys(tmp_path, duplicate):
    responses, fetch = _fixture(tmp_path)
    packument_url = next(
        url for url in responses if url.endswith("%40molecule-ai%2Fmcp-server")
    )
    raw = responses[packument_url].decode()
    if duplicate == "name":
        raw = raw.replace(
            '"name": "@molecule-ai/mcp-server",',
            '"name": "@molecule-ai/mcp-server", "name": "@molecule-ai/mcp-server",',
            1,
        )
    else:
        raw = raw.replace('"dist": {', '"dist": {}, "dist": {', 1)
    responses[packument_url] = raw.encode()

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "duplicate JSON key" in detail


def test_strict_json_loader_rejects_duplicate_official_consumer_fields():
    payload = (
        b'[{"name":"claude-code","repository":"repo",'
        b'"commit":"'
        + b"a" * 40
        + b'","commit":"'
        + b"b" * 40
        + b'"}]'
    )

    with pytest.raises(lockstep.MCPPinLockstepError, match="duplicate JSON key"):
        lockstep.strict_json_loads(payload, "official consumer manifest")


@pytest.mark.parametrize("suffix", ["?credential=must-not-log", "#unexpected"])
def test_mcp_tarball_url_rejects_query_or_fragment_before_fetch(tmp_path, suffix):
    responses, fetch = _fixture(tmp_path)
    packument_url = next(
        url for url in responses if url.endswith("%40molecule-ai%2Fmcp-server")
    )
    packument = json.loads(responses[packument_url])
    packument["versions"]["1.8.3"]["dist"]["tarball"] += suffix
    responses[packument_url] = json.dumps(packument).encode()

    ok, detail = lockstep.run(tmp_path, fetch_bytes=fetch)

    assert not ok
    assert "canonical" in detail
    assert "must-not-log" not in detail


@pytest.mark.parametrize(
    "suffix",
    [
        "?credential=must-not-log#sha256={digest}",
        "#sha256={digest}&extra=1",
    ],
)
def test_runtime_wheel_reference_rejects_query_or_extra_fragment(suffix):
    digest = "a" * 64
    wheel = "molecules_workspace_runtime-0.4.25-py3-none-any.whl"
    index = f'<a href="{wheel}{suffix.format(digest=digest)}">wheel</a>'.encode()

    with pytest.raises(lockstep.MCPPinLockstepError, match="canonical") as error:
        lockstep._runtime_wheel_reference(index, "0.4.25")

    assert "must-not-log" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://git.moleculesai.app/runtime.whl",
        "https://evil.example/runtime.whl",
        "https://user@git.moleculesai.app/runtime.whl",
        "https://git.moleculesai.app:444/runtime.whl",
    ],
)
def test_package_fetch_rejects_noncanonical_origin_before_open(monkeypatch, url):
    opened = False

    def opener(_request, *, timeout):
        nonlocal opened
        opened = True
        raise AssertionError(timeout)

    monkeypatch.setattr(lockstep, "_open_package_url", opener)

    with pytest.raises(lockstep.MCPPinLockstepError, match="untrusted package URL"):
        lockstep._fetch_bytes(url)
    assert not opened


def test_redirect_handler_rejects_off_origin_location_before_follow():
    request = lockstep.urllib.request.Request(lockstep.MOLECULE_RUNTIME_INDEX_URL)

    with pytest.raises(lockstep.MCPPinLockstepError, match="redirected off origin"):
        lockstep._SameOriginRedirectHandler().redirect_request(
            request,
            None,
            302,
            "redirect",
            {},
            "https://other.example/artifact.whl",
        )


class _Response:
    def __init__(self, url: str, payload: bytes):
        self.url = url
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit: int):
        return self.payload[:limit]


def test_package_fetch_rejects_off_origin_final_response(monkeypatch):
    url = lockstep.MOLECULE_RUNTIME_INDEX_URL
    monkeypatch.setattr(
        lockstep,
        "_open_package_url",
        lambda _request, *, timeout: _Response("https://other.example/final", b"bad"),
    )

    with pytest.raises(lockstep.MCPPinLockstepError, match="redirected off origin"):
        lockstep._fetch_bytes(url)


def test_package_fetch_retries_only_transient_failures(monkeypatch):
    url = lockstep.MOLECULE_RUNTIME_INDEX_URL
    outcomes = [
        http.client.IncompleteRead(b"partial", 100),
        urllib.error.HTTPError(url, 503, "unavailable", {}, None),
        _Response(url, b"ok"),
    ]
    sleeps = []

    def opener(_request, *, timeout):
        assert timeout == lockstep._HTTP_ATTEMPT_TIMEOUT_SECONDS
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(lockstep, "_open_package_url", opener)
    monkeypatch.setattr(lockstep.time, "sleep", sleeps.append)

    assert lockstep._fetch_bytes(url) == b"ok"
    assert sleeps == [
        lockstep._HTTP_RETRY_DELAY_SECONDS,
        lockstep._HTTP_RETRY_DELAY_SECONDS * 2,
    ]


def test_package_fetch_exhausts_bounded_transport_retries(monkeypatch):
    url = lockstep.MOLECULE_RUNTIME_INDEX_URL
    calls = 0

    def opener(_request, *, timeout):
        nonlocal calls
        calls += 1
        raise http.client.IncompleteRead(b"partial", 100)

    monkeypatch.setattr(lockstep, "_open_package_url", opener)
    monkeypatch.setattr(lockstep.time, "sleep", lambda _delay: None)

    with pytest.raises(lockstep.MCPPinLockstepError, match="after 3 attempts"):
        lockstep._fetch_bytes(url)
    assert calls == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_package_fetch_does_not_retry_client_errors(monkeypatch, status):
    url = lockstep.MOLECULE_RUNTIME_INDEX_URL
    calls = 0

    def opener(_request, *, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(url, status, "client error", {}, None)

    monkeypatch.setattr(lockstep, "_open_package_url", opener)

    with pytest.raises(lockstep.MCPPinLockstepError, match=f"HTTP {status}"):
        lockstep._fetch_bytes(url)
    assert calls == 1


def test_runtime_wheel_rejects_oversized_member(monkeypatch):
    monkeypatch.setattr(lockstep, "_MAX_ARCHIVE_MEMBER_BYTES", 1024)
    wheel = _runtime_wheel(extra_members=(("oversized", b"x" * 1025),))

    with pytest.raises(lockstep.MCPPinLockstepError, match="member exceeds"):
        lockstep._runtime_contract(wheel, "0.4.25")


def test_runtime_wheel_rejects_excessive_total_expansion(monkeypatch):
    monkeypatch.setattr(lockstep, "_MAX_ARCHIVE_MEMBER_BYTES", 1024)
    monkeypatch.setattr(lockstep, "_MAX_WHEEL_UNCOMPRESSED_BYTES", 1024)
    wheel = _runtime_wheel(
        extra_members=(("extra-a", b"a" * 400), ("extra-b", b"b" * 400))
    )

    with pytest.raises(lockstep.MCPPinLockstepError, match="total uncompressed size"):
        lockstep._runtime_contract(wheel, "0.4.25")


def test_mcp_tarball_rejects_gzip_expansion(monkeypatch):
    monkeypatch.setattr(lockstep, "_MAX_TAR_UNCOMPRESSED_BYTES", 128)
    tarball = _mcp_tarball()

    with pytest.raises(lockstep.MCPPinLockstepError, match="gzip payload exceeds"):
        lockstep._verify_mcp_tarball(
            tarball,
            package="@molecule-ai/mcp-server",
            version="1.8.3",
            integrity="sha512-"
            + base64.b64encode(hashlib.sha512(tarball).digest()).decode(),
            shasum=hashlib.sha1(tarball).hexdigest(),
        )


def test_mcp_tarball_rejects_oversized_member(monkeypatch):
    monkeypatch.setattr(lockstep, "_MAX_ARCHIVE_MEMBER_BYTES", 1024)
    tarball = _mcp_tarball(extra_members=(("package/oversized", b"x" * 1025),))

    with pytest.raises(lockstep.MCPPinLockstepError, match="member exceeds"):
        lockstep._verify_mcp_tarball(
            tarball,
            package="@molecule-ai/mcp-server",
            version="1.8.3",
            integrity="sha512-"
            + base64.b64encode(hashlib.sha512(tarball).digest()).decode(),
            shasum=hashlib.sha1(tarball).hexdigest(),
        )


def test_cli_json_emits_machine_readable_attestation(tmp_path, monkeypatch, capsys):
    _responses, fetch = _fixture(tmp_path)
    expected = lockstep.attest(tmp_path, fetch_bytes=fetch)
    monkeypatch.setattr(lockstep, "attest", lambda _repo_root: expected)

    assert lockstep.main(["--repo-root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["checker_sentinel"] == lockstep.SENTINEL
    assert payload["runtime"]["version"] == "0.4.25"
    assert payload["management_mcp"]["required_tool"] == "create_workspace"


def test_cli_failure_is_nonzero_and_secret_free(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(tmp_path)],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1
    assert lockstep.SENTINEL in proc.stdout
    assert ".runtime-version" in proc.stdout
    assert "token" not in proc.stdout.lower()
