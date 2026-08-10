# syntax=docker/dockerfile:1.7
# Multi-stage build. Targets referenced by docker-compose.yml: api, mcp, worker.
# All three share one codebase and differ only in entrypoint.

FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# curl: the api healthcheck in docker-compose.yml.
# git:  Plane A ingestion resolves provenance with `git log -1 --format=%H -- <file>`
#       so a memory points at the exact commit that last touched its source file
#       (05-BUILD-PLAN Phase 1 acceptance). Without it ingest.git_sha() returns
#       None and every ingested memory silently loses its provenance.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
# The initial migration executes this file verbatim.
COPY 01-SCHEMA.sql ./sql/01-SCHEMA.sql
RUN pip install --no-cache-dir .

RUN useradd --uid 10001 --create-home appuser && chown -R appuser:appuser /app
USER appuser
ENV PYTHONPATH=/app/src

# ------------------------------------------------------------------ api / init
FROM base AS api
EXPOSE 8080
CMD ["uvicorn", "memory_platform.api:app", "--host", "0.0.0.0", "--port", "8080"]

# ------------------------------------------------------------------------ mcp
FROM base AS mcp
EXPOSE 8081
CMD ["uvicorn", "memory_platform.mcp_server:app", "--host", "0.0.0.0", "--port", "8081"]

# --------------------------------------------------------------------- worker
FROM base AS worker
CMD ["python", "-m", "memory_platform.worker"]
