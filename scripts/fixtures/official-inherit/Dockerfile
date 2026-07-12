FROM python:3.11-slim
ARG RUNTIME_VERSION=
ARG MOLECULE_RUNTIME_INDEX=https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/
RUN useradd -u 1000 -m -s /bin/bash agent
WORKDIR /app
COPY requirements.txt .
RUN runtime_requirement="molecules-workspace-runtime"; \
    if [ -n "${RUNTIME_VERSION}" ]; then \
      runtime_requirement="${runtime_requirement}==${RUNTIME_VERSION}"; \
    fi; \
    mkdir -p /tmp/molecule-runtime; \
    pip download --isolated --only-binary=:all: --no-deps \
      --index-url "${MOLECULE_RUNTIME_INDEX}" \
      --dest /tmp/molecule-runtime "${runtime_requirement}"; \
    pip install --isolated -r requirements.txt /tmp/molecule-runtime/*.whl; \
    rm -rf /tmp/molecule-runtime
ENTRYPOINT ["molecule-runtime"]
