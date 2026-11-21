FROM ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec AS uv

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS python-git
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

FROM python-git AS workspace
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace

FROM python-git AS verifier
RUN install -d -m 0700 /grading
COPY --chmod=0400 grading/checks.json /grading/checks.json
COPY src/factory/verify.py /grading/verify.py
ENTRYPOINT ["python", "-IB", "/grading/verify.py"]

FROM workspace AS delivery-worker
COPY pyproject.toml uv.lock /workspace/
RUN uv sync --locked --no-dev --extra recovery
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/factory/src
COPY src/factory/ /factory/src/factory/
COPY grading/checks.json /grading/checks.json
WORKDIR /factory

FROM python-git AS recovery-service
ENV PYTHONPATH=/factory/src:/factory PYTHONDONTWRITEBYTECODE=1
COPY src/factory/ /factory/src/factory/
COPY benchmarks/ /factory/benchmarks/
WORKDIR /factory
ENTRYPOINT ["python", "-m", "factory.recovery_services"]

FROM verifier AS recovery-verifier
ENV PYTHONPATH=/factory/src PYTHONDONTWRITEBYTECODE=1
COPY src/factory/ /factory/src/factory/
ENTRYPOINT ["python", "-m", "factory.recovery_services", "verifier"]

FROM workspace AS codex-workspace
COPY pyproject.toml uv.lock /workspace/
RUN uv sync --locked --no-dev --extra recovery
ENV PATH=/opt/venv/bin:$PATH UV_NO_SYNC=true
COPY src/factory/ /factory/src/factory/
WORKDIR /
