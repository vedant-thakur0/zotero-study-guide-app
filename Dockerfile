# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# Zotero Study Guide — production image (gunicorn)
#
# Platform-agnostic: runs on AWS ECS Express Mode (current target), Geddes/
# Kubernetes (eventual target), or any container host.
#
# BUILD FOR amd64: ECS Fargate is x86_64. Build with
#   docker buildx build --platform linux/amd64 ...
# A native arm64 build (Apple Silicon) crashes on ECS with "exec format error".
#
# Runtime secrets (NOT baked in): none required by default — the deployed app
# uses client-mode, where each user supplies their own LLM key at runtime via the
# Setup tab (sent per-request, never stored). PURDUE_GENAI_API_KEY env var is
# still honored if a single shared key is preferred.
#
# Default port: 8080. The host's injected $PORT is honored (-e PORT=<n> locally).
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Non-root user for least-privilege execution.
# Give it a real home dir (--home) so gunicorn's control server and any
# tooling have a writable HOME; without it the user's home is /nonexistent
# and gunicorn logs "[Errno 13] Permission denied: '/nonexistent'".
RUN addgroup --system zsg \
    && adduser --system --ingroup zsg --home /home/zsg zsg

WORKDIR /app

# ── Install Python deps first (layer-cached until requirements change) ──────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy only the files the app needs at runtime ────────────────────────────
# Source package
COPY src/ src/

# Config files that contain NO secrets (read at runtime by v2 routes)
COPY llm_config.yaml   .
COPY color_config.yaml .

# gunicorn entrypoint
COPY wsgi.py .

# ── Environment ──────────────────────────────────────────────────────────────
# Metrics writes go to /tmp so they don't fight a read-only app directory.
ENV ZSG_METRICS_PATH=/tmp/metrics.jsonl

# Default port (Cloud Run overrides this with the PORT env var at startup)
ENV PORT=8080

# Tell Python where the zsg package lives
ENV PYTHONPATH=/app/src

# Writable HOME for the non-root user (gunicorn control server, caches)
ENV HOME=/home/zsg

# Drop to non-root
USER zsg

# ── Entrypoint ───────────────────────────────────────────────────────────────
# 2 workers: safe default for Cloud Run single-CPU instances.
# Adjust with GUNICORN_WORKERS env var if needed.
CMD gunicorn \
        --bind "0.0.0.0:${PORT}" \
        --workers "${GUNICORN_WORKERS:-2}" \
        --timeout 120 \
        --access-logfile - \
        wsgi:app
