# syntax=docker/dockerfile:1

FROM python:3.13-slim AS base
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── deps: install into a virtualenv the runner copies whole ──
# Only requirements.txt is copied, so a code change doesn't reinstall packages.
FROM base AS deps
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

# ── runner: production image ──
FROM base AS runner
ENV PATH="/opt/venv/bin:$PATH"

COPY --from=deps /opt/venv /opt/venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# curl: Coolify's health check runs it inside the container, and the slim
# image has neither curl nor wget.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged user; the slim image only has root.
RUN useradd --system --uid 1001 --no-create-home app
USER app

ARG PORT=8000
ENV PORT=${PORT}
EXPOSE ${PORT}

# -f fails on the 503 /health returns while the db or Redis is down.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS --max-time 4 "http://127.0.0.1:${PORT}/health" > /dev/null || exit 1

# Coolify passes the commit as a build arg when "Include Source Commit in
# Build" is on; /health reports it. Last, so a new commit only rebuilds this.
ARG SOURCE_COMMIT=""
ENV SOURCE_COMMIT=${SOURCE_COMMIT}

# Starts the API. Migrations normally run beforehand: the compose file's
# one-off `migrate` service. Where there is no such step, as on Coolify,
# MIGRATE_ON_START=true runs them here, and a failed migration stops the
# container so the host keeps the previous version.
# exec makes uvicorn PID 1, so SIGTERM on redeploy reaches it and in-flight
# requests finish. --proxy-headers: behind the host's load balancer, so URLs
# FastAPI builds (e.g. redirects) keep https. WEB_CONCURRENCY sets workers.
CMD ["sh", "-c", "if [ \"$MIGRATE_ON_START\" = true ]; then alembic upgrade head || exit 1; fi; exec uvicorn app.main:app --host 0.0.0.0 --port \"$PORT\" --proxy-headers --forwarded-allow-ips '*'"]
