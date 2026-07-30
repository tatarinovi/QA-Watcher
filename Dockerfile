FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app

RUN mkdir -p /app/data /app/reports \
    && chmod 700 /app/data /app/reports \
    && useradd --system --uid 10001 --home /app watcher \
    && chown -R watcher:watcher /app

USER watcher
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)"]
CMD ["/app/.venv/bin/python", "-m", "app.cli", "serve"]
