#!/usr/bin/env bash
# test-conformance-gate.sh — self-test for the reusable conformance gate.
#
# Exercises the FAIL-CLOSED branches of both modes offline (local fixtures, no
# network, no real npm install). It builds a tiny fake @scope/pkg whose
# createServer() registers a configurable tool set under a monkeypatchable MCP
# SDK shim, installs it into a throwaway node_modules by hand, and drives
# introspect-manifest.mjs against it. It also drives conformance-gate.sh with a
# stubbed packument (file:// registry) and a stubbed git tag set.
#
# Asserts the gate refuses to green on: empty accepted set, manifest missing all
# accepted caps, server-name mismatch, zero tools — and WARNs (exit 0) on a
# transitional-alias-only match. Mirrors the originals' contract.
#
# Run: bash test-conformance-gate.sh   (needs node + bash; no network)

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MJS="$HERE/introspect-manifest.mjs"
GATE_SH="$HERE/conformance-gate.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok   - $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL - $1"; }

# expect_exit <expected-code> <desc> -- runs the rest of the args, compares $?.
expect_exit() {
  local want="$1" desc="$2"; shift 2
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" -eq "$want" ]; then ok "$desc (exit $rc)"; else
    bad "$desc — wanted exit $want, got $rc"
    echo "$out" | sed 's/^/      /'
  fi
}
# expect_contains <needle> <desc> -- last command output must contain needle.
LAST_OUT=""
run_capture() { LAST_OUT="$("$@" 2>&1)"; return $?; }
expect_contains() {
  if printf '%s' "$LAST_OUT" | grep -qF "$1"; then ok "$2"; else
    bad "$2 — output missing: $1"; echo "$LAST_OUT" | sed 's/^/      /'
  fi
}

# ── Build a fake installed package with a configurable tool set ──────────────
# make_install <dir> <server-name> <tool1,tool2,...>
make_install() {
  local dir="$1" sname="$2" tools="$3"
  local nm="$dir/node_modules"
  rm -rf "$dir"; mkdir -p "$nm/@modelcontextprotocol/sdk/server" "$nm/@fake/pkg"
  # Minimal MCP SDK shim with a patchable McpServer.prototype.tool.
  cat > "$nm/@modelcontextprotocol/sdk/server/mcp.js" <<'EOF'
export class McpServer {
  constructor(info) { this._serverInfo = info || {}; this.server = { _serverInfo: this._serverInfo }; }
  tool(name) { return name; }
}
EOF
  cat > "$nm/@modelcontextprotocol/sdk/package.json" <<'EOF'
{ "name": "@modelcontextprotocol/sdk", "version": "0.0.0", "type": "module" }
EOF
  # Fake package whose createServer registers the requested tools.
  cat > "$nm/@fake/pkg/package.json" <<EOF
{ "name": "@fake/pkg", "version": "9.9.9", "type": "module", "main": "index.mjs" }
EOF
  local toolcalls=""
  IFS=',' read -ra arr <<< "$tools"
  for t in "${arr[@]}"; do [ -n "$t" ] && toolcalls+="  s.tool(${t@Q});"$'\n'; done
  cat > "$nm/@fake/pkg/index.mjs" <<EOF
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
export function createServer() {
  const s = new McpServer({ name: ${sname@Q} });
${toolcalls}  return s;
}
EOF
}

echo "== package-introspection mode =="
command -v node >/dev/null 2>&1 || { echo "node not found — skipping mjs tests"; }

if command -v node >/dev/null 2>&1; then
  D="$TMP/inst"

  # 1. Happy path: canonical cap present → PASS (exit 0).
  make_install "$D" "molecule-platform" "create_workspace,list_workspaces"
  run_capture node "$MJS" --package "@fake/pkg" --install-dir "$D" \
    --required-caps "create_workspace" --expected-server-name "molecule-platform"
  [ $? -eq 0 ] && ok "canonical cap present → pass" || bad "canonical cap present should pass"
  expect_contains "OK — published build satisfies" "pass emits OK line"

  # 2. Transitional-alias-only present → WARN but PASS (exit 0).
  make_install "$D" "molecule-platform" "provision_workspace"
  run_capture node "$MJS" --package "@fake/pkg" --install-dir "$D" \
    --required-caps "create_workspace" --transitional-aliases "provision_workspace" \
    --expected-server-name "molecule-platform"
  [ $? -eq 0 ] && ok "alias-only → pass (non-fatal)" || bad "alias-only should pass"
  expect_contains "::warning::" "alias-only emits ::warning::"

  # 3. NONE of accepted caps present → FAIL (exit 1) [the staging degrade].
  make_install "$D" "molecule-platform" "some_unrelated_tool"
  expect_exit 1 "no accepted caps → fail-closed" \
    node "$MJS" --package "@fake/pkg" --install-dir "$D" \
      --required-caps "create_workspace" --transitional-aliases "provision_workspace"

  # 4. Server-name mismatch → FAIL (exit 1).
  make_install "$D" "wrong-name" "create_workspace"
  expect_exit 1 "server-name mismatch → fail-closed" \
    node "$MJS" --package "@fake/pkg" --install-dir "$D" \
      --required-caps "create_workspace" --expected-server-name "molecule-platform"

  # 5. Zero tools registered → FAIL (exit 1, introspection unreliable).
  make_install "$D" "molecule-platform" ""
  expect_exit 1 "zero tools → fail-closed" \
    node "$MJS" --package "@fake/pkg" --install-dir "$D" --required-caps "create_workspace"

  # 6. Empty accepted set (no contract, no required-caps) → FAIL (exit 1).
  make_install "$D" "molecule-platform" "create_workspace"
  expect_exit 1 "empty required set → fail-closed" \
    node "$MJS" --package "@fake/pkg" --install-dir "$D" --required-caps ""

  # 7. createServer not exported → FAIL (exit 1).
  make_install "$D" "molecule-platform" "create_workspace"
  cat > "$D/node_modules/@fake/pkg/index.mjs" <<'EOF'
export const nothing = true;
EOF
  expect_exit 1 "createServer not exported → fail-closed" \
    node "$MJS" --package "@fake/pkg" --install-dir "$D" --required-caps "create_workspace"

  # 8. Contract-path drives the accepted set (plural required_tools + alias).
  make_install "$D" "molecule-platform" "create_workspace"
  cat > "$TMP/contract.json" <<'EOF'
{ "mcp_server_name": "molecule-platform",
  "required_tools": ["create_workspace"],
  "transitional_tool_aliases": ["provision_workspace"] }
EOF
  run_capture node "$MJS" --package "@fake/pkg" --install-dir "$D" --contract "$TMP/contract.json"
  [ $? -eq 0 ] && ok "contract-path accepted set → pass" || bad "contract-path should pass"
  expect_contains "molecule-platform" "contract mcp_server_name used as expected name"
fi

echo "== registry-provenance mode =="
# Drive conformance-gate.sh against a concrete packument file. The gate builds
# PACKUMENT_URL=${REGISTRY%/}/${PKG}. Set REGISTRY to a dir (file://) and PKG to
# a json filename so the concatenation lands on a real file — no network.
REGDIR="$TMP/regdir"; mkdir -p "$REGDIR"
cat > "$REGDIR/fakepkg.json" <<'EOF'
{ "name": "@fake/pkg", "versions": { "1.0.0": {}, "1.1.0": {}, "2.0.0": {} } }
EOF

# A repo with v1.0.0 + v1.1.0 tagged but NOT v2.0.0 (and no allowlist) → DRIFT.
GITREPO="$TMP/gitrepo"; mkdir -p "$GITREPO"
( cd "$GITREPO" && git init -q && git config user.email t@t && git config user.name t \
  && git commit -q --allow-empty -m init \
  && git tag v1.0.0 && git tag v1.1.0 )

# 9. Drift (2.0.0 published, untagged, not allowlisted) → FAIL (exit 1).
run_capture bash -c "cd '$GITREPO' && PACKAGE='fakepkg.json' REGISTRY='file://$REGDIR' ALLOWLIST='' bash '$GATE_SH'"
[ $? -eq 1 ] && ok "untagged published version → drift fail-closed" || bad "drift should fail-closed (got $?)"
expect_contains "2.0.0" "drift names the untagged version"

# 10. Allowlisting 2.0.0 → PASS (exit 0).
run_capture bash -c "cd '$GITREPO' && PACKAGE='fakepkg.json' REGISTRY='file://$REGDIR' ALLOWLIST='2.0.0' bash '$GATE_SH'"
[ $? -eq 0 ] && ok "allowlisted version → pass" || bad "allowlisted should pass (got $?)"

# 11. Unreachable/empty packument → FAIL (exit 1).
expect_exit 1 "unreachable packument → fail-closed" \
  bash -c "cd '$GITREPO' && PACKAGE='nope.json' REGISTRY='file://$REGDIR' ALLOWLIST='' bash '$GATE_SH'"

# 12. Unparseable packument JSON → FAIL (exit 1).
echo "not json" > "$REGDIR/bad.json"
expect_exit 1 "unparseable packument → fail-closed" \
  bash -c "cd '$GITREPO' && PACKAGE='bad.json' REGISTRY='file://$REGDIR' ALLOWLIST='' bash '$GATE_SH'"

echo
echo "== summary: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
