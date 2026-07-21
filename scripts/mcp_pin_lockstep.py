#!/usr/bin/env python3
"""Credential-free MCP artifact lockstep verification for runtime templates.

This static pre-pull gate deliberately proves only immutable artifact metadata:

    .runtime-version
      -> trusted runtime wheel URL + SHA-256 + wheel identity
      -> declared MCP package metadata + packaged prebake-helper bytes
      -> exact trusted npm tarball + registry integrity + package identity

It never executes wheel, helper, Dockerfile, or consumer-repository code. Runtime
release tests own helper semantics; each template's required Tier-4 image test owns
the installed wheel, offline cache, and final built-image behavior.
"""
from __future__ import annotations

import argparse
import ast
import base64
import gzip
import hashlib
import hmac
import http.client
import io
import json
import os
import re
import stat
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

MOLECULE_RUNTIME_INDEX_URL = (
    "https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/"
    "molecules-workspace-runtime/"
)
_PACKAGE_HOST = "git.moleculesai.app"
_PACKAGE_ORIGIN = ("https", _PACKAGE_HOST, 443)
_HTTP_ATTEMPT_TIMEOUT_SECONDS = 10
_HTTP_MAX_ATTEMPTS = 3
_HTTP_RETRY_DELAY_SECONDS = 0.25
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_WHEEL_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
_MAX_TAR_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_BYTES = 2 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_RUNTIME_PIN_BYTES = 32
_STABLE_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_NPM_SCOPE_RE = re.compile(r"^@[a-z0-9][a-z0-9._-]*$")
_NPM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MCP_TOOL_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_REQUIRED_METADATA = frozenset(
    {
        "MANAGEMENT_MCP_NPM_PACKAGE",
        "MANAGEMENT_MCP_PINNED_VERSION",
        "MANAGEMENT_MCP_COMPATIBLE_RANGE",
        "MANAGEMENT_MCP_REGISTRY",
        "MANAGEMENT_MCP_REGISTRY_SCOPE",
        "REQUIRED_TOOL",
    }
)
SENTINEL = "mcp-pin-lockstep:sentinel:executed"


class MCPPinLockstepError(Exception):
    """A fail-closed mcp-pin-lockstep contract violation."""


def strict_json_loads(payload: bytes, label: str):
    """Decode JSON while rejecting duplicate object keys at every depth."""

    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise MCPPinLockstepError(f"{label} is not UTF-8 JSON") from exc

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MCPPinLockstepError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        raise MCPPinLockstepError(f"{label} is malformed JSON") from exc


class _RuntimeWheelLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.hrefs.append(href)


def _https_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    return ("https", parsed.hostname.lower(), port or 443)


def _same_origin(left: str, right: str) -> bool:
    origin = _https_origin(left)
    return origin is not None and origin == _https_origin(right)


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject an off-origin Location before urllib issues the next request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _https_origin(newurl) != _PACKAGE_ORIGIN or not _same_origin(
            req.full_url, newurl
        ):
            raise MCPPinLockstepError("package request redirected off origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_PACKAGE_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


def _open_package_url(request: urllib.request.Request, *, timeout: int):
    return _PACKAGE_OPENER.open(request, timeout=timeout)


def _fetch_bytes(url: str) -> bytes:
    """Bounded credential-free GET with transient-only bounded retries.

    Transport failures, 429, and 5xx retry up to three 10-second attempts.
    Authentication and all other 4xx responses fail immediately.
    """
    if _https_origin(url) != _PACKAGE_ORIGIN:
        raise MCPPinLockstepError("refusing untrusted package URL")
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0"})
    last_error: Exception | None = None
    for attempt in range(1, _HTTP_MAX_ATTEMPTS + 1):
        try:
            with _open_package_url(request, timeout=_HTTP_ATTEMPT_TIMEOUT_SECONDS) as response:
                final_url = response.geturl()
                if not _same_origin(url, final_url):
                    raise MCPPinLockstepError("package request redirected off origin")
                length = response.headers.get("Content-Length")
                if length and int(length) > _MAX_ARTIFACT_BYTES:
                    raise MCPPinLockstepError(
                        f"package response exceeds {_MAX_ARTIFACT_BYTES} bytes"
                    )
                payload = response.read(_MAX_ARTIFACT_BYTES + 1)
                if len(payload) > _MAX_ARTIFACT_BYTES:
                    raise MCPPinLockstepError(
                        f"package response exceeds {_MAX_ARTIFACT_BYTES} bytes"
                    )
                return payload
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and not 500 <= exc.code <= 599:
                raise MCPPinLockstepError(
                    f"package fetch failed: HTTP {exc.code} (not retryable)"
                ) from exc
            last_error = exc
        except (
            TimeoutError,
            ConnectionError,
            urllib.error.URLError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            last_error = exc
        except ValueError as exc:
            raise MCPPinLockstepError("malformed package response") from exc
        if attempt < _HTTP_MAX_ATTEMPTS:
            time.sleep(_HTTP_RETRY_DELAY_SECONDS * attempt)
    raise MCPPinLockstepError(
        "package fetch failed after "
        f"{_HTTP_MAX_ATTEMPTS} attempts ({last_error.__class__.__name__})"
    ) from last_error


def _exact_semver(value: str, label: str) -> tuple[int, int, int]:
    match = _STABLE_SEMVER_RE.fullmatch(value)
    if not match:
        raise MCPPinLockstepError(f"{label} must be an exact stable semver")
    return tuple(int(part) for part in match.groups())


def _caret_contains(compatible: str, pinned: str) -> bool:
    if not compatible.startswith("^"):
        raise MCPPinLockstepError("MCP compatible range must be a caret stable semver")
    floor = _exact_semver(compatible[1:], "MCP compatible range floor")
    version = _exact_semver(pinned, "runtime MCP pinned version")
    if floor[0] > 0:
        ceiling = (floor[0] + 1, 0, 0)
    elif floor[1] > 0:
        ceiling = (0, floor[1] + 1, 0)
    else:
        ceiling = (0, 0, floor[2] + 1)
    return floor <= version < ceiling


def _template_runtime_pin(repo_root: Path) -> str:
    pin_path = repo_root / ".runtime-version"
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(pin_path, flags)
    except FileNotFoundError as exc:
        raise MCPPinLockstepError("missing .runtime-version exact runtime pin") from exc
    except OSError as exc:
        raise MCPPinLockstepError(
            ".runtime-version must be a regular file, not a symlink or special file"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise MCPPinLockstepError(
                ".runtime-version must be a regular file, not a symlink or special file"
            )
        payload = os.read(descriptor, _MAX_RUNTIME_PIN_BYTES + 1)
    finally:
        os.close(descriptor)
    if metadata.st_size > _MAX_RUNTIME_PIN_BYTES or len(payload) > _MAX_RUNTIME_PIN_BYTES:
        raise MCPPinLockstepError(".runtime-version is too large")
    try:
        pin = payload.decode("utf-8").strip()
    except UnicodeError as exc:
        raise MCPPinLockstepError(".runtime-version is not UTF-8 text") from exc
    _exact_semver(pin, ".runtime-version")
    return pin


def _runtime_wheel_reference(index: bytes, runtime_version: str) -> tuple[str, str]:
    expected = f"molecules_workspace_runtime-{runtime_version}-py3-none-any.whl"
    parser = _RuntimeWheelLinks()
    try:
        parser.feed(index.decode("utf-8"))
    except UnicodeError as exc:
        raise MCPPinLockstepError("runtime package index is not UTF-8 HTML") from exc
    matches: list[tuple[str, str]] = []
    for href in parser.hrefs:
        absolute = urllib.parse.urljoin(MOLECULE_RUNTIME_INDEX_URL, href)
        parsed = urllib.parse.urlsplit(absolute)
        if urllib.parse.unquote(Path(parsed.path).name) != expected:
            continue
        if parsed.query:
            raise MCPPinLockstepError(
                "exact runtime wheel URL is not canonical (query forbidden)"
            )
        digest_match = re.fullmatch(r"sha256=([0-9a-f]{64})", parsed.fragment)
        if digest_match is None:
            raise MCPPinLockstepError(
                "exact runtime wheel URL is not canonical (one sha256 fragment required)"
            )
        digest = digest_match.group(1)
        clean = urllib.parse.urlunsplit(parsed._replace(fragment=""))
        if not _same_origin(clean, MOLECULE_RUNTIME_INDEX_URL):
            raise MCPPinLockstepError("runtime wheel URL leaves trusted registry")
        matches.append((clean, digest))
    if len(matches) != 1:
        raise MCPPinLockstepError(
            f"expected exactly one immutable runtime wheel for {runtime_version}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _declared_runtime_metadata(source: str) -> dict[str, str]:
    """Read data declarations without importing or executing the runtime module."""
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise MCPPinLockstepError(
            "runtime platform_agent_identity.py is invalid Python"
        ) from exc

    writes: dict[str, list[ast.AST]] = {name: [] for name in _REQUIRED_METADATA}
    for node in ast.walk(module):
        if (
            isinstance(node, ast.Name)
            and node.id in _REQUIRED_METADATA
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            writes[node.id].append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in _REQUIRED_METADATA:
                writes[node.name].append(node)
        elif isinstance(node, ast.arg) and node.arg in _REQUIRED_METADATA:
            writes[node.arg].append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    for name in _REQUIRED_METADATA:
                        writes[name].append(node)
                    continue
                bound = alias.asname
                if bound is None:
                    bound = (
                        alias.name.partition(".")[0]
                        if isinstance(node, ast.Import)
                        else alias.name
                    )
                if bound in _REQUIRED_METADATA:
                    writes[bound].append(node)
        elif isinstance(node, ast.ExceptHandler) and node.name in _REQUIRED_METADATA:
            writes[node.name].append(node)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name in _REQUIRED_METADATA:
                writes[node.name].append(node)
        elif isinstance(node, ast.MatchMapping) and node.rest in _REQUIRED_METADATA:
            writes[node.rest].append(node)

    values: dict[str, str] = {}
    literal_targets: dict[str, ast.Name] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id in _REQUIRED_METADATA
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            values[target.id] = node.value.value
            literal_targets[target.id] = target

    invalid = sorted(
        name
        for name in _REQUIRED_METADATA
        if len(writes[name]) != 1 or literal_targets.get(name) is not writes[name][0]
    )
    if invalid:
        raise MCPPinLockstepError(
            "runtime wheel requires exactly one top-level literal declaration for MCP "
            "artifact metadata: "
            + ", ".join(invalid)
        )
    return values


def _runtime_contract(wheel_bytes: bytes, runtime_version: str) -> dict[str, str]:
    source_path = "molecule_runtime/platform_agent_identity.py"
    helper_path = "molecule_runtime/scripts/prebake-mgmt-mcp.sh"
    try:
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as wheel:
            members = wheel.infolist()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise MCPPinLockstepError(
                    f"runtime wheel has too many members: {len(members)}"
                )
            total_size = 0
            for member in members:
                if member.flag_bits & 0x1:
                    raise MCPPinLockstepError("runtime wheel contains an encrypted member")
                if member.file_size < 0 or member.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
                    raise MCPPinLockstepError(
                        f"runtime wheel member exceeds {_MAX_ARCHIVE_MEMBER_BYTES} "
                        "uncompressed bytes"
                    )
                total_size += member.file_size
                if total_size > _MAX_WHEEL_UNCOMPRESSED_BYTES:
                    raise MCPPinLockstepError(
                        "runtime wheel total uncompressed size exceeds "
                        f"{_MAX_WHEEL_UNCOMPRESSED_BYTES} bytes"
                    )

            member_names = [member.filename for member in members]
            names = set(member_names)
            if len(names) != len(member_names):
                raise MCPPinLockstepError(
                    "runtime wheel contains duplicate member names"
                )
            if source_path not in names or helper_path not in names:
                raise MCPPinLockstepError(
                    "exact runtime wheel is missing declared MCP metadata or "
                    "the packaged prebake helper"
                )

            metadata_paths = [
                name
                for name in names
                if name.endswith(".dist-info/METADATA")
                and Path(name).name == "METADATA"
            ]
            if len(metadata_paths) != 1:
                raise MCPPinLockstepError(
                    "exact runtime wheel must contain one METADATA file; "
                    f"found {len(metadata_paths)}"
                )
            metadata = BytesParser().parsebytes(wheel.read(metadata_paths[0]))
            names = metadata.get_all("Name", [])
            versions = metadata.get_all("Version", [])
            if len(names) != 1 or len(versions) != 1:
                raise MCPPinLockstepError(
                    "runtime wheel METADATA requires exactly one Name and Version header"
                )
            if (
                names[0].lower().replace("_", "-")
                != "molecules-workspace-runtime"
            ):
                raise MCPPinLockstepError(
                    "runtime wheel METADATA has the wrong project name"
                )
            if versions[0] != runtime_version:
                raise MCPPinLockstepError(
                    "runtime wheel METADATA version does not match .runtime-version"
                )

            source = wheel.read(source_path).decode("utf-8")
            helper = wheel.read(helper_path)
    except (zipfile.BadZipFile, KeyError, UnicodeError, OSError) as exc:
        raise MCPPinLockstepError("runtime wheel is malformed") from exc

    if not helper:
        raise MCPPinLockstepError("runtime wheel packages an empty prebake helper")

    values = _declared_runtime_metadata(source)
    values["_PREBAKE_HELPER_SHA256"] = hashlib.sha256(helper).hexdigest()

    if not _caret_contains(
        values["MANAGEMENT_MCP_COMPATIBLE_RANGE"],
        values["MANAGEMENT_MCP_PINNED_VERSION"],
    ):
        raise MCPPinLockstepError(
            f"runtime MCP pin {values['MANAGEMENT_MCP_PINNED_VERSION']} is outside "
            f"compatible range {values['MANAGEMENT_MCP_COMPATIBLE_RANGE']}"
        )
    package = values["MANAGEMENT_MCP_NPM_PACKAGE"]
    scope = values["MANAGEMENT_MCP_REGISTRY_SCOPE"]
    registry = values["MANAGEMENT_MCP_REGISTRY"]
    required_tool = values["REQUIRED_TOOL"]
    if not _NPM_SCOPE_RE.fullmatch(scope):
        raise MCPPinLockstepError("runtime MCP registry scope is not a valid npm scope")
    package_prefix = scope + "/"
    package_name = package.removeprefix(package_prefix)
    if (
        not package.startswith(package_prefix)
        or not _NPM_NAME_RE.fullmatch(package_name)
    ):
        raise MCPPinLockstepError(
            "runtime MCP package name is invalid or disagrees with its registry scope"
        )
    if not _MCP_TOOL_RE.fullmatch(required_tool):
        raise MCPPinLockstepError("runtime MCP required tool is empty or malformed")
    parsed_registry = urllib.parse.urlsplit(registry)
    if (
        _https_origin(registry) != _PACKAGE_ORIGIN
        or parsed_registry.path != "/api/packages/molecule-ai/npm/"
        or parsed_registry.query
        or parsed_registry.fragment
    ):
        raise MCPPinLockstepError("runtime wheel names an untrusted MCP registry")
    return values


def _verify_mcp_tarball(
    tarball: bytes,
    *,
    package: str,
    version: str,
    integrity: str,
    shasum: str,
) -> None:
    if not integrity.startswith("sha512-"):
        raise MCPPinLockstepError("exact MCP artifact lacks sha512 integrity")
    try:
        expected_sha512 = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
    except ValueError as exc:
        raise MCPPinLockstepError("exact MCP artifact has malformed sha512 integrity") from exc
    if not hmac.compare_digest(hashlib.sha512(tarball).digest(), expected_sha512):
        raise MCPPinLockstepError("exact MCP tarball sha512 integrity mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", shasum):
        raise MCPPinLockstepError("exact MCP artifact has malformed sha1 shasum")
    # npm's legacy shasum is an additional metadata consistency check; the
    # security integrity boundary above is SHA-512.
    legacy_sha1 = hashlib.sha1(tarball, usedforsecurity=False).hexdigest()
    if not hmac.compare_digest(legacy_sha1, shasum):
        raise MCPPinLockstepError("exact MCP tarball sha1 shasum mismatch")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(tarball), mode="rb") as compressed:
            uncompressed = compressed.read(_MAX_TAR_UNCOMPRESSED_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise MCPPinLockstepError("exact MCP gzip payload is malformed") from exc
    if len(uncompressed) > _MAX_TAR_UNCOMPRESSED_BYTES:
        raise MCPPinLockstepError(
            f"MCP gzip payload exceeds {_MAX_TAR_UNCOMPRESSED_BYTES} uncompressed bytes"
        )
    regular_members: set[str]
    try:
        with tarfile.open(fileobj=io.BytesIO(uncompressed), mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise MCPPinLockstepError(
                    f"MCP tarball has too many members: {len(members)}"
                )
            member_names = [member.name for member in members]
            if len(set(member_names)) != len(member_names):
                raise MCPPinLockstepError(
                    "MCP tarball contains duplicate member names"
                )
            regular_members = {member.name for member in members if member.isfile()}
            total_size = 0
            for item in members:
                if item.size < 0 or item.size > _MAX_ARCHIVE_MEMBER_BYTES:
                    raise MCPPinLockstepError(
                        f"MCP tarball member exceeds {_MAX_ARCHIVE_MEMBER_BYTES} "
                        "uncompressed bytes"
                    )
                total_size += item.size
                if total_size > _MAX_TAR_UNCOMPRESSED_BYTES:
                    raise MCPPinLockstepError(
                        "MCP tarball total uncompressed member size exceeds "
                        f"{_MAX_TAR_UNCOMPRESSED_BYTES} bytes"
                    )
            member = archive.getmember("package/package.json")
            if not member.isfile():
                raise MCPPinLockstepError("MCP tarball package.json is not a bounded file")
            stream = archive.extractfile(member)
            if stream is None:
                raise MCPPinLockstepError("MCP tarball package.json is unreadable")
            manifest = strict_json_loads(
                stream.read(), "exact MCP tarball package.json"
            )
    except (tarfile.TarError, KeyError, OSError) as exc:
        raise MCPPinLockstepError("exact MCP tarball is malformed") from exc
    if not isinstance(manifest, dict):
        raise MCPPinLockstepError("exact MCP tarball package.json must be a JSON object")
    if manifest.get("name") != package or manifest.get("version") != version:
        raise MCPPinLockstepError("MCP tarball package identity does not match its exact pin")
    binaries = manifest.get("bin")
    if isinstance(binaries, str) and binaries:
        targets = [binaries]
    elif (
        isinstance(binaries, dict)
        and binaries
        and all(
            isinstance(command, str)
            and command
            and isinstance(target, str)
            and target
            for command, target in binaries.items()
        )
    ):
        targets = list(binaries.values())
    else:
        raise MCPPinLockstepError("MCP tarball has no executable bin entry")
    for target in targets:
        relative = target.removeprefix("./")
        if (
            not relative
            or relative.startswith("/")
            or "\\" in relative
            or any(part in ("", ".", "..") for part in relative.split("/"))
            or f"package/{relative}" not in regular_members
        ):
            raise MCPPinLockstepError(
                "MCP tarball executable bin entry is not a packaged regular file"
            )


def _verify_exact_mcp_artifact(values: dict[str, str], fetch_bytes) -> dict[str, str]:
    package = values["MANAGEMENT_MCP_NPM_PACKAGE"]
    version = values["MANAGEMENT_MCP_PINNED_VERSION"]
    registry = values["MANAGEMENT_MCP_REGISTRY"]
    packument_url = registry + urllib.parse.quote(package, safe="")
    packument = strict_json_loads(
        fetch_bytes(packument_url), "MCP registry packument"
    )
    if not isinstance(packument, dict) or packument.get("name") != package:
        raise MCPPinLockstepError("MCP registry packument has the wrong package identity")
    versions = packument.get("versions")
    exact = versions.get(version) if isinstance(versions, dict) else None
    if not isinstance(exact, dict):
        raise MCPPinLockstepError(
            f"exact MCP package version {version} is missing from the registry"
        )
    if exact.get("name") != package or exact.get("version") != version:
        raise MCPPinLockstepError("exact MCP registry metadata has a mismatched identity")
    dist = exact.get("dist")
    if not isinstance(dist, dict):
        raise MCPPinLockstepError("exact MCP registry metadata lacks dist integrity")
    tarball_url = dist.get("tarball")
    integrity = dist.get("integrity")
    shasum = dist.get("shasum")
    if not all(isinstance(value, str) and value for value in (tarball_url, integrity, shasum)):
        raise MCPPinLockstepError("exact MCP registry metadata lacks immutable dist fields")
    parsed_tarball = urllib.parse.urlsplit(tarball_url)
    if (
        not _same_origin(tarball_url, registry)
        or parsed_tarball.query
        or parsed_tarball.fragment
    ):
        raise MCPPinLockstepError(
            "MCP tarball URL is not a canonical trusted-registry URL"
        )
    _verify_mcp_tarball(
        fetch_bytes(tarball_url),
        package=package,
        version=version,
        integrity=integrity,
        shasum=shasum,
    )
    return {
        "packument_url": packument_url,
        "tarball_url": tarball_url,
        "integrity": integrity,
        "shasum": shasum,
    }


def attest(
    repo_root: Path,
    *,
    fetch_bytes=_fetch_bytes,
) -> dict[str, object]:
    """Return the immutable artifact attestation; never execute fetched content."""
    runtime_version = _template_runtime_pin(repo_root)
    index = fetch_bytes(MOLECULE_RUNTIME_INDEX_URL)
    wheel_url, wheel_sha = _runtime_wheel_reference(index, runtime_version)
    wheel = fetch_bytes(wheel_url)
    if not hmac.compare_digest(hashlib.sha256(wheel).hexdigest(), wheel_sha):
        raise MCPPinLockstepError("exact runtime wheel sha256 mismatch")
    values = _runtime_contract(wheel, runtime_version)
    mcp_artifact = _verify_exact_mcp_artifact(values, fetch_bytes)

    return {
        "schema_version": 1,
        "runtime": {
            "project": "molecules-workspace-runtime",
            "version": runtime_version,
            "wheel_url": wheel_url,
            "wheel_sha256": wheel_sha,
            "prebake_helper_sha256": values["_PREBAKE_HELPER_SHA256"],
        },
        "management_mcp": {
            "package": values["MANAGEMENT_MCP_NPM_PACKAGE"],
            "pinned_version": values["MANAGEMENT_MCP_PINNED_VERSION"],
            "compatible_range": values["MANAGEMENT_MCP_COMPATIBLE_RANGE"],
            "registry": values["MANAGEMENT_MCP_REGISTRY"],
            "registry_scope": values["MANAGEMENT_MCP_REGISTRY_SCOPE"],
            "required_tool": values["REQUIRED_TOOL"],
            "artifact": mcp_artifact,
        },
    }


def run(
    repo_root: Path,
    *,
    fetch_bytes=_fetch_bytes,
) -> tuple[bool, str]:
    try:
        attestation = attest(repo_root, fetch_bytes=fetch_bytes)
    except Exception as exc:  # runner boundary: unexpected conditions fail closed
        return False, str(exc) or exc.__class__.__name__

    runtime = attestation["runtime"]
    management = attestation["management_mcp"]
    return True, (
        f"runtime {runtime['version']} immutable wheel metadata -> "
        f"{management['package']}@{management['pinned_version']} immutable tarball; "
        f"exact pin satisfies {management['compatible_range']}; packaged helper "
        f"sha256={runtime['prebake_helper_sha256'][:12]} (execution is Tier-4)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="verify immutable runtime-to-MCP artifact metadata"
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="runtime-template checkout containing .runtime-version",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the machine-readable immutable artifact attestation",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    if args.json:
        try:
            payload = attest(repo_root)
        except Exception as exc:
            print(str(exc) or exc.__class__.__name__, file=sys.stderr)
            return 1
        payload["checker_sentinel"] = SENTINEL
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    ok, detail = run(repo_root)
    print(SENTINEL)
    print(detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
