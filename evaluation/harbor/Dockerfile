# syntax=docker/dockerfile:1.18@sha256:dabfc0969b935b2080555ace70ee69a5261af8a8f1b4df97b9e7fbcf6722eddf
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv
FROM docker:28.4.0-cli@sha256:6a73c9433f2ba4279815be1e60f5739288b939dda1e48151d8c393537802de37 AS docker
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

ADD https://github.com/harbor-framework/harbor.git#3ee9f53e6b459712fdec4e8ce6613d7aa5610299 /opt/harbor
WORKDIR /opt/harbor
ENV UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/harbor-venv \
    PATH=/opt/harbor-venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
RUN echo '9ececffbba0731d4f8f0cf8fa117b39e59d4094d25582386689ea0901d7e041c  uv.lock' | sha256sum -c - \
    && uv sync --python /usr/local/bin/python --locked --no-dev --no-editable
