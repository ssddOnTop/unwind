# syntax=docker/dockerfile:1
FROM python:3.12.12-slim-bookworm AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app/be
COPY be/pyproject.toml be/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

FROM python:3.12.12-slim-bookworm AS runtime
ENV PATH="/app/be/.venv/bin:$PATH" \
    PYTHONPATH=/app/be \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COMPANION_DAILY_SYNTHESIS_MODE=disabled \
    LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false
RUN groupadd --gid 10001 companion \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin companion \
    && install -d -m 0700 -o 10001 -g 10001 /data
WORKDIR /app/be
COPY --from=dependencies /app/be/.venv /app/be/.venv
COPY be/companion /app/be/companion
# Only schemas and rules ship; example records live exclusively in test targets.
COPY contracts/*.json /app/contracts/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import json, urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2); assert r.status == 200 and json.load(r) == {'status': 'ok'}"]
CMD ["python", "-m", "companion.container"]
