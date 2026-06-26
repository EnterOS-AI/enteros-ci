#!/usr/bin/env node
/**
 * introspect-manifest.mjs — package-introspection mode of the reusable
 * conformance gate.
 *
 * GENERALIZED from
 * molecule-ai/molecule-core/scripts/mcp-verb-manifest-check/check-published-mcp-manifest.mjs.
 * The original hardcoded the package (@molecule-ai/mcp-server), the server mode
 * (management), and read its accepted set only from a core contract file. Here
 * the package, server mode, accepted set (contract OR --required-caps), and
 * server-name assertion are all parameters, so ANY consumer can adopt the same
 * fail-closed manifest ⊇ required-caps check.
 *
 * WHY THIS EXISTS
 * ───────────────
 * A capability VERB is PRODUCED by one repo and DELIVERED as a PUBLISHED npm
 * build. Nothing stops a published build from skewing away from the consumer's
 * contract — exactly the staging incident: a stale published build exposed only
 * `provision_workspace` while core had hand-asserted `create_workspace`, so
 * every freshly provisioned concierge silently degraded. This gate resolves the
 * ACTUAL tool manifest of the PUBLISHED build and asserts it satisfies the
 * contract BEFORE any consumer provisions against it.
 *
 * HOW IT RESOLVES THE MANIFEST
 * ────────────────────────────
 * run.sh installs the PUBLISHED package from the Gitea npm registry into a
 * throwaway dir and passes it as --install-dir. This script loads the installed
 * build's compiled createServer() under the requested --server-mode via a
 * monkeypatched MCP SDK (McpServer.prototype.tool records names), yielding the
 * literal tool set the published build registers — the same set a live consumer
 * would surface.
 *
 * PROVENANCE HAZARD — resolve the PUBLISHED package, never build from source.
 * The published artifacts may carry a mode split (e.g. management) that git
 * `main` does not; building from source would introspect a tool set the fleet
 * never runs. This script therefore deliberately runs against an INSTALLED
 * published tarball, not a source checkout.
 *
 * ASSERTIONS
 *   FAIL (exit 1) when:
 *     • the installed build can't be loaded / introspected, or
 *     • the introspected server name != --expected-server-name (when asserted), or
 *     • zero tools registered (introspection unreliable), or
 *     • the accepted set is empty (would fail-close every consumer), or
 *     • the manifest contains NONE of the accepted capabilities.
 *   WARN (exit 0, ::warning) when:
 *     • the manifest satisfies ONLY a transitional alias (canonical absent) —
 *       a loud early signal that removing the alias before updating the build
 *       would re-trigger the degrade; kept non-fatal so the migration window
 *       stays mergeable.
 *
 * Accepted set:
 *   --contract <path>  → required_tools (∪ legacy singular required_tool) and
 *                        transitional_tool_aliases, plus mcp_server_name as the
 *                        default expected-server-name.
 *   --required-caps    → explicit list (newline/comma separated), with
 *                        --transitional-aliases as the WARN band. Use when the
 *                        consumer has no contract file.
 *
 * Usage:
 *   node introspect-manifest.mjs \
 *     --package @molecule-ai/mcp-server \
 *     --install-dir <dir containing node_modules/<package>> \
 *     --server-mode management \
 *     [--contract <path>] | [--required-caps "a,b" [--transitional-aliases "c"]] \
 *     [--expected-server-name <name>]
 */

import { readFileSync, writeFileSync, rmSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join, isAbsolute } from "node:path";

function arg(flag, fallback) {
  const i = process.argv.indexOf(flag);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
function fail(msg) {
  console.error(`::error::${msg}`);
  process.exit(1);
}
function warn(msg) {
  console.error(`::warning::${msg}`);
}
function splitList(s) {
  return (s || "")
    .split(/[\n,]/)
    .map((x) => x.trim())
    .filter(Boolean);
}

const pkg = arg("--package", "@molecule-ai/mcp-server");
const installDir = arg("--install-dir", process.cwd());
const serverMode = arg("--server-mode", "management");
const contractPath = arg("--contract", "");
const requiredCapsArg = arg("--required-caps", "");
const transitionalArg = arg("--transitional-aliases", "");
let expectedServerName = arg("--expected-server-name", "");

// ── Determine the accepted capability set ───────────────────────────────────
let requiredTools = [];
let aliases = [];

if (contractPath) {
  let contract;
  try {
    contract = JSON.parse(readFileSync(contractPath, "utf8"));
  } catch (e) {
    fail(`Cannot read contract at ${contractPath}: ${e.message}`);
  }
  // Prefer the plural verb-SSOT fields; fall back to the legacy singular
  // required_tool so this is safe before/after a contract-shape change.
  requiredTools = Array.isArray(contract.required_tools) ? contract.required_tools : [];
  if (requiredTools.length === 0 && typeof contract.required_tool === "string") {
    requiredTools = [contract.required_tool];
  }
  aliases = Array.isArray(contract.transitional_tool_aliases)
    ? contract.transitional_tool_aliases
    : [];
  // The contract's mcp_server_name is the default name assertion unless the
  // caller overrode --expected-server-name explicitly.
  if (!expectedServerName && contract.mcp_server_name) {
    expectedServerName = contract.mcp_server_name;
  }
} else {
  requiredTools = splitList(requiredCapsArg);
  aliases = splitList(transitionalArg);
}

if (requiredTools.length === 0) {
  fail(
    `No required capabilities declared (contract required_tools/required_tool empty, or --required-caps empty) — ` +
      `the consumer would derive an EMPTY accepted set and fail-close. Refusing to "pass" an empty gate.`,
  );
}
const accepted = [...new Set([...requiredTools, ...aliases])];

// ── Resolve the published manifest via an in-tree child harness ──────────────
const absInstall = isAbsolute(installDir) ? installDir : join(process.cwd(), installDir);

// The harness is written into <install>/node_modules so its own module URL is
// inside the install tree — every bare specifier then resolves exactly as the
// installed package resolves it (the published dist is ESM and imports
// "@modelcontextprotocol/sdk" by bare specifier with no "exports" map, so the
// ESM resolver and CJS require.resolve pick DIFFERENT files; running from inside
// node_modules guarantees we patch the ESM McpServer the package actually loads).
// It patches McpServer.prototype.tool to record names, builds the server in the
// requested mode, and prints one JSON line.
const harnessSrc = `
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
const recorded = [];
const origTool = McpServer.prototype.tool;
McpServer.prototype.tool = function (name, ...rest) {
  if (typeof name === "string") recorded.push(name);
  try { return origTool.apply(this, [name, ...rest]); } catch { return undefined; }
};
process.env.MOLECULE_MCP_MODE = ${JSON.stringify(serverMode)};
// JEST_WORKER_ID suppresses the package's main() stdio auto-start.
process.env.JEST_WORKER_ID = process.env.JEST_WORKER_ID || "manifest-check";
try {
  const mod = await import(${JSON.stringify(pkg)});
  if (typeof mod.createServer !== "function") {
    console.log(JSON.stringify({ error: "createServer-not-exported" }));
    process.exit(0);
  }
  const srv = mod.createServer();
  const name = srv?.server?._serverInfo?.name ?? srv?.name ?? null;
  console.log(JSON.stringify({ name, tools: [...new Set(recorded)].sort() }));
} catch (e) {
  console.log(JSON.stringify({ error: String(e && e.message ? e.message : e) }));
}
`;

const harnessPath = join(absInstall, "node_modules", ".conformance-manifest-harness.mjs");
let result;
try {
  writeFileSync(harnessPath, harnessSrc, "utf8");
  const proc = spawnSync(process.execPath, [harnessPath], {
    cwd: absInstall,
    encoding: "utf8",
    timeout: 60_000,
  });
  if (proc.status !== 0 && !proc.stdout) {
    fail(`Manifest harness failed (exit ${proc.status}): ${proc.stderr || "(no output)"}`);
  }
  const line = (proc.stdout || "").trim().split("\n").filter(Boolean).pop() || "";
  try {
    result = JSON.parse(line);
  } catch {
    fail(`Manifest harness produced no parseable output. stdout="${proc.stdout}" stderr="${proc.stderr}"`);
  }
} finally {
  try {
    rmSync(harnessPath, { force: true });
  } catch {
    /* best-effort cleanup */
  }
}

if (result.error) {
  fail(`Could not introspect the published build's ${serverMode} manifest: ${result.error}`);
}
const builtName = result.name;
const tools = Array.isArray(result.tools) ? result.tools : [];
if (tools.length === 0) {
  fail(`Published build's ${serverMode} server registered ZERO tools — introspection is unreliable; refusing to pass.`);
}

// ── Assert ───────────────────────────────────────────────────────────────────
console.log(`Package: ${pkg}`);
console.log(`Published ${serverMode} server name: ${builtName}`);
console.log(`Published ${serverMode} tool manifest (${tools.length}): ${tools.join(", ")}`);
console.log(`Accepted capabilities (required ∪ transitional): ${accepted.join(", ")}`);
console.log(`Required (canonical): ${requiredTools.join(", ")}`);
console.log(`Transitional aliases: ${aliases.join(", ") || "(none)"}`);

if (expectedServerName && !(typeof builtName === "string" && builtName === expectedServerName)) {
  fail(
    `Published ${serverMode} server registers under name "${builtName}" but the expected ` +
      `server name is "${expectedServerName}" — a consumer deriving ids from the server name ` +
      `would match NOTHING from this build.`,
  );
}

const presentAccepted = accepted.filter((v) => tools.includes(v));
if (presentAccepted.length === 0) {
  fail(
    `Published build exposes NONE of the accepted capabilities [${accepted.join(", ")}]. ` +
      `A consumer's fail-closed gate would reject EVERY entity provisioned from this build ` +
      `(the staging stale-build degrade). Publish a build that exposes at least one accepted ` +
      `capability before this can pass.`,
  );
}

const presentCanonical = requiredTools.filter((v) => tools.includes(v));
if (presentCanonical.length === 0) {
  warn(
    `Published build exposes ONLY transitional alias capability(s) [${presentAccepted.join(", ")}] — ` +
      `the canonical required capability(s) [${requiredTools.join(", ")}] are ABSENT. The gate passes ` +
      `today only because the transitional alias is still accepted. Removing the alias WITHOUT first ` +
      `publishing a build that exposes the canonical capability would re-trigger the degrade. Publish ` +
      `an updated build before retiring the alias.`,
  );
}

console.log(`OK — published build satisfies the conformance gate (accepted capabilities present: ${presentAccepted.join(", ")}).`);
process.exit(0);
