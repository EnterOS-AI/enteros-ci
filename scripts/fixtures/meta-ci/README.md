# meta-ci selftest fixture

A valid `runtime-template` fixture used by `.gitea/workflows/meta-ci-selftest.yml` to
prove the reusable router runs and posts a single aggregate context on the live runner.
Its exact `.runtime-version` and executable Dockerfile prebake delegation make the
`mcp-pin-lockstep` bundle perform the real credential-free registry verification too.

`official-consumers.json` is the single list of immutable official-template commits used
by the second self-test job. That job anonymously fetches each commit, exports it with
`git archive`, and runs the current router against the clean tree; the JSON is reference
data only and does not duplicate the verifier.
