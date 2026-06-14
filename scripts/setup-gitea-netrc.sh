#!/usr/bin/env bash
# setup-gitea-netrc.sh — safe Gitea auth setup for agent runtimes.
#
# Problem: `curl -u "<user>:<token>"` leaks the token into process argv and
# platform activity logs. curl can read credentials from ~/.netrc instead,
# keeping the token out of argv.
#
# This script writes ~/.netrc from the agent's existing env credentials
# (GIT_HTTP_USERNAME / GIT_HTTP_PASSWORD) so that subsequent `curl --netrc`
# calls authenticate without exposing the token on the command line.
#
# Security: the token is written to a tempfile that is created mode 0600
# BEFORE any credential bytes land, then moved atomically into place. No
# intermediate file ever holds the token at a permission wider than 0600,
# regardless of the caller's umask.
#
# Owner/harness integration: run this once per agent session startup, before
# any Gitea API calls. The file is created with mode 600.

set -euo pipefail

NETRC="${HOME}/.netrc"
HOST="${GITEA_HOST:-git.moleculesai.app}"
USER="${GIT_HTTP_USERNAME:-}"
PASS="${GIT_HTTP_PASSWORD:-}"

if [ -z "$USER" ] || [ -z "$PASS" ]; then
  echo "::warning::GIT_HTTP_USERNAME or GIT_HTTP_PASSWORD not set; skipping ~/.netrc setup. Gitea curl calls will need an alternative safe-auth method." >&2
  exit 0
fi

# Create a private tempfile in the same directory as the destination so the
# final rename is atomic and cannot cross filesystems.
netrc_dir=$(dirname "$NETRC")
mkdir -p "$netrc_dir"
tmp=$(mktemp "$netrc_dir/.netrc.tmp.XXXXXX")

# Guarantee 0600 before writing any credential bytes. mktemp may create the
# file with 0600 on most systems, but we set it explicitly so the script is
# umask-independent and auditable.
chmod 600 "$tmp"

# Write credentials only after the file is confirmed private.
cat > "$tmp" <<EOF
machine $HOST
login $USER
password $PASS
EOF

# Atomic replace: readers either see the old file (or none) or the new file;
# they never see a partially-written or under-permissioned file.
mv -f "$tmp" "$NETRC"

# Defensive: ensure the final file is 0600 even if mktemp/umask/mv somehow
# widened permissions (e.g., ACLs).
chmod 600 "$NETRC"

echo "wrote $NETRC (mode 600) for $HOST"
