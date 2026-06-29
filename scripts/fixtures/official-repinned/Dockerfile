FROM python:3.11-slim
ARG RUNTIME_VERSION=
RUN useradd -u 1000 -m -s /bin/bash agent
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && \
    if [ -n "${RUNTIME_VERSION}" ]; then \
      pip install --no-cache-dir --upgrade "molecule-ai-workspace-runtime==${RUNTIME_VERSION}"; \
    fi
ENTRYPOINT ["molecule-runtime"]
