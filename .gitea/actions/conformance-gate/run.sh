#!/usr/bin/env bash
# conformance-gate run.sh — dispatcher for the reusable conformance gate.
#
# Invoked by the `conformance-gate` composite action (action.yml). The action
# threads inputs in as env. This script dispatches on $MODE:
#
#   registry-provenance   — generalized molecule-mcp-server provenance-gate.sh:
#                           every PUBLISHED version of $PACKAGE on the registry
#                           packument must have a matching v<version> git tag.
#                           PACKAGE / REGISTRY / ALLOWLIST are env-driven (the
#                           original hardcoded them).
#   package-introspection — install the PUBLISHED $PACKAGE@$VERSION from the
#                           Gitea npm registry into a throwaway dir, then run
#                           introspect-manifest.mjs to assert the build's ACTUAL
#                           tool manifest ⊇ the contract's accepted capabilities.
#
# FAIL-CLOSED: an infra error / unreachable registry / unparseable manifest /
# empty required set NEVER passes. The only non-fatal band is the
# package-introspection transitional-alias WARN (emitted by the .mjs).
#
# AUTH / TRUST: $REGISTRY_TOKEN is OPTIONAL (the registry endpoints are public).
# When $REQUIRE_TOKEN=true and the token is empty: fail-closed on a TRUSTED
# context ($IS_TRUSTED=true), soft-skip on an UNTRUSTED one (fork PRs can't hold
# secrets; the trusted post-merge/scheduled run gates before any provision).

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE="${MODE:-}"
PACKAGE="${PACKAGE:-}"
REGISTRY="${REGISTRY:-https://git.moleculesai.app/api/packages/molecule-ai/npm}"
VERSION="${VERSION:-latest}"
ALLOWLIST="${ALLOWLIST:-}"
CONTRACT_PATH="${CONTRACT_PATH:-}"
REQUIRED_CAPS="${REQUIRED_CAPS:-}"
TRANSITIONAL_ALIASES="${TRANSITIONAL_ALIASES:-}"
SERVER_MODE="${SERVER_MODE:-management}"
EXPECTED_SERVER_NAME="${EXPECTED_SERVER_NAME:-}"
REGISTRY_TOKEN="${REGISTRY_TOKEN:-}"
REQUIRE_TOKEN="${REQUIRE_TOKEN:-false}"
IS_TRUSTED="${IS_TRUSTED:-true}"

fail() { echo "::error::CONFORMANCE-GATE FAIL: $*" >&2; exit 1; }
note() { echo "::notice::$*"; }

[ -n "$MODE" ]    || fail "input 'mode' is required (registry-provenance | package-introspection)"
[ -n "$PACKAGE" ] || fail "input 'package' is required"

# --- shared token / trust gate ------------------------------------------------
# Only enforced when the caller opts in via require-token. Soft-skip is reserved
# for genuinely untrusted contexts — never for trusted ones (that would be a
# fail-OPEN hole on the path that actually gates provisioning).
if [ "$REQUIRE_TOKEN" = "true" ] && [ -z "$REGISTRY_TOKEN" ]; then
  if [ "$IS_TRUSTED" = "true" ]; then
    fail "require-token=true but registry-token is empty on a TRUSTED context -- failing closed (a read:package token is mandatory here)"
  fi
  note "require-token=true and registry-token empty on an UNTRUSTED context (fork PR) -- soft-skipping; the trusted run gates before any consumer provisions."
  exit 0
fi

case "$MODE" in
  registry-provenance)
    export PACKAGE REGISTRY ALLOWLIST REGISTRY_TOKEN
    exec bash "$HERE/conformance-gate.sh"
    ;;

  package-introspection)
    command -v node >/dev/null 2>&1 || fail "node is required for package-introspection mode but was not found on PATH"
    command -v npm  >/dev/null 2>&1 || fail "npm is required for package-introspection mode but was not found on PATH"

    if [ -z "$CONTRACT_PATH" ] && [ -z "${REQUIRED_CAPS//[[:space:]]/}" ]; then
      fail "package-introspection needs either contract-path or required-caps (both empty) -- refusing to derive an empty accepted set"
    fi

    WORKDIR="$(mktemp -d 2>/dev/null || mktemp -d -t conformance-gate)"
    trap 'rm -rf "$WORKDIR"' EXIT
    cd "$WORKDIR" || fail "could not enter throwaway install dir $WORKDIR"

    # Scope the package's registry + optional auth. Mirrors the install wiring
    # of molecule-core/.gitea/workflows/mcp-verb-published-manifest.yml. The
    # scope is the leading @scope of the package name, if any.
    SCOPE=""
    case "$PACKAGE" in
      @*/*) SCOPE="${PACKAGE%%/*}" ;;  # e.g. @molecule-ai
    esac
    # Registry host (strip scheme) for the _authToken key.
    REG_NOSCHEME="${REGISTRY#https:}"
    REG_NOSCHEME="${REG_NOSCHEME#http:}"
    {
      if [ -n "$SCOPE" ]; then
        echo "${SCOPE}:registry=${REGISTRY}"
      else
        echo "registry=${REGISTRY}"
      fi
      if [ -n "$REGISTRY_TOKEN" ]; then
        echo "${REG_NOSCHEME}/:_authToken=${REGISTRY_TOKEN}"
      fi
    } > "$WORKDIR/.npmrc"

    echo "Installing PUBLISHED ${PACKAGE}@${VERSION} from ${REGISTRY} into a throwaway dir..."
    if ! npm install --no-save --no-audit --no-fund --userconfig "$WORKDIR/.npmrc" "${PACKAGE}@${VERSION}" >npm-install.log 2>&1; then
      sed 's/^/  npm: /' npm-install.log >&2 || true
      fail "could not install ${PACKAGE}@${VERSION} from the registry -- failing closed (never green on an install/infra error)"
    fi

    PKG_DIR="$WORKDIR/node_modules/$PACKAGE"
    [ -d "$PKG_DIR" ] || fail "installed but $PKG_DIR is missing -- failing closed"
    RESOLVED="$(node -e 'try{process.stdout.write(require(process.argv[1]).version||"")}catch(e){process.exit(0)}' "$PKG_DIR/package.json" 2>/dev/null || true)"
    echo "Resolved ${PACKAGE} version: ${RESOLVED:-unknown} (requested ${VERSION})"

    args=( --install-dir "$WORKDIR" --server-mode "$SERVER_MODE" )
    [ -n "$CONTRACT_PATH" ]          && args+=( --contract "$CONTRACT_PATH" )
    [ -n "$REQUIRED_CAPS" ]          && args+=( --required-caps "$REQUIRED_CAPS" )
    [ -n "$TRANSITIONAL_ALIASES" ]   && args+=( --transitional-aliases "$TRANSITIONAL_ALIASES" )
    [ -n "$EXPECTED_SERVER_NAME" ]   && args+=( --expected-server-name "$EXPECTED_SERVER_NAME" )
    [ -n "$PACKAGE" ]                && args+=( --package "$PACKAGE" )

    exec node "$HERE/introspect-manifest.mjs" "${args[@]}"
    ;;

  *)
    fail "unknown mode '$MODE' (expected registry-provenance | package-introspection)"
    ;;
esac
