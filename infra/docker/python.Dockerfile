# syntax=docker/dockerfile:1.7
# One image, many entrypoints (ingest / processor / api / simulator / batch): shared domain
# library, identical dependency set, single SBOM to scan. Each runs as its own Deployment.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home app
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app/src ./src
COPY db ./db
COPY data/bangalore_graph.npz ./data/bangalore_graph.npz
COPY data/kb ./data/kb
RUN mkdir -p /app/data/models /secrets && chown -R app /app/data /secrets
USER app
ENV GRAPH_PATH=/app/data/bangalore_graph.npz MODEL_DIR=/app/data/models
EXPOSE 8000 8001 9100
CMD ["python", "-m", "voltpath.api"]
