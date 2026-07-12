FROM python:3.11-slim
ARG RUNTIME_VERSION=
ARG MOLECULE_RUNTIME_INDEX=https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/
RUN useradd -u 1000 -m -s /bin/bash agent
WORKDIR /app
COPY requirements.txt .
RUN runtime_project="molecules-workspace-runtime"; \
    runtime_requirement="$(python3 /tmp/prepare_runtime_requirements.py \
      requirements.txt /tmp/template-requirements.txt \
      --runtime-version "${RUNTIME_VERSION}")"; \
    case "${runtime_requirement}" in "${runtime_project}"*) ;; *) exit 1 ;; esac; \
    rm -rf /tmp/molecule-runtime; \
    mkdir -p /tmp/molecule-runtime; \
    pip download --isolated --only-binary=:all: --no-deps \
      --index-url "${MOLECULE_RUNTIME_INDEX}" \
      --dest /tmp/molecule-runtime "${runtime_requirement}"; \
    test "$(find /tmp/molecule-runtime -maxdepth 1 -name '*.whl' | wc -l)" -eq 1; \
    pip install --isolated -r /tmp/template-requirements.txt /tmp/molecule-runtime/*.whl; \
    rm -rf /tmp/molecule-runtime
ENTRYPOINT ["molecule-runtime"]
