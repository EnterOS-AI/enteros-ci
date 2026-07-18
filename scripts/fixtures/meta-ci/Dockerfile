FROM python:3.11-slim

ARG RUNTIME_VERSION=
RUN test -n "${RUNTIME_VERSION}"

# The self-test fixture exercises the same executable delegation contract as the
# four official runtime templates. The meta-CI lockstep runner inspects this
# instruction; this fixture is never built or published as an image.
RUN bash "$(python3 -c 'import molecule_runtime')/scripts/prebake-mgmt-mcp.sh"
