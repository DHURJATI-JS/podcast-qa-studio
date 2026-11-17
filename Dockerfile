# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------- 1. front-end assets
# Tailwind CSS (minified) + esbuild (minified JS bundle incl. the Socket.IO client).
FROM node:22.21-alpine3.22 AS assets
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
COPY app/templates /build/app/templates
COPY app/static/js /build/app/static/js
RUN npm run build

# ---------------------------------------------------------------- 2. shared base
FROM python:3.13-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY --from=assets /build/app/static/dist ./app/static/dist

# ---------------------------------------------------------------- 3. tests (docker-compose.test.yml)
FROM base AS test
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY pytest.ini ./
COPY tests ./tests
COPY loadtest ./loadtest
USER app
CMD ["python", "-m", "pytest", "-q"]

# ---------------------------------------------------------------- 4. runtime (default, last stage)
FROM base AS runtime
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1
CMD ["python", "-m", "app.cli", "serve"]
