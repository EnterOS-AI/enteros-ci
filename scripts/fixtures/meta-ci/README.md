# meta-ci selftest fixture

A valid `runtime-template` fixture used by `.gitea/workflows/meta-ci-selftest.yml` to
prove the canonical router executes locally and emits its sentinel on the live runner.
Its exact `.runtime-version` matches the four immutable official-template refs below and
makes the data-only `mcp-pin-lockstep` bundle perform the real credential-free registry
verification. Docker installation and helper execution remain the runtime-template's
required Tier-4 proof.

`official-consumers.json` is the single list of immutable official-template
commits used by the second self-test job. Its current four refs are the reviewed merge
commits that wire the same immutable final-image MCP verifier into each Tier-4 job. The
job rejects duplicate JSON fields, anonymously fetches each commit, and reads only three
bounded static files: `.runtime-version`, `.gitea/workflows/ci.yml`, and
`tests/test_ci_runtime_image_pin.py`. It fails if the four runtime pins differ, runs the
standalone artifact checker, and statically proves that each workflow and its regression
test pin the reviewed molecule-ci verifier and retain the offline, unprivileged,
resource-bounded final-image contract. Consumer workflow and test code is never
executed. The JSON remains reference data only and does not duplicate either verifier.
