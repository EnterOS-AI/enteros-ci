# meta-ci selftest fixture

A valid `runtime-template` fixture used by `.gitea/workflows/meta-ci-selftest.yml` to
prove the canonical router executes locally and emits its sentinel on the live runner.
Its exact `.runtime-version` matches the four immutable official-template refs below and
makes the data-only `mcp-pin-lockstep` bundle perform the real credential-free registry
verification. Docker installation and helper execution remain the runtime-template's
required Tier-4 proof.

`official-consumers.json` is the single list of immutable official-template candidate
commits used by the second self-test job. Its current four refs are the runtime 0.4.35
main commits (Claude Code and Hermes) or latest propagation heads (Codex and OpenClaw),
all of which resolve to the same attested runtime and MCP artifacts. The job rejects
duplicate JSON fields, anonymously fetches each commit, reads only its
`.runtime-version`, fails if the four pins differ, and runs the standalone artifact
checker against a one-file proof directory. The JSON is reference data only and does not
duplicate the verifier.
