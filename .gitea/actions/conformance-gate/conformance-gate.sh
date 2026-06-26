#!/usr/bin/env bash
# conformance-gate.sh — registry-provenance mode of the reusable conformance gate.
#
# GENERALIZED from molecule-ai/molecule-mcp-server/.gitea/scripts/provenance-gate.sh.
# The original hardcoded PKG / REGISTRY / ALLOWLIST; here they are env-driven so
# ANY consumer can adopt the same fail-closed publish-provenance check.
#
# WHAT THIS CHECKS
#   Every PUBLISHED version of $PACKAGE on the Gitea npm registry packument must
#   have a matching v<version> git tag. This catches out-of-band publishes — a
#   publish that skipped the tag-triggered workflow also skipped the v<version>
#   tag. (For @molecule-ai/mcp-server, 1.6.0 / 1.6.1 were published with no tag.)
#
# FAIL-CLOSED
#   - a published-but-untagged version (drift)                      -> exit 1
#   - the packument fetch is non-200 / empty / unparseable          -> exit 1
#   - git tag enumeration fails (TAGS empty while PUBLISHED nonempty -> the drift
#     computation flags every non-allowlisted version: correct fail-closed)
#   Never silently green on an infra error or unreachable registry.
#
# DIRECTION-AWARE
#   Only PUBLISHED-without-tag is a violation. TAGGED-without-publish (a tag may
#   precede or skip a publish) is BENIGN and tolerated.
#
# ALLOWLIST (advisory-first rollout)
#   $ALLOWLIST = space-separated pre-gate published versions, subtracted from the
#   drift set so the gate is GREEN at introduction without retro-tagging history.
#   FROZEN — new versions must be tag-provenanced. Clean alternative: retro-tag
#   the allowlisted versions and empty $ALLOWLIST.
#
# AUTH / SECRET
#   The packument is fetched from the Gitea npm registry. The raw packument
#   endpoint is unauthenticated-readable, but to be robust against the registry
#   enforcing read:package we send a bearer token IF one is present in the
#   environment as $REGISTRY_TOKEN. The token is OPTIONAL here.
#
# Env (set by run.sh from action inputs):
#   PACKAGE (required), REGISTRY (required), ALLOWLIST (optional),
#   REGISTRY_TOKEN (optional).

set -uo pipefail

PKG="${PACKAGE:?PACKAGE required}"
REGISTRY="${REGISTRY:?REGISTRY required}"
ALLOWLIST="${ALLOWLIST:-}"
PACKUMENT_URL="${REGISTRY%/}/${PKG}"

fail() { echo "CONFORMANCE-GATE (registry-provenance) FAIL: $*" >&2; echo "::error::conformance-gate registry-provenance: $*" >&2; exit 1; }

# --- 1. fetch the published-version set (the packument) ----------------------
# Optional bearer: only sent if REGISTRY_TOKEN is set & non-empty.
auth_args=()
if [ -n "${REGISTRY_TOKEN:-}" ]; then
  auth_args=(-H "Authorization: Bearer ${REGISTRY_TOKEN}")
fi

# -f makes curl exit non-zero on HTTP >=400; capture body + exit status.
PACKUMENT=$(curl -fsS "${auth_args[@]}" "$PACKUMENT_URL")
curl_rc=$?
if [ $curl_rc -ne 0 ]; then
  fail "could not fetch packument from $PACKUMENT_URL (curl rc=$curl_rc) -- failing closed instead of passing on an unreachable registry"
fi
if [ -z "$PACKUMENT" ]; then
  fail "packument fetch returned empty body from $PACKUMENT_URL -- failing closed"
fi

# Parse published versions out of `.versions | keys`. python3 is a hard CI dep;
# fail closed if the JSON is unparseable or yields no versions.
PUBLISHED=$(printf '%s' "$PACKUMENT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    sys.stderr.write("unparseable packument JSON: %s\n" % e)
    sys.exit(3)
v = d.get("versions")
if not isinstance(v, dict) or not v:
    sys.stderr.write("packument has no .versions object\n")
    sys.exit(4)
print("\n".join(sorted(v.keys())))
')
parse_rc=$?
if [ $parse_rc -ne 0 ]; then
  fail "could not parse published versions from packument (rc=$parse_rc) -- failing closed"
fi
if [ -z "$PUBLISHED" ]; then
  fail "no published versions parsed from packument -- failing closed"
fi

# --- 2. enumerate the v* git tag set -----------------------------------------
# CI checks out the repo, so local tags are authoritative. A shallow checkout
# may lack tags; fall back to the remote. An empty tag set with a non-empty
# publish set is itself a drift signal, not a reason to pass.
TAGS=$(git tag -l 'v*' 2>/dev/null | sed 's/^v//')
if [ -z "$TAGS" ]; then
  TAGS=$(git ls-remote --tags origin 'v*' 2>/dev/null \
    | sed -n 's#.*refs/tags/v\([^^{}]*\)$#\1#p')
fi

# --- 3. compute drift = published - tagged - allowlist -----------------------
DRIFT=""
for ver in $PUBLISHED; do
  tagged="no"
  for t in $TAGS; do
    if [ "$t" = "$ver" ]; then tagged="yes"; break; fi
  done
  [ "$tagged" = "yes" ] && continue
  allowed="no"
  for a in $ALLOWLIST; do
    if [ "$a" = "$ver" ]; then allowed="yes"; break; fi
  done
  [ "$allowed" = "yes" ] && continue
  DRIFT="$DRIFT $ver"
done
DRIFT=$(echo "$DRIFT" | xargs 2>/dev/null || true)

if [ -n "$DRIFT" ]; then
  fail "published versions with NO matching v* git tag (out-of-band publish?): $DRIFT
  Each published version must have a v<version> git tag. If this is a legitimate
  publish, create the v<version> tag. Do NOT add to the allowlist -- it is frozen."
fi

echo "CONFORMANCE-GATE (registry-provenance) PASS: every published version of $PKG has a matching v* git tag (allowlisted: ${ALLOWLIST:-none})"
