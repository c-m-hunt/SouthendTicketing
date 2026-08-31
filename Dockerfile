# --- build stage: resolve dependencies into a self-contained venv ---
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

WORKDIR /opt/app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies before source, so the layer survives code changes.
# --no-install-project skips the app itself, which is not copied yet.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --extra deploy

COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra deploy

# --- runtime stage: no uv, no build tooling ---
FROM python:3.13-slim-bookworm

RUN useradd --create-home --uid 1000 app

# The app directory stays root-owned and read-only to the app user; only the
# database lives somewhere writable.
WORKDIR /opt/app
COPY --from=build /opt/app /opt/app

# SQLite needs to write the database and its journal, so the directory itself
# must be writable, not just the file. Mount a volume here to keep history
# across deploys.
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

ENV PATH="/opt/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL="sqlite:////data/app.db"

USER app

EXPOSE 8080

# Threads, not more workers: the database is a single SQLite file on one
# volume, so extra processes would contend for it, while the thing actually
# being waited on is the club's site. Two sync workers meant two concurrent
# requests in total, and one uncached availability read can hold a worker for
# as long as the upstream read timeout.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", \
     "--workers", "2", "--threads", "4", "--worker-class", "gthread", \
     "--timeout", "120", "wsgi:app"]
