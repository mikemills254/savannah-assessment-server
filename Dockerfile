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

# Unprivileged user; the slim image only has root.
RUN useradd --system --uid 1001 --no-create-home app
USER app

ARG PORT=8000
ENV PORT=${PORT}
EXPOSE ${PORT}

# The slim image has no curl or wget. urlopen raises on the 503 /health returns
# while the db or Redis is down, so the check fails.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['PORT'] + '/health', timeout=4)" || exit 1

# Starts the API. Migrations run beforehand in the compose file's one-off
# `migrate` service. exec makes uvicorn PID 1, so SIGTERM on redeploy reaches it
# and in-flight requests finish.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"$PORT\""]
