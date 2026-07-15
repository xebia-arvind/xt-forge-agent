# XT-Forge deployable image — Python + Node + Playwright Chromium in one
# container. Same image runs the web service (gunicorn) and the worker
# service (django-q2 qcluster); only the CMD differs (see render.yaml).
#
# Build context is the REPO ROOT (not this directory), so paths below are
# relative to the repo root:
#   docker build -f ai-healer-django/Dockerfile -t xt-forge:test .

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_MAJOR=20

# --- System deps: Node 20, build toolchain, Postgres client libs ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates build-essential libpq-dev \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- Playwright Chromium OS-level deps ---
# `install-deps` is best-effort; if npx isn't ready yet the failing exit
# doesn't kill the build, so `|| true`.
RUN npx --yes playwright install-deps chromium || true

WORKDIR /app

# --- Python deps first for layer caching ---
COPY ai-healer-django/requirements.txt /app/ai-healer-django/requirements.txt
RUN pip install --no-cache-dir -r /app/ai-healer-django/requirements.txt

# --- Node deps: repo-root cucumber-js + ts-morph sidecar ---
COPY package.json package-lock.json* /app/
COPY ai-healer-django/flaky_healer/test_generation/ts_normalizer/package*.json \
     /app/ai-healer-django/flaky_healer/test_generation/ts_normalizer/
RUN npm ci --omit=optional \
    && cd /app/ai-healer-django/flaky_healer/test_generation/ts_normalizer \
    && npm ci --omit=optional

# --- Copy the rest of the repo ---
COPY . /app/

# --- Playwright Chromium binary (baked into image so redeploys don't refetch) ---
RUN npx --yes playwright install chromium

# --- collectstatic at build time; env values below are build-only ---
# DATABASE_URL points at a dummy sqlite so Django's DATABASES resolves to
# a driver that IS installed. Without it, settings.py falls back to the
# MySQL block and Django tries to import mysqlclient (which we removed).
# collectstatic never opens a real DB connection — it only inspects
# staticfiles apps — so a nonexistent SQLite file is fine.
WORKDIR /app/ai-healer-django/flaky_healer
RUN DJANGO_SECRET_KEY=build-only \
    ALLOWED_HOSTS=* \
    DEBUG=true \
    DATABASE_URL="sqlite:////tmp/build.sqlite3" \
    python manage.py collectstatic --noinput

# Default command: gunicorn web server. The worker service overrides via
# render.yaml's dockerCommand (`python manage.py qcluster`).
CMD ["sh", "-c", "gunicorn flaky_healer.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${GUNICORN_WORKERS:-2} --timeout 120"]
