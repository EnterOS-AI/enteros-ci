# Safe Gitea authentication for agents

## Problem

Agents calling the Gitea API with `curl -u "<user>:<token>"` leak the token
into the process argument vector (`/proc/*/cmdline`) and platform activity logs.
This is a fleet-wide security hygiene issue — any process on the host or any log
reader can capture the token.

## Fix

Use `~/.netrc` (mode `600`) so curl reads credentials from a file instead of the
command line. Provide a setup script and a `gitea-curl` wrapper to make the safe
path the easy path.

## Setup

Run once per agent session (ideally from agent harness startup):

```bash
bash molecule-ci/scripts/setup-gitea-netrc.sh
```

Prerequisites:
- `GIT_HTTP_USERNAME` and `GIT_HTTP_PASSWORD` are set by the harness.
- `GITEA_HOST` defaults to `git.moleculesai.app`; override if needed.

This writes `~/.netrc` with mode `600`:

```
machine git.moleculesai.app
login <GIT_HTTP_USERNAME>
password <GIT_HTTP_PASSWORD>
```

## Usage

After setup, use `gitea-curl` in place of `curl` for Gitea API calls:

```bash
# OK — token is read from ~/.netrc, never appears in argv
gitea-curl -sS https://git.moleculesai.app/api/v1/user

# OK — all normal curl flags work
gitea-curl -sS -X GET \
  "https://git.moleculesai.app/api/v1/repos/molecule-ai/molecule-core/pulls?state=open"
```

`gitea-curl`:
- Forces `curl --netrc`.
- Refuses `-u` / `--user`.
- Refuses inline `-H Authorization: ...` headers.

### Plain curl

If you must use plain curl, always pass `--netrc` and never `-u`:

```bash
# OK
curl --netrc -sS https://git.moleculesai.app/api/v1/user

# NEVER DO THIS — token leaks in argv
curl -u "$GIT_HTTP_USERNAME:$GIT_HTTP_PASSWORD" https://git.moleculesai.app/api/v1/user
```

## Files

- `scripts/setup-gitea-netrc.sh` — writes `~/.netrc` from env credentials.
- `bin/gitea-curl` — safe curl wrapper for `git.moleculesai.app`.

## Owner/harness-gated piece

The setup script must be invoked by the agent harness at session startup. If the
harness cannot be changed, agents can run it manually, but the durable fix
requires harness integration so the safe-auth path is always available before
any Gitea API call.

The harness should also avoid printing `GIT_HTTP_PASSWORD` in logs or passing it
to subprocesses that do not need it. Rotating to short-lived tokens and storing
them in a file (e.g., `/run/secrets/gitea-token`) would be even stronger, but
that requires harness-level secret provisioning.
