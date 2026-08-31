# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.13.15
ARG PYTHON_IMAGE_DIGEST=sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca
ARG UV_VERSION=0.12.7
ARG UV_IMAGE_DIGEST=sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945

FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_IMAGE_DIGEST} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm@${PYTHON_IMAGE_DIGEST} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src

RUN uv sync --frozen --no-dev --no-editable

FROM python:${PYTHON_VERSION}-slim-bookworm@${PYTHON_IMAGE_DIGEST}

ENV HOME=/home/exporter \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    WORK_DIR=/tmp/dump

RUN groupadd --gid 10001 exporter \
    && useradd \
      --uid 10001 \
      --gid exporter \
      --create-home \
      --home-dir /home/exporter \
      --shell /usr/sbin/nologin \
      exporter \
    && mkdir -p /tmp/dump \
    && chown exporter:exporter /tmp/dump

COPY --from=builder --chown=exporter:exporter /app/.venv /app/.venv

USER 10001:10001
WORKDIR /home/exporter

ENTRYPOINT ["python", "-m", "database_exporter"]
