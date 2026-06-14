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

# Ensure the netrc is created with tight permissions.
umask 077

cat > "$NETRC" <<EOF
machine $HOST
login $USER
password $PASS
EOF

chmod 600 "$NETRC"
echo "wrote $NETRC (mode 600) for $HOST"
